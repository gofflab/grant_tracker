from datetime import date

from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import editor_required

from .. import services
from ..forms import AwardForm, BudgetPeriodForm
from ..models import Activity, Application, Award, BudgetPeriod, Task, log_activity
from .common import done, render_form


def _gantt(awards, pending=()):
    """Position bars for awards (and optional pending applications) on a shared time axis."""
    today = timezone.localdate()
    spans = []
    for a in awards:
        if a.start_date and a.effective_end:
            spans.append((a.start_date, a.effective_end))
    for app in pending:
        if app.proposed_start and app.proposed_end:
            spans.append((app.proposed_start, app.proposed_end))
    if not spans:
        return None
    lo = min(min(s for s, _ in spans), date(today.year, 1, 1))
    hi = max(max(e for _, e in spans), date(today.year, 12, 31))
    lo = date(lo.year, 1, 1)
    hi = date(hi.year, 12, 31)
    total = (hi - lo).days or 1

    def pct(d):
        return round((d - lo).days * 100 / total, 2)

    rows = []
    for a in awards:
        if not (a.start_date and a.effective_end):
            continue
        row = {"label": a.application.display_title, "url": a.get_absolute_url(), "kind": "award", "obj": a,
               "left": pct(a.start_date), "width": max(0.5, pct(a.end_date or a.effective_end) - pct(a.start_date))}
        if a.nce_end_date and a.end_date:
            row["nce_left"] = pct(a.end_date)
            row["nce_width"] = max(0.5, pct(a.nce_end_date) - pct(a.end_date))
        rows.append(row)
    for app in pending:
        if app.proposed_start and app.proposed_end:
            rows.append({"label": app.display_title, "url": app.get_absolute_url(), "kind": "pending", "obj": app,
                         "left": pct(app.proposed_start), "width": max(0.5, pct(app.proposed_end) - pct(app.proposed_start))})
    ticks = [{"label": y, "left": pct(date(y, 1, 1))} for y in range(lo.year, hi.year + 1)]
    return {"rows": rows, "ticks": ticks, "today": pct(today) if lo <= today <= hi else None}


def award_list(request):
    today = timezone.localdate()
    view = request.GET.get("view", "active")
    qs = Award.objects.select_related("application", "application__funder").prefetch_related("periods")
    active = [a for a in qs if a.is_active]
    closed = [a for a in qs if not a.is_active]
    awards = active if view == "active" else closed if view == "closed" else list(qs)
    awards.sort(key=lambda a: (a.effective_end or date.max))

    current_direct = 0
    for a in active:
        p = next((p for p in a.periods.all() if p.start_date <= today <= p.end_date), None)
        if p and p.direct_costs:
            current_direct += p.direct_costs
    ending_soon = [a for a in active if a.days_remaining is not None and a.days_remaining <= 365]

    pending = []
    if request.GET.get("pending") == "1":
        pending = list(Application.objects.filter(status__in=Application.PENDING).exclude(proposed_start=None))
    return render(request, "grants/awards/list.html", {
        "awards": awards,
        "view": view,
        "active_count": len(active),
        "closed_count": len(closed),
        "current_direct": current_direct,
        "active_total": sum((a.awarded_total or 0) for a in active),
        "ending_soon": ending_soon,
        "gantt": _gantt(awards if view != "closed" else closed, pending),
        "show_pending": bool(pending) or request.GET.get("pending") == "1",
    })


def award_detail(request, pk):
    award = get_object_or_404(Award.objects.select_related("application", "application__funder", "grants_specialist"), pk=pk)
    app = award.application
    periods = list(award.periods.all())
    reporting = app.tasks.filter(
        Q(category__in=Task.POST_AWARD) | Q(auto_key__startswith=f"award:{award.pk}:") | Q(created_at__gte=award.created_at)
    ).select_related("assignee")
    open_reports = [t for t in reporting if not t.is_done]
    done_reports = [t for t in reporting if t.is_done]
    totals = {
        "direct": sum((p.direct_costs or 0) for p in periods),
        "indirect": sum((p.indirect_costs or 0) for p in periods),
        "spent": sum((p.spent_to_date or 0) for p in periods),
    }
    totals["total"] = totals["direct"] + totals["indirect"]
    docs = app.documents.filter(category__in=["noa", "progress", "financial", "jit", "correspondence"])
    if not request.user.is_owner:
        docs = docs.filter(restricted=False)
    return render(request, "grants/awards/detail.html", {
        "award": award,
        "app": app,
        "periods": periods,
        "totals": totals,
        "open_reports": open_reports,
        "done_reports": done_reports,
        "personnel": app.personnel.select_related("person"),
        "documents": docs,
        "current": next((p for p in periods if p.is_current), None),
    })


@editor_required
def award_edit(request, pk):
    award = get_object_or_404(Award.objects.select_related("application"), pk=pk)
    first_time = not award.periods.exists()
    form = AwardForm(request.POST or None, instance=award, initial={"generate_schedule": first_time})
    if request.method == "POST" and form.is_valid():
        award = form.save()
        log_activity(award.application, request.user, Activity.Kind.AWARD, "Award details updated")
        if form.cleaned_data.get("generate_schedule"):
            created_periods = services.generate_budget_periods(award, replace=False)
            c, u, r = services.generate_reporting_tasks(award, request.user)
            messages.success(request, f"Award saved. Budget years: {len(created_periods)}. Reporting tasks: {c} added, {u} rescheduled.")
        else:
            messages.success(request, "Award saved.")
        return redirect(award)
    return render(request, "grants/awards/form.html", {"form": form, "award": award, "app": award.application, "first_time": first_time})


@editor_required
@require_POST
def award_create(request, app_pk):
    app = get_object_or_404(Application, pk=app_pk)
    if hasattr(app, "award"):
        return redirect(app.award)
    if app.status != Application.Status.AWARDED:
        services.change_status(app, Application.Status.AWARDED, request.user, "Award record added")
        app.refresh_from_db()
    award = getattr(app, "award", None) or services.create_award(app, request.user)
    return redirect("grants:award_edit", pk=award.pk)


@editor_required
@require_POST
def award_regenerate(request, pk):
    award = get_object_or_404(Award, pk=pk)
    if not award.periods.exists():
        services.generate_budget_periods(award)
    c, u, r = services.generate_reporting_tasks(award, request.user)
    return done(request, f"Reporting schedule: {c} added, {u} rescheduled, {r} removed.", refresh=True, redirect_to=award.get_absolute_url())


@editor_required
def period_create(request, award_pk):
    award = get_object_or_404(Award, pk=award_pk)
    last = award.periods.order_by("-number").first()
    initial = {"number": (last.number + 1) if last else 1}
    if last:
        from datetime import timedelta

        from dateutil.relativedelta import relativedelta

        initial["start_date"] = last.end_date + timedelta(days=1)
        initial["end_date"] = last.end_date + relativedelta(years=1)
    form = BudgetPeriodForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        period = form.save(commit=False)
        period.award = award
        if BudgetPeriod.objects.filter(award=award, number=period.number).exists():
            form.add_error("number", "That budget year already exists.")
        else:
            period.save()
            return done(request, "Budget year added.", refresh=True, redirect_to=award.get_absolute_url())
    return render_form(request, "_generic_form.html", {"form": form}, "Add budget year")


@editor_required
def period_edit(request, pk):
    period = get_object_or_404(BudgetPeriod.objects.select_related("award"), pk=pk)
    form = BudgetPeriodForm(request.POST or None, instance=period)
    if request.method == "POST" and form.is_valid():
        form.save()
        return done(request, f"Year {period.number} saved.", refresh=True, redirect_to=period.award.get_absolute_url())
    return render_form(request, "_generic_form.html", {"form": form}, f"Budget year {period.number}")


@editor_required
def period_delete(request, pk):
    period = get_object_or_404(BudgetPeriod.objects.select_related("award"), pk=pk)
    if request.method == "POST":
        award = period.award
        period.delete()
        return done(request, "Budget year deleted.", refresh=True, redirect_to=award.get_absolute_url())
    return render_form(request, "_confirm_delete.html", {"object": period, "danger": True}, "Delete budget year", submit_label="Delete")
