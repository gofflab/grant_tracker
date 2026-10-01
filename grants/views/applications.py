from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import editor_required, owner_required

from .. import services
from ..forms import (
    ApplicationForm,
    ApplyChecklistForm,
    CommentForm,
    StatusChangeForm,
)
from ..models import (
    Activity,
    Application,
    ChecklistTemplate,
    Comment,
    Funder,
    Opportunity,
    Tag,
    Task,
    log_activity,
)
from .common import done, is_htmx, paginate, render_form

SORTS = {
    # Open applications by next deadline, then decided ones newest first.
    "smart": ["closed_flag", "open_deadline", F("submitted_on").desc(nulls_last=True), "-updated_at"],
    "deadline": [F("sponsor_deadline").asc(nulls_last=True), "-updated_at"],
    "-deadline": [F("sponsor_deadline").desc(nulls_last=True)],
    "updated": ["-updated_at"],
    "title": ["title"],
    "funder": ["funder__name", "title"],
    "status": ["status", "title"],
    "submitted": [F("submitted_on").desc(nulls_last=True)],
    "amount": [F("requested_total").desc(nulls_last=True)],
}

STATUS_GROUPS = {
    "open": Application.PRE_SUBMISSION + Application.PENDING,
    "preparing": Application.PRE_SUBMISSION,
    "pending": Application.PENDING,
    "awarded": [Application.Status.AWARDED],
    "closed": [s for s in Application.CLOSED if s != Application.Status.AWARDED],
}


def filter_applications(params, qs=None):
    qs = qs if qs is not None else Application.objects.all()
    q = params.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(title__icontains=q) | Q(short_name__icontains=q) | Q(mechanism__icontains=q)
            | Q(sponsor_id__icontains=q) | Q(funder__name__icontains=q) | Q(funder__short_name__icontains=q)
            | Q(review_panel__icontains=q) | Q(sponsor_unit__icontains=q) | Q(abstract__icontains=q)
        )
    group = params.get("group")
    if group in STATUS_GROUPS:
        qs = qs.filter(status__in=STATUS_GROUPS[group])
    statuses = [s for s in params.getlist("status") if s in Application.Status.values]
    if statuses:
        qs = qs.filter(status__in=statuses)
    if params.get("funder"):
        qs = qs.filter(funder_id=params.get("funder"))
    if params.get("mechanism"):
        qs = qs.filter(mechanism__iexact=params.get("mechanism"))
    if params.get("role"):
        qs = qs.filter(role=params.get("role"))
    if params.get("tag"):
        qs = qs.filter(tags__id=params.get("tag"))
    if params.get("year", "").isdigit():
        y = int(params["year"])
        qs = qs.filter(Q(submitted_on__year=y) | Q(submitted_on__isnull=True, sponsor_deadline__year=y))
    if params.get("starred"):
        qs = qs.filter(is_starred=True)
    return qs.distinct()


def application_list(request):
    params = request.GET
    sort = params.get("sort", "smart")
    qs = filter_applications(params, Application.objects.select_related("funder", "award").prefetch_related("tags"))
    qs = qs.annotate(
        closed_flag=Case(When(status__in=Application.CLOSED, then=Value(1)), default=Value(0), output_field=IntegerField()),
        open_deadline=Case(When(status__in=Application.PRE_SUBMISSION, then=F("sponsor_deadline")), default=None),
    ).order_by(*SORTS.get(sort, SORTS["smart"]))
    page = paginate(request, qs)
    years = sorted(
        {d.year for d in Application.objects.exclude(submitted_on=None).values_list("submitted_on", flat=True)}
        | {timezone.localdate().year},
        reverse=True,
    )
    ctx = {
        "page": page,
        "sort": sort,
        "funders": Funder.objects.filter(applications__isnull=False).distinct(),
        "mechanisms": Application.objects.exclude(mechanism="").values_list("mechanism", flat=True).distinct().order_by("mechanism"),
        "tags": Tag.objects.filter(kind=Tag.Kind.TOPIC, applications__isnull=False).distinct(),
        "years": years,
        "roles": Application.Role.choices,
        "groups": [("open", "Open"), ("preparing", "Preparing"), ("pending", "Pending decision"),
                   ("awarded", "Awarded"), ("closed", "Not funded / closed")],
        "statuses": Application.Status.choices,
        "total": qs.count(),
    }
    template = "grants/applications/_table.html" if is_htmx(request) and request.GET.get("partial") else "grants/applications/list.html"
    return render(request, template, ctx)


def application_board(request):
    show_all = request.GET.get("all") == "1"
    since = timezone.localdate().replace(day=1).replace(year=timezone.localdate().year - 1)
    qs = Application.objects.select_related("funder").prefetch_related("tags").annotate(
        open_tasks=Count("tasks", filter=Q(tasks__status__in=Task.OPEN))
    )
    if request.GET.get("funder"):
        qs = qs.filter(funder_id=request.GET["funder"])
    columns = []
    statuses = list(Application.Status) if show_all else (
        Application.PRE_SUBMISSION + Application.PENDING + [Application.Status.AWARDED, Application.Status.NOT_FUNDED]
    )
    for status in statuses:
        items = qs.filter(status=status)
        if status in Application.CLOSED and not show_all:
            items = items.filter(Q(decision_on__gte=since) | Q(decision_on__isnull=True, updated_at__date__gte=since))
        items = items.order_by(F("sponsor_deadline").asc(nulls_last=True), "-updated_at")
        columns.append({"status": status, "label": Application.Status(status).label, "items": list(items)})
    return render(request, "grants/applications/board.html", {
        "columns": columns, "show_all": show_all, "funders": Funder.objects.filter(applications__isnull=False).distinct(),
    })


# ---------------------------------------------------------------------------
# Detail page and its live sections
# ---------------------------------------------------------------------------

SECTIONS = {"tasks", "documents", "personnel", "feedback", "activity"}


def section_context(app, name, user):
    ctx = {"app": app}
    if name == "tasks":
        tasks = list(app.tasks.select_related("assignee"))
        open_tasks = [t for t in tasks if not t.is_done]
        done_tasks = [t for t in tasks if t.is_done]
        ctx.update(
            open_tasks=open_tasks,
            done_tasks=done_tasks,
            task_total=len(tasks),
            task_pct=round(len(done_tasks) * 100 / len(tasks)) if tasks else 0,
            templates=ChecklistTemplate.objects.all(),
        )
    elif name == "documents":
        docs = app.documents.select_related("uploaded_by")
        if not user.is_owner:
            docs = docs.filter(restricted=False)
        ctx["documents"] = docs
    elif name == "personnel":
        ctx["personnel"] = app.personnel.select_related("person")
        ctx["total_pm"] = sum((p.person_months or 0) for p in ctx["personnel"])
    elif name == "feedback":
        ctx["feedback"] = app.feedback.prefetch_related("criteria", "themes").select_related("document")
    elif name == "activity":
        comments = list(app.comments.select_related("author")[:100])
        activities = list(app.activities.select_related("actor")[:200])
        items = [{"kind": "comment", "obj": c, "at": c.created_at} for c in comments]
        items += [{"kind": a.kind, "obj": a, "at": a.created_at} for a in activities]
        items.sort(key=lambda i: i["at"], reverse=True)
        ctx["items"] = items
        ctx["comment_form"] = CommentForm()
    return ctx


def _get_app(pk):
    return get_object_or_404(
        Application.objects.select_related("funder", "opportunity", "parent", "program_officer", "grants_admin"), pk=pk
    )


def application_detail(request, pk):
    app = _get_app(pk)
    award = getattr(app, "award", None)
    ctx = {
        "app": app,
        "award": award,
        "lineage": app.lineage() if (app.parent_id or app.resubmissions.exists()) else [],
        "status_changes": app.status_changes.select_related("changed_by")[:20],
        "statuses": Application.Status.choices,
        "counts": {
            "tasks": app.tasks.filter(status__in=Task.OPEN).count(),
            "documents": app.documents.count() if request.user.is_owner else app.documents.filter(restricted=False).count(),
            "personnel": app.personnel.count(),
            "feedback": app.feedback.count(),
            "comments": app.comments.count(),
        },
    }
    for name in SECTIONS:
        ctx[f"section_{name}"] = section_context(app, name, request.user)
    return render(request, "grants/applications/detail.html", ctx)


def application_section(request, pk, name):
    if name not in SECTIONS:
        raise Http404
    app = _get_app(pk)
    return render(request, f"grants/applications/_section_{name}.html", section_context(app, name, request.user))


# ---------------------------------------------------------------------------
# Create / edit / delete
# ---------------------------------------------------------------------------


def _initial_from_opportunity(opp):
    return {
        "opportunity": opp.pk,
        "title": opp.title,
        "funder": opp.funder_id,
        "sponsor_unit": opp.sponsor_unit,
        "mechanism": opp.mechanism,
        "announcement_number": opp.number,
        "loi_deadline": opp.loi_deadline,
        "internal_deadline": opp.internal_deadline,
        "sponsor_deadline": opp.deadline,
        "duration_years": opp.max_duration_years,
        "program_officer": opp.contact_id,
        "portal_url": opp.url,
        "tags": list(opp.tags.values_list("pk", flat=True)),
    }


@editor_required
def application_create(request):
    initial = {}
    opp_id = request.GET.get("opportunity")
    if opp_id:
        opp = get_object_or_404(Opportunity, pk=opp_id)
        initial = _initial_from_opportunity(opp)
    form = ApplicationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        app = form.save(commit=False)
        app.created_by = request.user
        app.status = form.cleaned_data["initial_status"]
        app.save()
        form.save_m2m()
        form.save_tags(app)
        log_activity(app, request.user, Activity.Kind.CREATED, "Application created")
        if app.opportunity and app.opportunity.status in (Opportunity.Status.WATCHING, Opportunity.Status.PLANNING):
            app.opportunity.status = Opportunity.Status.APPLYING
            app.opportunity.save(update_fields=["status", "updated_at"])
        template_id = request.POST.get("apply_template")
        if template_id:
            template = ChecklistTemplate.objects.filter(pk=template_id).first()
            if template:
                services.apply_checklist(app, template, request.user)
        messages.success(request, "Application created.")
        return redirect(app)
    return render(request, "grants/applications/form.html", {
        "form": form, "title": "New application",
        "templates": ChecklistTemplate.objects.filter(applies_to=ChecklistTemplate.AppliesTo.SUBMISSION),
    })


@editor_required
def application_edit(request, pk):
    app = get_object_or_404(Application, pk=pk)
    form = ApplicationForm(request.POST or None, instance=app)
    if request.method == "POST" and form.is_valid():
        form.save()
        changed = ", ".join(form[f].label.lower() for f in form.changed_data if f in form.fields and f not in ("new_tags", "extra"))
        log_activity(app, request.user, Activity.Kind.EDITED, f"Edited {changed}" if changed else "Edited details")
        messages.success(request, "Saved.")
        return redirect(app)
    return render(request, "grants/applications/form.html", {"form": form, "title": f"Edit: {app.display_title}", "app": app})


@owner_required
def application_delete(request, pk):
    app = get_object_or_404(Application, pk=pk)
    if request.method == "POST":
        title = app.display_title
        app.delete()
        messages.success(request, f"Deleted “{title}”.")
        if is_htmx(request):
            resp = HttpResponse(status=204)
            resp["HX-Redirect"] = reverse("grants:application_list")
            return resp
        return redirect("grants:application_list")
    return render_form(
        request, "_confirm_delete.html",
        {"object": app, "danger": True,
         "warning": "Its tasks, documents, personnel, review feedback, award record and history are deleted too."},
        "Delete application", submit_label="Delete",
    )


# ---------------------------------------------------------------------------
# Status workflow
# ---------------------------------------------------------------------------


@editor_required
def application_status(request, pk):
    app = get_object_or_404(Application, pk=pk)
    if request.method == "POST" and request.POST.get("quick"):
        status = request.POST.get("status")
        if status not in Application.Status.values:
            return HttpResponse(status=400)
        award = services.change_status(app, status, request.user)
        if award:
            messages.success(request, "Congratulations! Confirm the award details.")
            resp = HttpResponse(status=204)
            resp["HX-Redirect"] = reverse("grants:award_edit", args=[award.pk])
            return resp
        return done(request, f"{app.display_title} → {app.get_status_display()}", events=["board-changed"])

    initial = {"status": request.GET.get("to", app.status), "effective_date": timezone.localdate()}
    form = StatusChangeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        status = data["status"]
        when = data.get("effective_date")
        fields = []
        if when and status in Application.PENDING and status == Application.Status.SUBMITTED:
            app.submitted_on = when
            fields.append("submitted_on")
        if when and status in Application.DECIDED:
            app.decision_on = when
            fields.append("decision_on")
        if when and status == Application.Status.REVIEWED and not app.review_date:
            app.review_date = when
            fields.append("review_date")
        for f in ("impact_score", "percentile", "review_outcome"):
            if data.get(f) not in (None, ""):
                setattr(app, f, data[f])
                fields.append(f)
        if fields:
            app.save(update_fields=fields + ["updated_at"])
        award = services.change_status(app, status, request.user, data.get("note", ""))
        if award:
            messages.success(request, "Congratulations! Confirm the award details and reporting schedule.")
            return done(request, redirect_to=reverse("grants:award_edit", args=[award.pk]))
        return done(request, f"Status changed to {app.get_status_display()}.", refresh=True, redirect_to=app.get_absolute_url())
    return render_form(
        request, "grants/applications/status_form.html", {"form": form, "app": app},
        "Change status", submit_label="Update status",
    )


@editor_required
@require_POST
def application_star(request, pk):
    app = get_object_or_404(Application, pk=pk)
    app.is_starred = not app.is_starred
    app.save(update_fields=["is_starred", "updated_at"])
    if is_htmx(request):
        return render(request, "grants/applications/_star.html", {"app": app})
    return redirect(app)


@editor_required
@require_POST
def application_clone(request, pk):
    app = get_object_or_404(Application, pk=pk)
    kind = request.POST.get("kind")
    if kind not in (Application.SubmissionType.RESUBMISSION, Application.SubmissionType.RENEWAL, "copy"):
        return HttpResponse(status=400)
    if kind == "copy":
        new = services.clone_application(app, app.submission_type, request.user)
        new.parent = None
        new.title = f"{app.title} (copy)"[:300]
        new.save(update_fields=["parent", "title"])
    else:
        new = services.clone_application(app, kind, request.user)
    messages.success(request, f"Created “{new.display_title}”. Update the dates and details.")
    url = reverse("grants:application_edit", args=[new.pk])
    if is_htmx(request):
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = url
        return resp
    return redirect(url)


@editor_required
def application_apply_checklist(request, pk):
    app = get_object_or_404(Application, pk=pk)
    form = ApplyChecklistForm(request.POST or None, initial={"template": request.GET.get("template")})
    if request.method == "POST" and form.is_valid():
        created = services.apply_checklist(app, form.cleaned_data["template"], request.user)
        msg = f"Added {len(created)} task{'s' if len(created) != 1 else ''}." if created else "All checklist items already exist."
        return done(request, msg, redirect_to=app.get_absolute_url(), events=["refresh-tasks", "refresh-activity"])
    return render_form(request, "grants/applications/checklist_form.html", {"form": form, "app": app}, "Apply checklist template", submit_label="Add tasks")


def application_history(request, pk):
    app = get_object_or_404(Application, pk=pk)
    records = list(app.history.select_related("history_user").order_by("-history_date")[:200])
    entries = []
    for rec in records:
        prev = rec.prev_record
        changes = []
        if prev:
            delta = rec.diff_against(prev)
            for c in delta.changes:
                field = Application._meta.get_field(c.field)
                changes.append({"field": getattr(field, "verbose_name", c.field), "old": c.old, "new": c.new})
        entries.append({"record": rec, "changes": changes, "created": prev is None})
    return render(request, "grants/applications/history.html", {"app": app, "entries": entries})


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@editor_required
@require_POST
def comment_create(request, pk):
    app = get_object_or_404(Application, pk=pk)
    form = CommentForm(request.POST)
    if form.is_valid() and form.cleaned_data["body"].strip():
        Comment.objects.create(application=app, author=request.user, body=form.cleaned_data["body"].strip())
        return done(request, "Note added.", redirect_to=app.get_absolute_url(), events=["refresh-activity"])
    return done(request, "Write something first.", redirect_to=app.get_absolute_url(), events=[], level="error")


@editor_required
@require_POST
def comment_delete(request, pk):
    comment = get_object_or_404(Comment, pk=pk)
    if comment.author_id != request.user.pk and not request.user.is_owner:
        raise PermissionDenied("You can only delete your own notes.")
    app = comment.application
    comment.delete()
    return done(request, "Note deleted.", redirect_to=app.get_absolute_url(), events=["refresh-activity"])
