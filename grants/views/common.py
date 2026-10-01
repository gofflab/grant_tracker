import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def safe_next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return fallback


def done(request, message=None, redirect_to=None, events=None, refresh=False, level="success"):
    """Finish a successful action.

    HTMX requests get 204 + HX-Trigger events (toast, closeModal, section refresh) so the page
    updates in place. Plain requests get a redirect with a flash message.
    """
    if is_htmx(request):
        response = HttpResponse(status=204)
        if refresh or (redirect_to and events is None):
            if message:
                messages.add_message(request, messages.SUCCESS if level == "success" else messages.ERROR, message)
            if refresh:
                response["HX-Refresh"] = "true"
            else:
                response["HX-Redirect"] = redirect_to
            return response
        triggers = {event: True for event in events or ()}
        if message:
            triggers["toast"] = {"message": message, "level": level}
        triggers["closeModal"] = True
        response["HX-Trigger"] = json.dumps(triggers)
        return response
    if message:
        messages.success(request, message)
    return redirect(safe_next(request, redirect_to or request.META.get("HTTP_REFERER") or "/"))


def render_form(request, template, context, title, submit_label="Save", size="", multipart=False):
    """Render a form either inside the modal (HTMX) or as a standalone page."""
    context = {
        **context,
        "base_template": "_modal_base.html" if is_htmx(request) else "_page_form_base.html",
        "form_title": title,
        "submit_label": submit_label,
        "modal_size": size,
        "multipart": multipart,
        "next": request.GET.get("next") or request.POST.get("next") or "",
        "cancel_url": safe_next(request, request.META.get("HTTP_REFERER") or "/"),
    }
    return render(request, template, context)


def paginate(request, queryset, per_page=50):
    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get("page"))


def json_for_chart(spec):
    return json.dumps(spec, default=float)
