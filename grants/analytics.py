"""Aggregations for the dashboard and analytics pages."""

from collections import Counter, OrderedDict, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .effort import overlap_fraction
from .models import Application, Award, Tag

OUTCOME_SERIES = [
    ("Awarded", [Application.Status.AWARDED], 1),
    ("Not funded", [Application.Status.NOT_FUNDED], 2),
    ("Pending", Application.PENDING, 3),
    ("Withdrawn", [Application.Status.WITHDRAWN], "muted"),
]


def fiscal_year(d, start_month=None):
    start_month = start_month or settings.FISCAL_YEAR_START_MONTH
    if start_month == 1:
        return d.year
    return d.year + 1 if d.month >= start_month else d.year


def year_bounds(year, basis):
    """(start, end) dates for a calendar or fiscal year label."""
    if basis == "fy" and settings.FISCAL_YEAR_START_MONTH != 1:
        m = settings.FISCAL_YEAR_START_MONTH
        return date(year - 1, m, 1), date(year, m, 1) - timedelta(days=1)
    return date(year, 1, 1), date(year, 12, 31)


def year_of(d, basis):
    return fiscal_year(d) if basis == "fy" else d.year


def submission_year(app, basis):
    d = app.submitted_on or app.sponsor_deadline
    return year_of(d, basis) if d else None


# ---------------------------------------------------------------------------
# Funding runway: money per year from active awards (and optionally pending)
# ---------------------------------------------------------------------------


def _spread(amount, start, end, years, basis):
    out = [Decimal(0)] * len(years)
    if amount is None or not (start and end) or end < start:
        return out
    for i, y in enumerate(years):
        ys, ye = year_bounds(y, basis)
        frac = overlap_fraction(start, end, ys, ye) * Decimal((ye - ys).days + 1) / Decimal((end - start).days + 1)
        out[i] += Decimal(amount) * frac
    return out


def award_by_year(award, years, basis="cy", measure="total"):
    periods = list(award.periods.all())
    out = [Decimal(0)] * len(years)
    if periods:
        for p in periods:
            amount = p.direct_costs if measure == "direct" else p.total
            for i, v in enumerate(_spread(amount, p.start_date, p.end_date, years, basis)):
                out[i] += v
        return out
    amount = award.awarded_direct_total if measure == "direct" else award.awarded_total
    return _spread(amount, award.start_date, award.effective_end, years, basis)


def runway(years, basis="cy", measure="total", include_pending=True, top=6):
    awards = list(
        Award.objects.filter(status__in=Award.ACTIVE_STATUSES).select_related("application").prefetch_related("periods")
    )
    rows = []
    for a in awards:
        values = award_by_year(a, years, basis, measure)
        if any(values):
            rows.append((a.application.display_title, values))
    rows.sort(key=lambda r: -sum(r[1]))
    series = []
    for i, (name, values) in enumerate(rows[:top]):
        series.append({"name": name, "data": [float(round(v)) for v in values], "slot": i + 1})
    if len(rows) > top:
        other = [sum(r[1][i] for r in rows[top:]) for i in range(len(years))]
        series.append({"name": "Other awards", "data": [float(round(v)) for v in other], "slot": "muted"})
    pending_values = None
    if include_pending:
        pending_values = [Decimal(0)] * len(years)
        for app in Application.objects.filter(status__in=Application.PENDING):
            amount = app.requested_direct_total if measure == "direct" else app.requested_total
            for i, v in enumerate(_spread(amount, app.proposed_start, app.proposed_end, years, basis)):
                pending_values[i] += v
        if any(pending_values):
            series.append({"name": "Pending (if all funded)", "data": [float(round(v)) for v in pending_values],
                           "slot": 8 if len(series) < 7 else "muted"})
    table = [{"name": s["name"], "values": s["data"]} for s in series]
    return {"labels": [str(y) for y in years], "series": series, "table": table}


# ---------------------------------------------------------------------------
# Outcomes and success rates
# ---------------------------------------------------------------------------


def decided(qs):
    return qs.filter(status__in=Application.DECIDED)


def success_rate(qs):
    awarded = qs.filter(status=Application.Status.AWARDED).count()
    total = qs.filter(status__in=Application.DECIDED).count()
    return (round(awarded * 100 / total) if total else None), awarded, total


def submitted_apps():
    """Applications that actually went out the door (excludes ideas and abandoned drafts)."""
    return Application.objects.filter(
        status__in=Application.PENDING + [Application.Status.AWARDED, Application.Status.NOT_FUNDED, Application.Status.WITHDRAWN]
    ).select_related("funder", "award")


def outcomes_by_year(apps, years, basis):
    index = {y: i for i, y in enumerate(years)}
    series = []
    for name, statuses, slot in OUTCOME_SERIES:
        data = [0] * len(years)
        for app in apps:
            y = submission_year(app, basis)
            if y in index and app.status in statuses:
                data[index[y]] += 1
        if any(data) or name in ("Awarded", "Not funded"):
            series.append({"name": name, "data": data, "slot": slot})
    return series


def rate_by(apps, key_fn, min_n=1, limit=10):
    groups = defaultdict(lambda: [0, 0])
    for app in apps:
        if app.status not in Application.DECIDED:
            continue
        key = key_fn(app)
        if not key:
            continue
        groups[key][1] += 1
        if app.status == Application.Status.AWARDED:
            groups[key][0] += 1
    rows = [(k, a, n, round(a * 100 / n)) for k, (a, n) in groups.items() if n >= min_n]
    rows.sort(key=lambda r: (-r[2], r[0]))
    return rows[:limit]


def awarded_by_year(years, basis):
    index = {y: i for i, y in enumerate(years)}
    data = [0.0] * len(years)
    for award in Award.objects.select_related("application"):
        d = award.notice_date or award.application.decision_on or award.start_date
        amount = award.awarded_total or award.application.requested_total
        if d and amount:
            y = year_of(d, basis)
            if y in index:
                data[index[y]] += float(amount)
    return data


def months_between(a, b):
    return round((b - a).days / 30.44, 1)


def decision_times(apps):
    out = []
    for app in apps:
        if app.status in Application.DECIDED and app.submitted_on and app.decision_on and app.decision_on >= app.submitted_on:
            out.append(months_between(app.submitted_on, app.decision_on))
    return out


def scores(apps):
    points = {"awarded": [], "not_funded": []}
    for app in apps:
        if app.percentile is not None and app.impact_score is not None and app.status in Application.DECIDED:
            points["awarded" if app.status == Application.Status.AWARDED else "not_funded"].append(
                {"x": float(app.percentile), "y": float(app.impact_score), "label": app.display_title}
            )
    return points


def submissions_by_month(apps):
    counts = [0] * 12
    for app in apps:
        d = app.submitted_on or app.sponsor_deadline
        if d:
            counts[d.month - 1] += 1
    return counts


def critique_themes(limit=10):
    return list(
        Tag.objects.filter(kind=Tag.Kind.CRITIQUE)
        .annotate(n=Count("feedback", distinct=True))
        .filter(n__gt=0)
        .order_by("-n", "name")[:limit]
    )


def pipeline_counts():
    counts = dict(Application.objects.values_list("status").annotate(n=Count("id")))
    return [(s, Application.Status(s).label, counts.get(s, 0)) for s in Application.PRE_SUBMISSION + Application.PENDING]


def weighted_pipeline():
    total = Decimal(0)
    unweighted = Decimal(0)
    for app in Application.objects.filter(status__in=Application.PRE_SUBMISSION + Application.PENDING).exclude(requested_total=None):
        unweighted += app.requested_total
        if app.probability is not None:
            total += app.requested_total * Decimal(app.probability) / 100
    return total, unweighted
