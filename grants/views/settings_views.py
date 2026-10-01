from django.contrib import messages
from django.db.models import Count, ProtectedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from accounts.permissions import editor_required

from ..forms import ChecklistItemFormSet, ChecklistTemplateForm, FunderForm, PersonForm, TagForm
from ..models import ChecklistTemplate, Funder, Person, Tag
from .common import done, render_form

MODELS = {
    "funders": (Funder, FunderForm, "funder"),
    "people": (Person, PersonForm, "person"),
    "tags": (Tag, TagForm, "tag"),
}


def settings_home(request):
    tab = request.GET.get("tab", "funders")
    ctx = {"tab": tab}
    if tab == "funders":
        ctx["items"] = Funder.objects.annotate(n_apps=Count("applications", distinct=True), n_opps=Count("opportunities", distinct=True))
    elif tab == "people":
        kind = request.GET.get("kind")
        qs = Person.objects.annotate(n=Count("assignments")).select_related("user")
        if kind:
            qs = qs.filter(kind=kind)
        ctx.update(items=qs, kinds=Person.Kind.choices, kind=kind)
    elif tab == "tags":
        ctx["items"] = Tag.objects.annotate(
            n_apps=Count("applications", distinct=True), n_opps=Count("opportunities", distinct=True),
            n_feedback=Count("feedback", distinct=True),
        )
    elif tab == "templates":
        ctx["items"] = ChecklistTemplate.objects.annotate(n=Count("items"))
    return render(request, "grants/settings/home.html", ctx)


@editor_required
def ref_create(request, kind):
    if kind not in MODELS:
        raise Http404
    model, form_class, label = MODELS[kind]
    form = form_class(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        return done(request, f"{label.capitalize()} added.", refresh=True, redirect_to=f"/settings/?tab={kind}")
    return render_form(request, "_generic_form.html", {"form": form}, f"Add {label}")


@editor_required
def ref_edit(request, kind, pk):
    if kind not in MODELS:
        raise Http404
    model, form_class, label = MODELS[kind]
    obj = get_object_or_404(model, pk=pk)
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        return done(request, "Saved.", refresh=True, redirect_to=f"/settings/?tab={kind}")
    return render_form(request, "_generic_form.html", {"form": form}, f"Edit {label}")


@editor_required
def ref_delete(request, kind, pk):
    if kind not in MODELS:
        raise Http404
    model, _, label = MODELS[kind]
    obj = get_object_or_404(model, pk=pk)
    if request.method == "POST":
        try:
            obj.delete()
        except ProtectedError:
            messages.error(request, f"That {label} is still used on applications; remove it there first.")
            return done(request, refresh=True, redirect_to=f"/settings/?tab={kind}")
        return done(request, f"{label.capitalize()} deleted.", refresh=True, redirect_to=f"/settings/?tab={kind}")
    return render_form(request, "_confirm_delete.html", {"object": obj, "danger": True,
                       "warning": "Links from applications and opportunities are cleared."}, f"Delete {label}", submit_label="Delete")


@editor_required
def template_edit(request, pk=None):
    template = get_object_or_404(ChecklistTemplate, pk=pk) if pk else ChecklistTemplate()
    form = ChecklistTemplateForm(request.POST or None, instance=template)
    formset = ChecklistItemFormSet(request.POST or None, instance=template, prefix="items")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        template = form.save()
        formset.instance = template
        formset.save()
        messages.success(request, "Template saved.")
        return redirect("grants:template_edit", pk=template.pk)
    return render(request, "grants/settings/template_form.html", {"form": form, "formset": formset, "template": template})


@editor_required
def template_delete(request, pk):
    template = get_object_or_404(ChecklistTemplate, pk=pk)
    if request.method == "POST":
        template.delete()
        return done(request, "Template deleted.", refresh=True, redirect_to="/settings/?tab=templates")
    return render_form(request, "_confirm_delete.html", {"object": template, "danger": True,
                       "warning": "Tasks already created from it are kept."}, "Delete template", submit_label="Delete")
