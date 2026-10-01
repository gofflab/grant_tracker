from django.conf import settings
from django.utils import timezone

from .models import Application, Task


def global_context(request):
    ctx = {"SITE_NAME": settings.SITE_NAME, "today": timezone.localdate()}
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        today = timezone.localdate()
        ctx["nav_counts"] = {
            "overdue_tasks": Task.objects.filter(status__in=Task.OPEN, due_date__lt=today).count(),
            "open_apps": Application.objects.exclude(status__in=Application.CLOSED).count(),
        }
    return ctx
