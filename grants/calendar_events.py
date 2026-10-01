"""One place that turns deadlines, tasks and award dates into calendar events."""

from dataclasses import dataclass
from datetime import date

from django.db.models import Q

from .models import Application, Award, Opportunity, Task

KIND_LABELS = {
    "sponsor": "Sponsor deadline",
    "internal": "Internal deadline",
    "loi": "LOI / pre-proposal",
    "review": "Review / council",
    "task": "Task",
    "report": "Report / compliance",
    "award": "Award end",
    "opportunity": "Opportunity deadline",
}


@dataclass
class Event:
    date: date
    kind: str
    title: str
    subtitle: str
    url: str
    uid: str
    done: bool = False

    @property
    def kind_label(self):
        return KIND_LABELS.get(self.kind, self.kind)


def collect_events(start, end, include_done_tasks=False):
    events = []
    apps = Application.objects.select_related("funder").exclude(
        status__in=[Application.Status.NOT_PURSUED, Application.Status.WITHDRAWN]
    )
    for app in apps.filter(
        Q(sponsor_deadline__range=(start, end)) | Q(internal_deadline__range=(start, end))
        | Q(loi_deadline__range=(start, end)) | Q(review_date__range=(start, end)) | Q(council_date__range=(start, end))
    ):
        funder = app.funder.display_name if app.funder else ""
        sub = " · ".join(x for x in [funder, app.mechanism] if x)
        url = app.get_absolute_url()
        for field, kind, label in (
            ("loi_deadline", "loi", "LOI"),
            ("internal_deadline", "internal", "Internal"),
            ("sponsor_deadline", "sponsor", "Due"),
        ):
            d = getattr(app, field)
            if d and start <= d <= end:
                events.append(Event(d, kind, f"{label}: {app.display_title}", sub, url, f"app-{app.pk}-{kind}",
                                    done=app.status not in Application.PRE_SUBMISSION))
        if app.status in Application.PENDING:
            if app.review_date and start <= app.review_date <= end:
                events.append(Event(app.review_date, "review", f"Review: {app.display_title}",
                                    app.review_panel or sub, url, f"app-{app.pk}-review"))
            if app.council_date and start <= app.council_date <= end:
                events.append(Event(app.council_date, "review", f"Council: {app.display_title}", sub, url, f"app-{app.pk}-council"))

    for opp in Opportunity.objects.select_related("funder").filter(
        status__in=[Opportunity.Status.WATCHING, Opportunity.Status.PLANNING], applications__isnull=True
    ).filter(Q(deadline__range=(start, end)) | Q(loi_deadline__range=(start, end)) | Q(internal_deadline__range=(start, end))):
        sub = " · ".join(x for x in [opp.funder.display_name if opp.funder else "", opp.mechanism] if x)
        for field, label in (("loi_deadline", "LOI"), ("internal_deadline", "Internal"), ("deadline", "Due")):
            d = getattr(opp, field)
            if d and start <= d <= end:
                events.append(Event(d, "opportunity", f"{label}: {opp.title}", sub, opp.get_absolute_url(), f"opp-{opp.pk}-{field}"))

    tasks = Task.objects.select_related("application").filter(due_date__range=(start, end))
    if not include_done_tasks:
        tasks = tasks.filter(status__in=Task.OPEN)
    for t in tasks:
        kind = "report" if t.category in Task.POST_AWARD else "task"
        url = t.application.get_absolute_url() if t.application else "/tasks/"
        events.append(Event(t.due_date, kind, t.title, t.application.display_title if t.application else "",
                            url, f"task-{t.pk}", done=t.is_done))

    for award in Award.objects.select_related("application").filter(status__in=Award.ACTIVE_STATUSES):
        end_date = award.effective_end
        if end_date and start <= end_date <= end:
            events.append(Event(end_date, "award", f"Project ends: {award.application.display_title}",
                                award.award_number, award.get_absolute_url(), f"award-{award.pk}-end"))

    events.sort(key=lambda e: (e.date, list(KIND_LABELS).index(e.kind) if e.kind in KIND_LABELS else 99, e.title))
    return events
