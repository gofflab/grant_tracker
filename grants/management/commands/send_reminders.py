"""Email (and optionally Slack) a digest of upcoming deadlines and overdue tasks.

    python manage.py send_reminders            # weekly-style digest
    python manage.py send_reminders --urgent   # only items due within 2 days or overdue
    python manage.py send_reminders --dry-run  # print instead of sending
"""

import json
import logging
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.models import User
from grants.calendar_events import collect_events
from grants.models import Task

logger = logging.getLogger(__name__)


def build_digest(user, days, urgent=False):
    today = timezone.localdate()
    horizon = today + timedelta(days=2 if urgent else days)
    events = [e for e in collect_events(today, horizon) if e.kind != "task"]
    my_tasks = Task.objects.filter(status__in=Task.OPEN, assignee=user, due_date__lte=horizon).select_related("application")
    overdue = Task.objects.filter(status__in=Task.OPEN, due_date__lt=today).select_related("application", "assignee")
    if not user.is_owner:
        overdue = overdue.filter(assignee=user)
    return {
        "user": user,
        "events": events,
        "my_tasks": list(my_tasks),
        "overdue": list(overdue),
        "days": 2 if urgent else days,
        "site_url": settings.SITE_URL,
        "site_name": settings.SITE_NAME,
        "urgent": urgent,
    }


def post_to_slack(text):
    if not settings.SLACK_WEBHOOK_URL:
        return
    req = urllib.request.Request(
        settings.SLACK_WEBHOOK_URL, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)  # noqa: S310 - URL comes from deployment config
    except Exception:  # noqa: BLE001
        logger.warning("Slack webhook failed", exc_info=True)


class Command(BaseCommand):
    help = "Send the deadline and task digest to users who opted in."

    def add_arguments(self, parser):
        parser.add_argument("--urgent", action="store_true", help="Only items due within 2 days, plus overdue tasks.")
        parser.add_argument("--dry-run", action="store_true", help="Print digests instead of sending.")

    def handle(self, *args, urgent=False, dry_run=False, **opts):
        sent = 0
        owner_digest = None
        for user in User.objects.filter(is_active=True, email_digest=True).exclude(email=""):
            ctx = build_digest(user, user.digest_days_ahead, urgent=urgent)
            if not (ctx["events"] or ctx["my_tasks"] or ctx["overdue"]):
                continue
            if user.is_owner and owner_digest is None:
                owner_digest = ctx
            subject = (
                f"{settings.SITE_NAME}: {'due soon' if urgent else 'week ahead'} "
                f"({len(ctx['events'])} dates, {len(ctx['my_tasks'])} tasks{', ' + str(len(ctx['overdue'])) + ' overdue' if ctx['overdue'] else ''})"
            )
            body = render_to_string("grants/email/digest.txt", ctx)
            html = render_to_string("grants/email/digest.html", ctx)
            if dry_run:
                self.stdout.write(f"--- To: {user.email}\nSubject: {subject}\n{body}")
            else:
                send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], html_message=html, fail_silently=False)
            sent += 1
        if owner_digest and settings.SLACK_WEBHOOK_URL:
            text = render_to_string("grants/email/digest_slack.txt", owner_digest)
            if dry_run:
                self.stdout.write(f"--- Slack\n{text}")
            else:
                post_to_slack(text)
        self.stdout.write(self.style.SUCCESS(f"Digests {'prepared' if dry_run else 'sent'}: {sent}"))
