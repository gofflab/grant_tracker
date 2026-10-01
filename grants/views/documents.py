from urllib.parse import quote

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.permissions import editor_required

from .. import services
from ..forms import DocumentForm
from ..models import Activity, Application, Document, DocumentBlob, log_activity
from .common import done, paginate, render_form


def visible_documents(user):
    qs = Document.objects.all()
    if not user.is_owner:
        qs = qs.filter(restricted=False)
    return qs


def document_list(request):
    qs = visible_documents(request.user).select_related("application", "application__funder", "uploaded_by")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(title__icontains=q) | Q(filename__icontains=q) | Q(notes__icontains=q)
            | Q(extracted_text__icontains=q) | Q(application__title__icontains=q)
        )
    if request.GET.get("category"):
        qs = qs.filter(category=request.GET["category"])
    if request.GET.get("scope") == "library":
        qs = qs.filter(application__isnull=True)
    if request.GET.get("final"):
        qs = qs.filter(is_final=True)
    qs = qs.order_by("-created_at")
    return render(request, "grants/documents/list.html", {
        "page": paginate(request, qs), "categories": Document.Category.choices, "q": q,
    })


@editor_required
def document_create(request):
    app = None
    initial = {}
    if request.GET.get("application"):
        app = get_object_or_404(Application, pk=request.GET["application"])
        initial["application"] = app.pk
    if request.GET.get("category"):
        initial["category"] = request.GET["category"]
    form = DocumentForm(request.POST or None, request.FILES or None, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        doc = form.save(commit=False)
        doc.title = form.cleaned_data["title"]
        doc.uploaded_by = request.user
        doc.save()
        if form.cleaned_data.get("upload"):
            services.store_upload(doc, form.cleaned_data["upload"])
        if doc.application:
            log_activity(doc.application, request.user, Activity.Kind.DOCUMENT,
                         f"Added {doc.get_category_display().lower()}: “{doc.title}”")
        return done(request, "Document saved.", redirect_to=doc.application.get_absolute_url() if doc.application else None,
                    events=["refresh-documents", "refresh-activity"])
    return render_form(request, "grants/documents/form.html", {"form": form}, "Add document", submit_label="Save", multipart=True)


@editor_required
def document_edit(request, pk):
    doc = get_object_or_404(visible_documents(request.user), pk=pk)
    form = DocumentForm(request.POST or None, request.FILES or None, instance=doc, user=request.user)
    if request.method == "POST" and form.is_valid():
        doc = form.save(commit=False)
        doc.title = form.cleaned_data["title"]
        doc.save()
        if form.cleaned_data.get("upload"):
            services.store_upload(doc, form.cleaned_data["upload"])
        return done(request, "Document updated.", events=["refresh-documents"])
    return render_form(request, "grants/documents/form.html", {"form": form, "doc": doc}, "Edit document", multipart=True)


@editor_required
def document_delete(request, pk):
    doc = get_object_or_404(visible_documents(request.user), pk=pk)
    if request.method == "POST":
        app = doc.application
        title = doc.title
        doc.delete()
        if app:
            log_activity(app, request.user, Activity.Kind.DOCUMENT, f"Deleted document “{title}”")
        return done(request, "Document deleted.", events=["refresh-documents", "refresh-activity"])
    return render_form(request, "_confirm_delete.html", {"object": doc, "danger": True}, "Delete document", submit_label="Delete")


def document_download(request, pk):
    doc = get_object_or_404(Document, pk=pk)
    if doc.restricted and not request.user.is_owner:
        raise PermissionDenied("This document is restricted to the portal owner.")
    if not doc.filename:
        if doc.external_url:
            return redirect(doc.external_url)
        raise Http404
    try:
        blob = doc.blob
    except DocumentBlob.DoesNotExist:
        raise Http404("File content missing.")
    inline = request.GET.get("inline") == "1" and doc.content_type in services.INLINE_TYPES
    response = HttpResponse(bytes(blob.data), content_type=doc.content_type or "application/octet-stream")
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(doc.filename)}"
    response["Content-Length"] = str(len(blob.data))
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    if inline:
        # Let the browser's built-in PDF/image viewer render the file, nothing else.
        response["Content-Security-Policy"] = "default-src 'none'; object-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'"
    return response
