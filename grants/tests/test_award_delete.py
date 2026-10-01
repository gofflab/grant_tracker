"""Deleting an award created by mistake, and the Ended award status."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from grants import services
from grants.effort import line_bucket
from grants.models import Activity, Application, Award, BudgetPeriod, Task

from .helpers import add_person, make_app, make_user


def awarded_app(user, from_status=Application.Status.IN_REVIEW, **kw):
    app = make_app(
        status=from_status, proposed_start=date(2026, 7, 1), proposed_end=date(2031, 6, 30),
        requested_total=Decimal("3000000"), requested_direct_total=Decimal("2000000"), **kw,
    )
    award = services.change_status(app, Application.Status.AWARDED, user)
    app.refresh_from_db()
    return app, award


class DeleteAwardServiceTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_generated_tasks_cover_reporting_and_setup_only(self):
        app, award = awarded_app(self.user)
        mine = Task.objects.create(application=app, title="Order a new microscope", category=Task.Category.OTHER)
        generated = services.award_generated_tasks(award)
        self.assertTrue(generated.filter(auto_key__startswith=f"award:{award.pk}:").exists())
        self.assertTrue(generated.filter(title="Review Notice of Award terms and conditions").exists())
        self.assertNotIn(mine, generated)

    def test_delete_reverts_status_and_removes_award_data(self):
        app, award = awarded_app(self.user)
        self.assertEqual(services.status_before_award(app), Application.Status.IN_REVIEW)
        mine = Task.objects.create(application=app, title="Order a new microscope")
        generated = services.award_generated_tasks(award).count()

        summary = services.delete_award(award, self.user, Application.Status.IN_REVIEW, True, "dragged by mistake")

        app.refresh_from_db()
        self.assertFalse(Award.objects.filter(pk=award.pk).exists())
        self.assertFalse(BudgetPeriod.objects.exists())
        self.assertEqual(summary, {"periods": 5, "tasks": generated, "status": "Under review"})
        self.assertEqual(app.status, Application.Status.IN_REVIEW)
        self.assertIsNone(app.decision_on)  # a pending application has no decision yet
        self.assertEqual(list(app.tasks.all()), [mine])
        self.assertEqual(app.status_changes.first().note, "Award record deleted: dragged by mistake")
        self.assertTrue(Activity.objects.filter(application=app, kind=Activity.Kind.AWARD, description__startswith="Deleted").exists())

    def test_keep_tasks_and_keep_awarded(self):
        app, award = awarded_app(self.user)
        before = app.tasks.count()
        summary = services.delete_award(award, self.user, Application.Status.AWARDED, delete_tasks=False)
        app.refresh_from_db()
        self.assertEqual(app.status, Application.Status.AWARDED)
        self.assertEqual(app.tasks.count(), before)
        self.assertIsNone(summary["status"])
        self.assertFalse(hasattr(app, "award"))

    def test_back_to_preparation_clears_submitted_date(self):
        app, award = awarded_app(self.user, from_status=Application.Status.DRAFTING)
        self.assertIsNotNone(app.submitted_on)
        services.delete_award(award, self.user, Application.Status.DRAFTING)
        app.refresh_from_db()
        self.assertIsNone(app.submitted_on)
        self.assertIsNone(app.decision_on)

    def test_not_funded_keeps_decision_date(self):
        app, award = awarded_app(self.user)
        services.delete_award(award, self.user, Application.Status.NOT_FUNDED)
        app.refresh_from_db()
        self.assertEqual(app.decision_on, timezone.localdate())

    def test_status_before_award_without_history(self):
        app = make_app(status=Application.Status.AWARDED)
        self.assertEqual(services.status_before_award(app), Application.Status.PENDING_AWARD)


class DeleteAwardViewTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.client.force_login(self.owner)
        self.app, self.award = awarded_app(self.owner)
        self.url = f"/awards/{self.award.pk}/delete/"

    def test_confirmation_page_lists_what_is_deleted_and_kept(self):
        r = self.client.get(self.url)
        self.assertContains(r, "permanently deletes the award record")
        self.assertContains(r, "5 budget years")
        self.assertContains(r, "Tasks you added yourself")
        self.assertContains(r, 'value="in_review" selected')  # defaults to the status before Awarded
        self.assertNotContains(r, "This award has real data")

    def test_real_data_warning(self):
        self.award.award_number = "R01HD123456"
        self.award.save()
        BudgetPeriod.objects.filter(award=self.award, number=2).update(status="awarded", spent_to_date=Decimal("1000"))
        r = self.client.get(self.url)
        self.assertContains(r, "This award has real data: an award number, a Notice of Award recorded after year 1, spending recorded")
        self.assertContains(r, "<strong>Ended</strong>")

    def test_requires_typed_confirmation(self):
        r = self.client.post(self.url, {"move_to": "in_review", "delete_tasks": "on", "confirm": "yes"})
        self.assertContains(r, "Type DELETE to confirm.")
        self.assertTrue(Award.objects.filter(pk=self.award.pk).exists())

    def test_delete_redirects_to_application(self):
        r = self.client.post(self.url, {"move_to": "in_review", "delete_tasks": "on", "confirm": "delete"}, follow=True)
        self.assertRedirects(r, self.app.get_absolute_url())
        self.assertContains(r, "Award deleted, with 5 budget years and")
        self.assertContains(r, "Application moved to Under review.")
        self.assertFalse(Award.objects.exists())

    def test_htmx_delete_uses_hx_redirect(self):
        r = self.client.post(self.url, {"move_to": "awarded", "confirm": "DELETE"}, HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r["HX-Redirect"], self.app.get_absolute_url())
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, Application.Status.AWARDED)
        self.assertContains(self.client.get(self.app.get_absolute_url()), "there's no award record yet")

    def test_only_owners_can_delete(self):
        for role in (User.Role.EDITOR, User.Role.VIEWER):
            self.client.force_login(make_user(role, role))
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.post(self.url, {"move_to": "in_review", "confirm": "DELETE"}).status_code, 403)
        self.assertTrue(Award.objects.filter(pk=self.award.pk).exists())

    def test_leftover_award_is_flagged_on_application(self):
        services.change_status(self.app, Application.Status.NOT_FUNDED, self.owner)
        r = self.client.get(self.app.get_absolute_url())
        self.assertContains(r, "still has an award record")
        self.assertContains(r, self.url)


class EndedStatusTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_ended_awards_are_not_active(self):
        app, award = awarded_app(self.user)
        line = add_person(app)
        self.assertEqual(line_bucket(line, date(2027, 1, 1)), "active")
        award.status = Award.Status.ENDED
        award.save()
        self.assertFalse(award.is_active)
        line = type(line).objects.select_related("application__award").get(pk=line.pk)
        self.assertIsNone(line_bucket(line, date(2027, 1, 1)))  # no longer counted as committed effort
        r = self.client.get("/awards/?view=closed")
        self.assertContains(r, "Ended &amp; closed (1)")

    def test_mark_ended_from_award_page(self):
        app, award = awarded_app(self.user)
        Award.objects.filter(pk=award.pk).update(end_date=timezone.localdate() - timedelta(days=10))
        r = self.client.get(award.get_absolute_url())
        self.assertContains(r, "The project period ended")
        r = self.client.post(f"/awards/{award.pk}/status/", {"status": "ended"}, HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 204)
        award.refresh_from_db()
        self.assertEqual(award.status, Award.Status.ENDED)
        self.assertNotContains(self.client.get(award.get_absolute_url()), "The project period ended")
        self.assertTrue(Activity.objects.filter(application=app, description="Award marked ended").exists())

    def test_viewers_cannot_change_status(self):
        _, award = awarded_app(self.user)
        self.client.force_login(make_user("viewer", User.Role.VIEWER))
        self.assertEqual(self.client.post(f"/awards/{award.pk}/status/", {"status": "ended"}).status_code, 403)
