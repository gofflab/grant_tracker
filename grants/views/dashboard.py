from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.shortcuts import render
from django.utils import timezone

from .. import analytics
from ..calendar_events import collect_events
from ..models import Activity, Application, Award, Task
from .common import json_for_chart


def dashboard(request):
    today = timezone.localdate()
    open_statuses = Application.PRE_SUBMISSION + Application.PENDING

    active_awards = list(
        Award.objects.filter(status__in=Award.ACTIVE_STATUSES).select_related("application").prefetch_related("periods")
    )
    current_direct = Decimal(0)
    for a in active_awards:
        p = next((p for p in a.periods.all() if p.start_date <= today <= p.end_date), None)
        if p and p.direct_costs:
            current_direct += p.direct_costs
        elif not a.periods.all() and a.awarded_direct_total and a.start_date and a.effective_end:
            years = max(1, round((a.effective_end - a.start_date).days / 365.25))
            current_direct += a.awarded_direct_total / years

    pending = Application.objects.filter(status__in=Application.PENDING)
    preparing = Application.objects.filter(status__in=Application.PRE_SUBMISSION)
    since = today.replace(year=today.year - 5)
    rate, awarded_n, decided_n = analytics.success_rate(Application.objects.filter(decision_on__gte=since))
    weighted, unweighted = analytics.weighted_pipeline()

    upcoming = [
        e for e in collect_events(today, today + timedelta(days=45))
        if e.kind in ("sponsor", "internal", "loi", "report", "opportunity", "award", "review")
    ][:12]
    my_tasks = list(
        Task.objects.filter(status__in=Task.OPEN, assignee=request.user)
        .select_related("application").order_by("due_date")[:8]
    )
    overdue = Task.objects.filter(status__in=Task.OPEN, due_date__lt=today).count()
    ending = [a for a in active_awards if a.days_remaining is not None and 0 <= a.days_remaining <= 365]
    ending.sort(key=lambda a: a.days_remaining)

    years = list(range(today.year - 1, today.year + 5))
    rw = analytics.runway(years)

    ctx = {
        "kpi": {
            "active_awards": len(active_awards),
            "current_direct": current_direct,
            "pending_count": pending.count(),
            "pending_total": pending.aggregate(s=Sum("requested_total"))["s"],
            "preparing_count": preparing.count(),
            "next_due": preparing.filter(sponsor_deadline__gte=today).order_by("sponsor_deadline").first(),
            "success_rate": rate,
            "awarded_n": awarded_n,
            "decided_n": decided_n,
            "weighted": weighted,
            "unweighted": unweighted,
            "overdue": overdue,
        },
        "upcoming": upcoming,
        "my_tasks": my_tasks,
        "starred": Application.objects.filter(is_starred=True).select_related("funder")[:8],
        "pipeline": analytics.pipeline_counts(),
        "ending": ending[:5],
        "activity": Activity.objects.select_related("application", "actor")[:12],
        "runway_spec": json_for_chart({"type": "bar", "stacked": True, "labels": rw["labels"], "series": rw["series"], "format": "money"}),
        "runway": rw,
        "has_awards": bool(active_awards),
        "is_empty": not Application.objects.exists(),
    }
    return render(request, "grants/dashboard.html", ctx)
