from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from grants import analytics, services
from grants.models import Application, Award, Funder, Task

from .helpers import make_app, make_user, nih


def past_award(title, start, total, funder=None, **kw):
    """An award entered from the award side: award dates and money, but no submitted date or deadline."""
    app = make_app(title=title, status=Application.Status.AWARDED, funder=funder or nih(), **kw)
    Award.objects.create(application=app, start_date=start, end_date=start.replace(year=start.year + 5),
                         awarded_total=Decimal(total), status=Award.Status.CLOSED)
    return app


class AnalyticsMathTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user())
        self.year = timezone.localdate().year
        for y in (self.year - 3, self.year - 2):
            make_app(title=f"Not funded {y}", status=Application.Status.NOT_FUNDED, sponsor_deadline=date(y, 6, 5))
        past_award("Old R01", date(self.year - 2, 7, 1), "2000000")
        self.foundation = Funder.objects.get_or_create(name="Example Foundation", defaults={"funder_type": "foundation"})[0]
        past_award("Foundation grant", date(self.year - 2, 9, 1), "500000", funder=self.foundation)

    def get(self, **params):
        return self.client.get(reverse("grants:analytics"), {"span": "5", **params}).context

    def test_award_without_submission_date_counts_by_award_date(self):
        ctx = self.get(funder=nih().pk)
        self.assertEqual((ctx["tiles"]["awarded"], ctx["tiles"]["decided"]), (1, 3))
        self.assertEqual([a.title for a, _ in ctx["placed_late"]], ["Old R01"])
        self.assertContains(self.client.get(reverse("grants:analytics"), {"span": "5"}), "no submitted date or deadline")

    def test_funding_chart_and_total_follow_the_funder_filter(self):
        nih_ctx = self.get(funder=nih().pk)
        self.assertEqual(sum(nih_ctx["raw"]["awarded"]["series"][0]["data"]), 2000000)
        self.assertEqual(nih_ctx["tiles"]["total_awarded"], 2000000)
        all_ctx = self.get()
        self.assertEqual(all_ctx["tiles"]["total_awarded"], 2500000)
        self.assertEqual(sum(all_ctx["raw"]["awarded"]["series"][0]["data"]), all_ctx["tiles"]["total_awarded"])

    def test_award_record_left_on_a_non_awarded_application_is_not_counted(self):
        moved = past_award("Moved to not funded", date(self.year - 1, 7, 1), "900000")
        services.change_status(moved, Application.Status.NOT_FUNDED)
        ctx = self.get(funder=nih().pk)
        self.assertEqual(ctx["tiles"]["total_awarded"], 2000000)
        self.assertEqual(ctx["tiles"]["awarded"], 1)

    def test_application_with_no_dates_is_listed_not_counted(self):
        make_app(title="Mystery award", status=Application.Status.AWARDED)
        ctx = self.get(funder=nih().pk)
        self.assertEqual([a.title for a in ctx["undated"]], ["Mystery award"])
        self.assertEqual(ctx["tiles"]["awarded"], 1)

    def test_placement_prefers_submission_then_deadline_then_decision_then_award(self):
        app = past_award("Placed", date(2020, 7, 1), "1")
        self.assertEqual(analytics.placement_date(app), date(2020, 7, 1))
        app.decision_on = date(2020, 3, 1)
        self.assertEqual(analytics.placement_date(app), date(2020, 3, 1))
        app.sponsor_deadline = date(2019, 6, 5)
        self.assertEqual(analytics.placement_date(app), date(2019, 6, 5))
        app.submitted_on = date(2019, 6, 1)
        self.assertEqual(analytics.placement_date(app), date(2019, 6, 1))

    def test_dashboard_success_rate_counts_award_without_decision_date(self):
        kpi = self.client.get(reverse("grants:dashboard")).context["kpi"]
        # Both awards count by their start dates; the not-funded entries have no decision date and are left out.
        self.assertEqual((kpi["awarded_n"], kpi["decided_n"]), (2, 2))


class CreateAsAwardedTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def post(self, **extra):
        data = {"title": "Past R01", "funder": nih().pk, "initial_status": "awarded", "submission_type": "new",
                "priority": "medium", "role": "pi", **extra}
        return self.client.post(reverse("grants:application_create"), data)

    def test_creates_closed_award_with_budget_years_and_no_tasks(self):
        r = self.post(proposed_start="2018-07-01", proposed_end="2023-06-30", requested_total="3000000",
                      decision_on="2018-05-15")
        app = Application.objects.get(title="Past R01")
        award = app.award
        self.assertRedirects(r, reverse("grants:award_edit", args=[award.pk]), fetch_redirect_response=False)
        self.assertEqual(award.status, Award.Status.CLOSED)
        self.assertEqual(award.notice_date, date(2018, 5, 15))
        self.assertEqual(award.periods.count(), 5)
        self.assertFalse(Task.objects.filter(application=app).exists())
        # The setup form doesn't offer to generate a reporting schedule for a closed award.
        form = self.client.get(reverse("grants:award_edit", args=[award.pk])).context["form"]
        self.assertFalse(form.initial["generate_schedule"])

    def test_ongoing_award_is_offered_a_reporting_schedule(self):
        today = timezone.localdate()
        self.post(proposed_start=str(today - timedelta(days=400)), proposed_end=str(today + timedelta(days=1400)))
        award = Application.objects.get(title="Past R01").award
        self.assertEqual(award.status, Award.Status.ACTIVE)
        self.assertTrue(award.periods.exists())
        form = self.client.get(reverse("grants:award_edit", args=[award.pk])).context["form"]
        self.assertTrue(form.initial["generate_schedule"])
        # Once the schedule exists, editing the award doesn't offer it again.
        services.generate_reporting_tasks(award, self.user)
        form = self.client.get(reverse("grants:award_edit", args=[award.pk])).context["form"]
        self.assertFalse(form.initial["generate_schedule"])

    def test_ongoing_award_without_dates_stays_active(self):
        self.post()
        award = Application.objects.get(title="Past R01").award
        self.assertEqual(award.status, Award.Status.ACTIVE)
        self.assertIsNone(award.notice_date)

    def test_other_statuses_create_no_award(self):
        self.post(initial_status="not_funded")
        self.assertFalse(Award.objects.exists())


class LateStatusDateTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_past_deadline_is_used_as_submitted_date(self):
        app = make_app(status=Application.Status.DRAFTING, sponsor_deadline=date(2024, 2, 5))
        services.change_status(app, Application.Status.AWARDED, self.user)
        app.refresh_from_db()
        self.assertEqual(app.submitted_on, date(2024, 2, 5))

    def test_future_deadline_means_submitted_today(self):
        today = timezone.localdate()
        app = make_app(status=Application.Status.ROUTING, sponsor_deadline=today + timedelta(days=10))
        services.change_status(app, Application.Status.SUBMITTED, self.user)
        app.refresh_from_db()
        self.assertEqual(app.submitted_on, today)


class LateAwardNoticeDateTests(TestCase):
    def test_status_dialog_date_becomes_the_notice_date(self):
        self.client.force_login(make_user())
        app = make_app(status=Application.Status.PENDING_AWARD)
        self.client.post(reverse("grants:application_status", args=[app.pk]),
                         {"status": "awarded", "effective_date": "2022-05-01"}, HTTP_HX_REQUEST="true")
        app.refresh_from_db()
        self.assertEqual(app.award.notice_date, date(2022, 5, 1))
        self.assertEqual(analytics.awarded_by_year([app], [2022, 2026], "cy"), [0.0, 0.0])  # no amount entered
        app.award.awarded_total = Decimal("100")
        app.award.save()
        self.assertEqual(analytics.awarded_by_year([app], [2022, 2026], "cy"), [100.0, 0.0])

    def test_live_award_without_decision_date_is_noticed_today(self):
        app = make_app(status=Application.Status.PENDING_AWARD)
        services.change_status(app, Application.Status.AWARDED)
        self.assertEqual(app.award.notice_date, timezone.localdate())


class DashboardLeapDayTests(TestCase):
    def test_dashboard_renders_on_february_29(self):
        self.client.force_login(make_user())
        past_award("Old R01", date(2023, 3, 1), "1000")
        with mock.patch("django.utils.timezone.localdate", return_value=date(2028, 2, 29)):
            r = self.client.get(reverse("grants:dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["kpi"]["awarded_n"], 1)


class BoardOutcomeWindowTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user())
        self.old = make_app(title="Old award", status=Application.Status.AWARDED, decision_on=date(2019, 5, 1))

    def awarded_column(self, **params):
        columns = self.client.get(reverse("grants:application_board"), params).context["columns"]
        return next(c for c in columns if c["status"] == Application.Status.AWARDED)

    def test_older_outcomes_are_hidden_and_labelled(self):
        col = self.awarded_column()
        self.assertTrue(col["recent_only"])
        self.assertNotIn(self.old, col["items"])
        self.assertContains(self.client.get(reverse("grants:application_board")), "Show everything")

    def test_show_everything_includes_them(self):
        col = self.awarded_column(all="1")
        self.assertFalse(col["recent_only"])
        self.assertIn(self.old, col["items"])
