"""Effort (person-month) calculations used by the effort dashboard and Other Support reports."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.utils import timezone

from .models import Application, Award, Personnel

TWELVE = Decimal(12)


def overlap_fraction(start, end, win_start, win_end):
    """Fraction of the window [win_start, win_end] covered by [start, end]. Open ends cover everything."""
    s = max(start or win_start, win_start)
    e = min(end or win_end, win_end)
    if e < s:
        return Decimal(0)
    window = (win_end - win_start).days + 1
    return Decimal((e - s).days + 1) / Decimal(window)


def line_bucket(line, on):
    """'active', 'pending' or None for a personnel line on a given date."""
    app = line.application
    if app.status == Application.Status.AWARDED:
        award = getattr(app, "award", None)
        if award and not award.is_active:
            return None
        start, end = line.period()
        if (start and start > on) or (end and end < on):
            return "future" if start and start > on else None
        return "active"
    if app.status in Application.PENDING:
        return "pending"
    return None


def effort_lines():
    return (
        Personnel.objects.select_related("person", "application", "application__funder", "application__award")
        .filter(person_months__gt=0)
        .filter(application__status__in=[Application.Status.AWARDED, *Application.PENDING])
    )


def effort_summary(on=None):
    """Per-person committed (active), future-starting, and pending effort."""
    on = on or timezone.localdate()
    people = {}
    for line in effort_lines():
        bucket = line_bucket(line, on)
        if not bucket:
            continue
        entry = people.setdefault(line.person_id, {
            "person": line.person, "active": [], "future": [], "pending": [],
            "active_pm": Decimal(0), "pending_pm": Decimal(0), "future_pm": Decimal(0),
        })
        entry[bucket].append(line)
        entry[f"{bucket}_pm"] += line.person_months
    rows = sorted(people.values(), key=lambda r: (-r["active_pm"], r["person"].last_name))
    for r in rows:
        r["active_pct"] = (r["active_pm"] / TWELVE * 100).quantize(Decimal("1"))
        r["combined_pm"] = r["active_pm"] + r["pending_pm"]
        r["combined_pct"] = (r["combined_pm"] / TWELVE * 100).quantize(Decimal("1"))
    return rows


def effort_by_year(person, years, include_pending=True):
    """Calendar-year person-months for one person, split by application."""
    series = defaultdict(lambda: [Decimal(0)] * len(years))
    labels = {}
    for line in effort_lines().filter(person=person):
        app = line.application
        if app.status in Application.PENDING and not include_pending:
            continue
        award = getattr(app, "award", None)
        if award and not award.is_active and app.status == Application.Status.AWARDED:
            continue
        start, end = line.period()
        if not start and not end:
            continue
        for i, y in enumerate(years):
            frac = overlap_fraction(start, end, date(y, 1, 1), date(y, 12, 31))
            series[app.pk][i] += (line.person_months * frac).quantize(Decimal("0.01"))
        labels[app.pk] = (app, app.status in Application.PENDING)
    return [(labels[k][0], labels[k][1], v) for k, v in series.items()]


def budget_years(start, end):
    if not (start and end):
        return []
    years = []
    s = start
    while s <= end:
        e = min(s + relativedelta(years=1) - timedelta(days=1), end)
        years.append((s, e))
        s = e + timedelta(days=1)
    return years


def project_person_months(app, person):
    """Person-months per budget year for a person on one application (for Other Support)."""
    award = getattr(app, "award", None)
    start = (award.start_date if award else None) or app.proposed_start
    end = (award.effective_end if award else None) or app.proposed_end
    lines = [l for l in app.personnel.all() if l.person_id == person.pk and l.person_months is not None]
    out = []
    for i, (ys, ye) in enumerate(budget_years(start, end), start=1):
        pm = Decimal(0)
        for line in lines:
            ls, le = line.start_date or start, line.end_date or end
            pm += line.person_months * overlap_fraction(ls, le, ys, ye)
        out.append({"year": i, "start": ys, "end": ye, "pm": pm.quantize(Decimal("0.01"))})
    return out
