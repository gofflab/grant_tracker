import calendar as pycal
from collections import defaultdict
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from icalendar import Calendar
from icalendar import Event as ICalEvent

from accounts.models import User

from ..calendar_events import KIND_LABELS, collect_events
from .common import is_htmx


def calendar_view(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("y", today.year))
        month = int(request.GET.get("m", today.month))
        first = date(year, month, 1)
    except ValueError:
        first = today.replace(day=1)
    kinds = request.GET.getlist("kind") or list(KIND_LABELS)
    cal = pycal.Calendar(firstweekday=6)  # Sunday first
    weeks = cal.monthdatescalendar(first.year, first.month)
    start, end = weeks[0][0], weeks[-1][-1]
    by_day = defaultdict(list)
    for ev in collect_events(start, end):
        if ev.kind in kinds:
            by_day[ev.date].append(ev)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    agenda = [ev for ev in collect_events(today, today + timedelta(days=60)) if ev.kind in kinds]
    ctx = {
        "weeks": [[{"date": d, "events": by_day.get(d, []), "other": d.month != first.month} for d in w] for w in weeks],
        "first": first,
        "prev": prev_month,
        "next": next_month,
        "kinds": KIND_LABELS,
        "selected_kinds": kinds,
        "agenda": agenda,
        "dow": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    }
    template = "grants/calendar/_month.html" if is_htmx(request) else "grants/calendar/calendar.html"
    return render(request, template, ctx)


@login_not_required
def calendar_feed(request, token):
    """Read-only iCalendar feed authenticated by a per-user secret token."""
    try:
        user = User.objects.get(calendar_token=token, is_active=True)
    except User.DoesNotExist:
        raise Http404
    today = timezone.localdate()
    cal = Calendar()
    cal.add("prodid", f"-//{settings.SITE_NAME}//Grant Tracker//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", settings.SITE_NAME)
    cal.add("x-published-ttl", "PT6H")
    stamp = datetime.now(tz=timezone.get_current_timezone())
    host = settings.SITE_URL.split("://", 1)[-1]
    for ev in collect_events(today - timedelta(days=90), today + timedelta(days=730)):
        item = ICalEvent()
        item.add("uid", f"{ev.uid}@{host}")
        item.add("summary", ev.title)
        item.add("dtstart", ev.date)
        item.add("dtend", ev.date + timedelta(days=1))
        item.add("dtstamp", stamp)
        item.add("description", f"{ev.kind_label}{' · ' + ev.subtitle if ev.subtitle else ''}\n{settings.SITE_URL}{ev.url}")
        item.add("url", f"{settings.SITE_URL}{ev.url}")
        item.add("categories", [ev.kind_label])
        item.add("transp", "TRANSPARENT")
        cal.add_component(item)
    resp = HttpResponse(cal.to_ical(), content_type="text/calendar; charset=utf-8")
    resp["Content-Disposition"] = 'inline; filename="grant-tracker.ics"'
    resp["Cache-Control"] = "private, max-age=900"
    return resp
