import io
import json
from datetime import date, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from grants import services
from grants.models import Application, Award, Document, Funder, Opportunity, Task

from .helpers import add_person, make_app, make_user

HX = {"HTTP_HX_REQUEST": "true"}


class AccessControlTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.editor = make_user("editor", User.Role.EDITOR)
        self.viewer = make_user("viewer", User.Role.VIEWER)
        self.app = make_app()

    def test_everything_requires_login(self):
        for url in ["/", "/applications/", f"/applications/{self.app.pk}/", "/analytics/", "/documents/", "/admin/"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("login", r["Location"])

    def test_healthz_is_public(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_viewer_is_read_only(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(f"/applications/{self.app.pk}/").status_code, 200)
        self.assertEqual(self.client.get("/applications/new/").status_code, 403)
        r = self.client.post(f"/applications/{self.app.pk}/status/", {"status": "submitted", "quick": "1"})
        self.assertEqual(r.status_code, 403)
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, Application.Status.PLANNING)

    def test_editor_edits_but_cannot_delete_or_manage_users(self):
        self.client.force_login(self.editor)
        r = self.client.post("/applications/new/", {"title": "New one", "initial_status": "planning",
                                                    "submission_type": "new", "priority": "medium", "role": "pi"})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        self.assertTrue(Application.objects.filter(title="New one").exists())
        self.assertEqual(self.client.post(f"/applications/{self.app.pk}/delete/").status_code, 403)
        self.assertEqual(self.client.get("/accounts/users/").status_code, 403)

    def test_owner_can_delete(self):
        self.client.force_login(self.owner)
        self.client.post(f"/applications/{self.app.pk}/delete/")
        self.assertFalse(Application.objects.filter(pk=self.app.pk).exists())

    def test_security_headers(self):
        self.client.force_login(self.owner)
        r = self.client.get("/")
        self.assertIn("frame-ancestors 'none'", r["Content-Security-Policy"])
        self.assertEqual(r["X-Frame-Options"], "DENY")


class HtmxFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.app = make_app(status=Application.Status.DRAFTING, sponsor_deadline=timezone.localdate() + timedelta(days=30))

    def test_modal_form_success_triggers_refresh_then_close(self):
        r = self.client.post(f"/tasks/new/?application={self.app.pk}", {
            "title": "Draft aims", "application": self.app.pk, "category": "writing", "status": "todo", "priority": "medium",
        }, **HX)
        self.assertEqual(r.status_code, 204)
        triggers = json.loads(r["HX-Trigger"])
        self.assertEqual(list(triggers)[-1], "closeModal")  # close last so refresh events bubble first
        self.assertIn("refresh-tasks", triggers)
        self.assertTrue(self.app.tasks.filter(title="Draft aims").exists())

    def test_invalid_modal_form_rerenders_modal(self):
        r = self.client.post("/tasks/new/", {"title": ""}, **HX)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'class="modal')

    def test_task_toggle_returns_row(self):
        task = Task.objects.create(title="X", application=self.app)
        r = self.client.post(f"/tasks/{task.pk}/toggle/", **HX)
        self.assertContains(r, f'id="task-{task.pk}"')
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertIsNotNone(task.completed_at)
        self.client.post(f"/tasks/{task.pk}/toggle/", **HX)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.TODO)
        self.assertIsNone(task.completed_at)

    def test_board_quick_status_and_award_redirect(self):
        r = self.client.post(f"/applications/{self.app.pk}/status/", {"status": "submitted", "quick": "1"}, **HX)
        self.assertEqual(r.status_code, 204)
        r = self.client.post(f"/applications/{self.app.pk}/status/", {"status": "awarded", "quick": "1"}, **HX)
        award = Award.objects.get(application=self.app)
        self.assertEqual(r["HX-Redirect"], reverse("grants:award_edit", args=[award.pk]))

    def test_status_modal_records_scores(self):
        self.app.status = Application.Status.IN_REVIEW
        self.app.save()
        r = self.client.post(f"/applications/{self.app.pk}/status/", {
            "status": "reviewed", "effective_date": "2026-03-01", "impact_score": "25", "percentile": "11",
            "review_outcome": "scored", "note": "Good score",
        }, **HX)
        self.assertEqual(r["HX-Refresh"], "true")
        self.app.refresh_from_db()
        self.assertEqual((self.app.impact_score, self.app.percentile, self.app.review_date), (25, 11, date(2026, 3, 1)))

    def test_detail_sections_render(self):
        for name in ["tasks", "documents", "personnel", "feedback", "activity"]:
            r = self.client.get(f"/applications/{self.app.pk}/section/{name}/", **HX)
            self.assertContains(r, f'id="section-{name}"')
        self.assertEqual(self.client.get(f"/applications/{self.app.pk}/section/bogus/").status_code, 404)

    def test_feedback_with_criteria_and_new_theme(self):
        r = self.client.post(f"/applications/{self.app.pk}/feedback/new/", {
            "source": "reviewer", "reviewer_label": "Reviewer 1", "overall_score": "3",
            "weaknesses": "Needs preliminary data", "new_themes": "Scope creep",
            "criteria-TOTAL_FORMS": "2", "criteria-INITIAL_FORMS": "0", "criteria-MIN_NUM_FORMS": "0", "criteria-MAX_NUM_FORMS": "1000",
            "criteria-0-criterion": "Approach", "criteria-0-score": "4", "criteria-1-criterion": "Significance", "criteria-1-score": "2",
        }, **HX)
        self.assertEqual(r.status_code, 204, r.content[:500])
        fb = self.app.feedback.get()
        self.assertEqual(fb.criteria.count(), 2)
        self.assertEqual(fb.themes.get().name, "Scope creep")

    def test_personnel_quick_add_new_person(self):
        r = self.client.post(f"/applications/{self.app.pk}/personnel/new/", {
            "new_first_name": "Sam", "new_last_name": "Patel", "new_kind": "lab", "role": "postdoc", "person_months": "6",
        }, **HX)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.app.personnel.get().effort_percent, 50)


class DocumentViewTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.editor = make_user("editor", User.Role.EDITOR)
        self.app = make_app()

    def upload(self, name, content, **extra):
        data = {"category": "aims", "application": self.app.pk, "upload": SimpleUploadedFile(name, content)}
        data.update(extra)
        return self.client.post(f"/documents/new/?application={self.app.pk}", data, **HX)

    def test_upload_download_roundtrip(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.upload("aims-final.txt", b"hello aims").status_code, 204)
        doc = Document.objects.get()
        self.assertEqual(doc.title, "aims-final")
        r = self.client.get(f"/documents/{doc.pk}/download/")
        self.assertEqual(b"".join(r), b"hello aims") if hasattr(r, "streaming_content") else self.assertEqual(r.content, b"hello aims")
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")

    def test_rejects_unsafe_types(self):
        self.client.force_login(self.owner)
        r = self.upload("evil.html", b"<script>alert(1)</script>")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "isn&#x27;t allowed")
        self.assertFalse(Document.objects.exists())

    def test_restricted_documents_hidden_from_editors(self):
        self.client.force_login(self.owner)
        self.upload("salary.txt", b"secret", restricted="on")
        doc = Document.objects.get()
        self.assertTrue(doc.restricted)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(f"/documents/{doc.pk}/download/").status_code, 403)
        self.assertNotContains(self.client.get("/documents/"), "salary")
        self.assertNotContains(self.client.get("/search/?q=salary"), "salary.txt")

    def test_search_snippet_is_escaped(self):
        self.client.force_login(self.owner)
        doc = Document.objects.create(title="Notes", application=self.app)
        services.store_upload(doc, SimpleUploadedFile("n.txt", b"<script>alert('x')</script> zebrafinch neurons"))
        r = self.client.get("/search/?q=zebrafinch")
        self.assertContains(r, "Notes")
        self.assertNotContains(r, "<script>alert")
        self.assertContains(r, "<mark>zebrafinch</mark>")


class ReportsAndExportsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        today = timezone.localdate()
        self.app = make_app(status=Application.Status.AWARDED, title="Funded project", major_goals="Goal A",
                            proposed_start=today - timedelta(days=100), proposed_end=today + timedelta(days=1000),
                            requested_total=1000000)
        services.create_award(self.app, self.user)
        self.line = add_person(self.app, pm="2.4")
        make_app(status=Application.Status.SUBMITTED, title="Pending project")

    def test_other_support_formats(self):
        pid = self.line.person.pk
        r = self.client.get(f"/current-pending/?person={pid}")
        self.assertContains(r, "Funded project")
        self.assertContains(r, "Goal A")
        r = self.client.get(f"/current-pending/?person={pid}&format=docx")
        self.assertEqual(r.content[:2], b"PK")
        r = self.client.get(f"/current-pending/?person={pid}&format=csv")
        self.assertIn(b"Funded project", r.content)

    def test_calendar_feed_token(self):
        self.assertEqual(self.client.get("/calendar/feed/wrong.ics").status_code, 404)
        self.client.logout()
        r = self.client.get(f"/calendar/feed/{self.user.calendar_token}.ics")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"BEGIN:VCALENDAR", r.content)
        old = self.user.calendar_token
        self.user.rotate_calendar_token()
        self.assertEqual(self.client.get(f"/calendar/feed/{old}.ics").status_code, 404)

    def test_csv_export_and_pages(self):
        r = self.client.get("/applications/export.csv")
        self.assertIn(b"Funded project", r.content)
        for url in ["/", "/analytics/", "/analytics/?basis=fy&span=all", "/effort/", "/awards/", "/calendar/", f"/awards/{self.app.award.pk}/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_reminder_digest_dry_run(self):
        Task.objects.create(title="Overdue thing", assignee=self.user, due_date=timezone.localdate() - timedelta(days=2))
        out = io.StringIO()
        call_command("send_reminders", "--dry-run", stdout=out)
        self.assertIn("Overdue thing", out.getvalue())


class ImportTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def post_csv(self, text, dry_run):
        data = {"csv_file": SimpleUploadedFile("apps.csv", text.encode())}
        if dry_run:
            data["dry_run"] = "on"
        return self.client.post("/import/", data)

    CSV = (
        "title,funder,mechanism,status,submitted_on,decision_on,requested_total,percentile,award_number,award_start,award_end,tags\n"
        "Old R01,NIH,R01,Not funded,6/5/2023,11/20/2023,$2.5M,28,,,,squid\n"
        "Foundation grant,Brand New Foundation,,Awarded,2024-01-10,2024-05-01,250K,,BNF-1,2024-07-01,2026-06-30,\n"
        ",missing title,,,,,,,,,,\n"
        "Bad date,NIH,R21,Submitted,31/31/2024,,,,,,,\n"
    )

    def test_dry_run_saves_nothing(self):
        r = self.post_csv(self.CSV, dry_run=True)
        self.assertContains(r, "Preview")
        self.assertEqual(Application.objects.count(), 0)
        self.assertFalse(Funder.objects.filter(name="Brand New Foundation").exists())
        self.assertEqual(r.context["ok_count"], 2)
        self.assertEqual(r.context["err_count"], 2)

    def test_import_creates_records(self):
        self.post_csv(self.CSV, dry_run=False)
        old = Application.objects.get(title="Old R01")
        self.assertEqual(old.status, Application.Status.NOT_FUNDED)
        self.assertEqual(old.requested_total, 2500000)
        self.assertEqual(old.submitted_on, date(2023, 6, 5))
        self.assertEqual(old.tags.get().name, "squid")
        fg = Application.objects.get(title="Foundation grant")
        self.assertEqual(fg.funder.name, "Brand New Foundation")
        self.assertEqual(fg.award.award_number, "BNF-1")
        self.assertEqual(fg.award.periods.count(), 2)
        self.assertEqual(fg.requested_total, 250000)


class DemoDataTests(TestCase):
    def test_seed_and_remove(self):
        make_user()
        call_command("seed_demo", stdout=io.StringIO())
        self.assertGreater(Application.objects.count(), 5)
        self.assertTrue(Award.objects.exists())
        self.client.force_login(User.objects.get(username="owner"))
        for app in Application.objects.all():
            self.assertEqual(self.client.get(app.get_absolute_url()).status_code, 200)
        call_command("seed_demo", "--remove", stdout=io.StringIO())
        self.assertEqual(Application.objects.count(), 0)
        self.assertEqual(Opportunity.objects.count(), 0)
