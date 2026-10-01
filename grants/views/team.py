"""Personnel/effort lines and peer-review feedback on an application."""

from django.forms import inlineformset_factory
from django.shortcuts import get_object_or_404

from accounts.permissions import editor_required

from ..forms import CriterionFormSet, PersonnelForm, ReviewFeedbackForm
from ..models import Activity, Application, CriterionScore, Personnel, ReviewFeedback, log_activity
from .common import done, render_form

CRITERIA_SUGGESTIONS = [
    "Overall impact",
    "Factor 1: Importance of the Research",
    "Factor 2: Rigor and Feasibility",
    "Factor 3: Expertise and Resources",
    "Significance", "Investigator(s)", "Innovation", "Approach", "Environment",
    "Intellectual Merit", "Broader Impacts",
    "Feasibility", "Preliminary data", "Candidate", "Career development plan", "Mentor(s)",
]


def default_criteria(app):
    name = f"{app.funder.short_name} {app.funder.name}".lower() if app.funder else ""
    if "nih" in name or "national institutes of health" in name:
        return ["Factor 1: Importance of the Research", "Factor 2: Rigor and Feasibility", "Factor 3: Expertise and Resources"]
    if "nsf" in name or "national science foundation" in name:
        return ["Intellectual Merit", "Broader Impacts"]
    return []


@editor_required
def personnel_create(request, app_pk):
    app = get_object_or_404(Application, pk=app_pk)
    form = PersonnelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        line = form.save(commit=False)
        line.application = app
        line.save()
        log_activity(app, request.user, Activity.Kind.PERSONNEL, f"Added {line.person} ({line.get_role_display()})")
        return done(request, f"Added {line.person}.", redirect_to=app.get_absolute_url(), events=["refresh-personnel", "refresh-activity"])
    return render_form(request, "grants/team/personnel_form.html", {"form": form, "app": app}, "Add person to team")


@editor_required
def personnel_edit(request, pk):
    line = get_object_or_404(Personnel.objects.select_related("application", "person"), pk=pk)
    form = PersonnelForm(request.POST or None, instance=line)
    if request.method == "POST" and form.is_valid():
        form.save()
        return done(request, "Saved.", redirect_to=line.application.get_absolute_url(), events=["refresh-personnel"])
    return render_form(request, "grants/team/personnel_form.html", {"form": form, "app": line.application, "line": line},
                       f"Edit {line.person}")


@editor_required
def personnel_delete(request, pk):
    line = get_object_or_404(Personnel.objects.select_related("application", "person"), pk=pk)
    if request.method == "POST":
        app = line.application
        log_activity(app, request.user, Activity.Kind.PERSONNEL, f"Removed {line.person}")
        line.delete()
        return done(request, "Removed.", redirect_to=app.get_absolute_url(), events=["refresh-personnel", "refresh-activity"])
    return render_form(request, "_confirm_delete.html", {"object": line, "danger": True, "warning": "The person stays in your directory."},
                       "Remove from team", submit_label="Remove")


def _feedback_form(request, app, instance=None):
    form = ReviewFeedbackForm(request.POST or None, instance=instance, application=app)
    if instance is None and request.method != "POST":
        names = default_criteria(app)
        FormSet = inlineformset_factory(
            ReviewFeedback, CriterionScore, form=CriterionFormSet.form, fields=["criterion", "score", "comment"],
            extra=len(names), can_delete=True,
        )
        formset = FormSet(prefix="criteria", initial=[{"criterion": n} for n in names])
    else:
        formset = CriterionFormSet(request.POST or None, instance=instance or ReviewFeedback(), prefix="criteria")
    return form, formset


@editor_required
def feedback_create(request, app_pk):
    app = get_object_or_404(Application.objects.select_related("funder"), pk=app_pk)
    form, formset = _feedback_form(request, app)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        form.instance.application = app
        fb = form.save()
        formset.instance = fb
        formset.save()
        log_activity(app, request.user, Activity.Kind.REVIEW, f"Added review feedback: {fb}")
        return done(request, "Feedback saved.", redirect_to=app.get_absolute_url(), events=["refresh-feedback", "refresh-activity"])
    return render_form(request, "grants/team/feedback_form.html",
                       {"form": form, "formset": formset, "app": app, "suggestions": CRITERIA_SUGGESTIONS},
                       "Add review feedback", size="wide")


@editor_required
def feedback_edit(request, pk):
    fb = get_object_or_404(ReviewFeedback.objects.select_related("application"), pk=pk)
    app = fb.application
    form, formset = _feedback_form(request, app, instance=fb)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        form.save()
        formset.save()
        return done(request, "Feedback saved.", redirect_to=app.get_absolute_url(), events=["refresh-feedback"])
    return render_form(request, "grants/team/feedback_form.html",
                       {"form": form, "formset": formset, "app": app, "suggestions": CRITERIA_SUGGESTIONS, "fb": fb},
                       "Edit review feedback", size="wide")


@editor_required
def feedback_delete(request, pk):
    fb = get_object_or_404(ReviewFeedback.objects.select_related("application"), pk=pk)
    if request.method == "POST":
        app = fb.application
        fb.delete()
        return done(request, "Feedback deleted.", redirect_to=app.get_absolute_url(), events=["refresh-feedback"])
    return render_form(request, "_confirm_delete.html", {"object": fb, "danger": True}, "Delete feedback", submit_label="Delete")
