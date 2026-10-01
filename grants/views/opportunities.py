from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import editor_required, owner_required

from ..forms import OpportunityForm
from ..models import Funder, Opportunity, Tag
from .common import done, paginate, render_form


def opportunity_list(request):
    today = timezone.localdate()
    qs = Opportunity.objects.select_related("funder").prefetch_related("tags").annotate(app_count=Count("applications"))
    status = request.GET.get("status", "active")
    if status == "active":
        qs = qs.filter(status__in=Opportunity.ACTIVE_STATUSES)
    elif status in Opportunity.Status.values:
        qs = qs.filter(status=status)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(title__icontains=q) | Q(number__icontains=q) | Q(mechanism__icontains=q) | Q(summary__icontains=q)
            | Q(funder__name__icontains=q) | Q(funder__short_name__icontains=q) | Q(sponsor_unit__icontains=q)
        )
    if request.GET.get("funder"):
        qs = qs.filter(funder_id=request.GET["funder"])
    if request.GET.get("tag"):
        qs = qs.filter(tags__id=request.GET["tag"])
    window = request.GET.get("window")
    if window and window.isdigit():
        qs = qs.filter(deadline__gte=today, deadline__lte=today + timedelta(days=int(window)))
    if request.GET.get("limited"):
        qs = qs.filter(limited_submission=True)
    sort = request.GET.get("sort", "deadline")
    order = {
        "deadline": [F("deadline").asc(nulls_last=True)],
        "fit": [F("fit_score").desc(nulls_last=True), F("deadline").asc(nulls_last=True)],
        "added": ["-created_at"],
        "title": ["title"],
    }.get(sort, [F("deadline").asc(nulls_last=True)])
    qs = qs.order_by(*order).distinct()
    layout = request.GET.get("layout", "cards")
    return render(request, "grants/opportunities/list.html", {
        "page": paginate(request, qs, 60),
        "status": status,
        "statuses": Opportunity.Status.choices,
        "funders": Funder.objects.filter(opportunities__isnull=False).distinct(),
        "tags": Tag.objects.filter(kind=Tag.Kind.TOPIC, opportunities__isnull=False).distinct(),
        "sort": sort,
        "layout": layout,
    })


def opportunity_detail(request, pk):
    opp = get_object_or_404(Opportunity.objects.select_related("funder", "contact"), pk=pk)
    return render(request, "grants/opportunities/detail.html", {
        "opp": opp,
        "applications": opp.applications.select_related("funder").order_by("-created_at"),
        "statuses": Opportunity.Status.choices,
    })


@editor_required
def opportunity_create(request):
    form = OpportunityForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        opp = form.save(commit=False)
        opp.added_by = request.user
        opp.save()
        form.save_m2m()
        form.save_tags(opp)
        messages.success(request, "Opportunity added.")
        return redirect(opp)
    return render(request, "grants/opportunities/form.html", {"form": form, "title": "New opportunity"})


@editor_required
def opportunity_edit(request, pk):
    opp = get_object_or_404(Opportunity, pk=pk)
    form = OpportunityForm(request.POST or None, instance=opp)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect(opp)
    return render(request, "grants/opportunities/form.html", {"form": form, "title": f"Edit: {opp.title}", "opp": opp})


@editor_required
@require_POST
def opportunity_status(request, pk):
    opp = get_object_or_404(Opportunity, pk=pk)
    status = request.POST.get("status")
    if status in Opportunity.Status.values:
        opp.status = status
        opp.save(update_fields=["status", "updated_at"])
    return done(request, f"Marked {opp.get_status_display().lower()}.", refresh=True, redirect_to=opp.get_absolute_url())


@owner_required
def opportunity_delete(request, pk):
    opp = get_object_or_404(Opportunity, pk=pk)
    if request.method == "POST":
        opp.delete()
        messages.success(request, "Opportunity deleted.")
        return done(request, redirect_to="/opportunities/")
    return render_form(request, "_confirm_delete.html", {"object": opp, "danger": True,
                       "warning": "Applications linked to it are kept."}, "Delete opportunity", submit_label="Delete")
