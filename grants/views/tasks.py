import json
from datetime import timedelta

from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import editor_required

from ..forms import TaskForm
from ..models import Activity, Application, Task, log_activity
from .common import done, is_htmx, render_form


def task_list(request):
    today = timezone.localdate()
    view = request.GET.get("view", "open")
    qs = Task.objects.select_related("application", "application__funder", "assignee")
    if view == "mine":
        qs = qs.filter(assignee=request.user, status__in=Task.OPEN)
    elif view == "done":
        qs = qs.filter(status__in=[Task.Status.DONE, Task.Status.SKIPPED]).order_by("-completed_at")
    elif view == "post_award":
        qs = qs.filter(status__in=Task.OPEN, category__in=Task.POST_AWARD)
    else:
        qs = qs.filter(status__in=Task.OPEN)
    if request.GET.get("category"):
        qs = qs.filter(category=request.GET["category"])
    if request.GET.get("q"):
        q = request.GET["q"]
        qs = qs.filter(Q(title__icontains=q) | Q(application__title__icontains=q) | Q(application__short_name__icontains=q))

    tasks = list(qs[:500])
    if view == "done":
        groups = [("Completed", tasks)]
    else:
        week = today + timedelta(days=7)
        month = today + timedelta(days=30)
        buckets = {"Overdue": [], "Next 7 days": [], "Next 30 days": [], "Later": [], "No due date": []}
        for t in tasks:
            if not t.due_date:
                buckets["No due date"].append(t)
            elif t.due_date < today:
                buckets["Overdue"].append(t)
            elif t.due_date <= week:
                buckets["Next 7 days"].append(t)
            elif t.due_date <= month:
                buckets["Next 30 days"].append(t)
            else:
                buckets["Later"].append(t)
        groups = [(k, v) for k, v in buckets.items() if v]
    ctx = {
        "groups": groups, "view": view, "categories": Task.Category.choices, "count": len(tasks),
        "show_app": True,
    }
    if is_htmx(request) and request.GET.get("partial"):
        return render(request, "grants/tasks/_list_body.html", ctx)
    return render(request, "grants/tasks/list.html", ctx)


@editor_required
def task_create(request):
    app = None
    initial = {"assignee": request.user.pk}
    if request.GET.get("application"):
        app = get_object_or_404(Application, pk=request.GET["application"])
        initial["application"] = app.pk
        if app.status in Application.PRE_SUBMISSION and app.sponsor_deadline:
            initial["due_date"] = app.sponsor_deadline
    if request.GET.get("category"):
        initial["category"] = request.GET["category"]
    form = TaskForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        task = form.save(commit=False)
        task.created_by = request.user
        task.mark(task.status, request.user)
        task.save()
        if task.application:
            log_activity(task.application, request.user, Activity.Kind.TASK, f"Added task “{task.title}”")
        return done(request, "Task added.", redirect_to=task.application.get_absolute_url() if task.application else None,
                    events=["refresh-tasks", "refresh-activity"])
    return render_form(request, "grants/tasks/form.html", {"form": form}, "New task", submit_label="Add task")


@editor_required
def task_edit(request, pk):
    task = get_object_or_404(Task, pk=pk)
    form = TaskForm(request.POST or None, instance=task)
    if request.method == "POST" and form.is_valid():
        task = form.save(commit=False)
        task.mark(task.status, request.user)
        task.save()
        return done(request, "Task saved.", events=["refresh-tasks"])
    return render_form(request, "grants/tasks/form.html", {"form": form, "task": task}, "Edit task")


@editor_required
def task_delete(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        task.delete()
        return done(request, "Task deleted.", events=["refresh-tasks"])
    return render_form(request, "_confirm_delete.html", {"object": task, "danger": True}, "Delete task", submit_label="Delete")


@editor_required
@require_POST
def task_toggle(request, pk):
    task = get_object_or_404(Task.objects.select_related("application", "assignee"), pk=pk)
    new_status = request.POST.get("status")
    if new_status not in Task.Status.values:
        new_status = Task.Status.TODO if task.is_done else Task.Status.DONE
    task.mark(new_status, request.user)
    task.save()
    if task.is_done and task.application:
        log_activity(task.application, request.user, Activity.Kind.TASK, f"Completed “{task.title}”")
    if is_htmx(request):
        response = render(request, "grants/tasks/_row.html", {"t": task, "show_app": request.POST.get("show_app") == "1"})
        response["HX-Trigger"] = json.dumps({"tasks-changed": True})
        return response
    return done(request, "Task updated.")
