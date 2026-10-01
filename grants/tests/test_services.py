from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from grants import services
from grants.effort import effort_summary, overlap_fraction, project_person_months
from grants.models import (
    Activity,
    Application,
    Award,
    ChecklistItem,
    ChecklistTemplate,
    Funder,
    StatusChange,
    Task,
)

from .helpers import add_person, make_app, make_user


class StatusWorkflowTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_submitting_records_date_history_and_activity(self):
        app = make_app(status=Application.Status.ROUTING)
        services.change_status(app, Application.Status.SUBMITTED, self.user, "via ORA")
        app.refresh_from_db()
        self.assertEqual(app.submitted_on, timezone.localdate())
        sc = StatusChange.objects.get(application=app)
        self.assertEqual((sc.from_status, sc.to_status, sc.note), ("routing", "submitted", "via ORA"))
        self.assertTrue(Activity.objects.filter(application=app, kind=Activity.Kind.STATUS).exists())

    def test_existing_dates_are_kept(self):
        app = make_app(status=Application.Status.SUBMITTED, submitted_on=date(2025, 6, 5))
        services.change_status(app, Application.Status.NOT_FUNDED, self.user)
        app.refresh_from_db()
        self.assertEqual(app.submitted_on, date(2025, 6, 5))
        self.assertEqual(app.decision_on, timezone.localdate())

    def test_awarding_creates_award_periods_checklist_and_reports(self):
        app = make_app(
            status=Application.Status.PENDING_AWARD, proposed_start=date(2026, 7, 1), proposed_end=date(2031, 6, 30),
            requested_total=Decimal("3000000"), requested_direct_total=Decimal("2000000"),
        )
        award = services.change_status(app, Application.Status.AWARDED, self.user)
        self.assertIsInstance(award, Award)
        self.assertEqual(award.reporting, Funder.Reporting.NIH_SNAP)
        periods = list(award.periods.all())
        self.assertEqual(len(periods), 5)
        self.assertEqual(sum(p.total for p in periods), Decimal("3000000"))
        self.assertEqual(sum(p.direct_costs for p in periods), Decimal("2000000"))
        self.assertEqual(periods[0].start_date, date(2026, 7, 1))
        self.assertEqual(periods[-1].end_date, date(2031, 6, 30))
        # Default "Award setup" template from the seed migration plus the generated reporting schedule
        self.assertTrue(app.tasks.filter(title="Review Notice of Award terms and conditions").exists())
        self.assertEqual(app.tasks.filter(auto_key__startswith=f"award:{award.pk}:").count(), 4 + 3)

    def test_same_status_is_a_noop(self):
        app = make_app(status=Application.Status.DRAFTING)
        self.assertIsNone(services.change_status(app, Application.Status.DRAFTING, self.user))
        self.assertFalse(StatusChange.objects.exists())


class ReportingScheduleTests(TestCase):
    def setUp(self):
        self.app = make_app(status=Application.Status.AWARDED)

    def award(self, **kw):
        defaults = dict(application=self.app, start_date=date(2026, 7, 1), end_date=date(2031, 6, 30), reporting=Funder.Reporting.NIH_SNAP)
        defaults.update(kw)
        return Award.objects.create(**defaults)

    def test_nih_snap_dates(self):
        items = {key: due for key, _, _, due, _ in services.reporting_schedule(self.award())}
        # Budget period 1 ends June 30, 2027 -> RPPR due the 15th of the preceding month.
        self.assertEqual(items["rppr:2"], date(2027, 5, 15))
        self.assertEqual(items["rppr:5"], date(2030, 5, 15))
        self.assertNotIn("rppr:6", items)
        self.assertEqual(items["final_rppr"], date(2031, 6, 30) + timedelta(days=120))
        self.assertEqual(items["final_ffr"], items["final_fis"])

    def test_nce_moves_final_reports(self):
        items = {k: d for k, _, _, d, _ in services.reporting_schedule(self.award(nce_end_date=date(2032, 6, 30)))}
        self.assertEqual(items["final_rppr"], date(2032, 6, 30) + timedelta(days=120))

    def test_generic_annual_schedule(self):
        items = services.reporting_schedule(self.award(reporting=Funder.Reporting.ANNUAL, end_date=date(2029, 6, 30)))
        keys = [k for k, *_ in items]
        self.assertEqual(keys, ["annual:1", "annual:2", "final_report"])

    def test_regeneration_is_idempotent_and_respects_completed_tasks(self):
        award = self.award()
        self.assertEqual(services.generate_reporting_tasks(award)[0], 7)
        self.assertEqual(services.generate_reporting_tasks(award), (0, 0, 0))
        done = self.app.tasks.get(auto_key=f"award:{award.pk}:rppr:2")
        done.mark(Task.Status.DONE)
        done.save()
        award.start_date, award.end_date = date(2026, 9, 1), date(2029, 8, 31)
        award.save()
        created, updated, removed = services.generate_reporting_tasks(award)
        done.refresh_from_db()
        self.assertEqual(done.due_date, date(2027, 5, 15))  # completed task untouched
        self.assertEqual(removed, 2)  # years 4 and 5 no longer exist
        self.assertGreater(updated, 0)


class ChecklistTests(TestCase):
    def test_offsets_weekend_shift_and_dedupe(self):
        app = make_app(sponsor_deadline=date(2026, 10, 5), internal_deadline=date(2026, 9, 28))  # a Monday
        t = ChecklistTemplate.objects.create(name="T")
        ChecklistItem.objects.create(template=t, title="Aims", offset_days=-1)  # Sunday -> Friday Oct 2
        ChecklistItem.objects.create(template=t, title="Route", anchor="internal", offset_days=0)
        ChecklistItem.objects.create(template=t, title="Undated", anchor="none")
        services.apply_checklist(app, t)
        due = dict(app.tasks.values_list("title", "due_date"))
        self.assertEqual(due["Aims"], date(2026, 10, 2))
        self.assertEqual(due["Route"], date(2026, 9, 28))
        self.assertIsNone(due["Undated"])
        self.assertEqual(services.apply_checklist(app, t), [])

    def test_seeded_templates_exist(self):
        self.assertTrue(ChecklistTemplate.objects.filter(name__startswith="NIH research grant").exists())
        self.assertTrue(ChecklistTemplate.objects.filter(applies_to="award", is_default=True).exists())


class ResubmissionTests(TestCase):
    def test_clone_carries_team_and_links_parent(self):
        user = make_user()
        a0 = make_app(short_name="NSC R01", status=Application.Status.NOT_FUNDED, abstract="Aims...")
        add_person(a0, pm="2.4")
        a1 = services.clone_application(a0, Application.SubmissionType.RESUBMISSION, user)
        self.assertEqual(a1.parent, a0)
        self.assertEqual(a1.short_name, "NSC R01 A1")
        self.assertEqual(a1.resubmission_label, "A1")
        self.assertEqual(a1.status, Application.Status.PLANNING)
        self.assertEqual(a1.personnel.get().person_months, Decimal("2.4"))
        self.assertTrue(a1.tasks.filter(title__icontains="Introduction").exists())
        self.assertEqual([a.pk for a in a1.lineage()], [a0.pk, a1.pk])
        a2 = services.clone_application(a1, Application.SubmissionType.RESUBMISSION, user)
        self.assertEqual(a2.resubmission_label, "A2")
        self.assertEqual(a2.short_name, "NSC R01 A2")


class EffortTests(TestCase):
    def test_overlap_fraction(self):
        self.assertEqual(overlap_fraction(date(2026, 1, 1), date(2026, 12, 31), date(2026, 1, 1), date(2026, 12, 31)), 1)
        self.assertEqual(overlap_fraction(None, None, date(2026, 1, 1), date(2026, 12, 31)), 1)
        self.assertEqual(overlap_fraction(date(2027, 1, 1), None, date(2026, 1, 1), date(2026, 12, 31)), 0)

    def test_summary_splits_active_and_pending(self):
        today = timezone.localdate()
        funded = make_app(status=Application.Status.AWARDED)
        Award.objects.create(application=funded, start_date=today - timedelta(days=30), end_date=today + timedelta(days=700))
        line = add_person(funded, pm="3")
        pending = make_app(status=Application.Status.SUBMITTED)
        from grants.models import Personnel

        Personnel.objects.create(application=pending, person=line.person, role="pi", person_months=Decimal("1.2"))
        closed = make_app(status=Application.Status.NOT_FUNDED)
        Personnel.objects.create(application=closed, person=line.person, role="pi", person_months=Decimal("6"))
        row = effort_summary()[0]
        self.assertEqual(row["active_pm"], Decimal("3"))
        self.assertEqual(row["pending_pm"], Decimal("1.2"))
        self.assertEqual(row["active_pct"], 25)

    def test_project_person_months_per_budget_year(self):
        app = make_app(status=Application.Status.SUBMITTED, proposed_start=date(2026, 7, 1), proposed_end=date(2029, 6, 30))
        line = add_person(app, pm="1.2")
        years = project_person_months(app, line.person)
        self.assertEqual([y["pm"] for y in years], [Decimal("1.20")] * 3)


class DocumentStorageTests(TestCase):
    def test_upload_is_stored_in_database_with_text(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from grants.models import Document, DocumentBlob

        doc = Document.objects.create(title="Aims")
        services.store_upload(doc, SimpleUploadedFile("aims.txt", b"Neural stem cells in squid"))
        doc.refresh_from_db()
        self.assertEqual(doc.size, 26)
        self.assertEqual(len(doc.sha256), 64)
        self.assertIn("squid", doc.extracted_text)
        self.assertEqual(bytes(DocumentBlob.objects.get(document=doc).data), b"Neural stem cells in squid")

    def test_bad_pdf_does_not_crash_extraction(self):
        self.assertEqual(services.extract_text(b"not a pdf", "x.pdf"), "")
