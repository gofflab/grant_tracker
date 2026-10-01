"""Tiny scheduler for the `scheduler` container: sends digests at a fixed local hour.

Every day at REMINDER_HOUR it sends the urgent digest (items due within 2 days); on
DIGEST_WEEKDAY (0 = Monday) it sends the full look-ahead digest instead.
"""

import os
import time
from datetime import datetime, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Run the reminder schedule forever (used by the scheduler container)."

    def handle(self, *args, **opts):
        hour = int(os.environ.get("REMINDER_HOUR", "7"))
        weekday = int(os.environ.get("DIGEST_WEEKDAY", "0"))
        daily = os.environ.get("DAILY_URGENT_DIGEST", "1") not in ("0", "false", "no")
        self.stdout.write(f"Scheduler started: digest weekday={weekday}, hour={hour}:00 {timezone.get_current_timezone_name()}")
        while True:
            now = timezone.localtime()
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            time.sleep(max(30, (target - now).total_seconds()))
            run_at = timezone.localtime()
            try:
                if run_at.weekday() == weekday:
                    call_command("send_reminders")
                elif daily:
                    call_command("send_reminders", urgent=True)
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                self.stderr.write(f"{datetime.now().isoformat()} reminder run failed: {exc}")
