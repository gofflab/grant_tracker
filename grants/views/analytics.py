from statistics import median

from django.conf import settings
from django.shortcuts import render
from django.utils import timezone

from .. import analytics as an
from ..models import Application, Funder
from .common import json_for_chart

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def analytics(request):
    today = timezone.localdate()
    basis = request.GET.get("basis", "cy")
    if basis not in ("cy", "fy"):
        basis = "cy"
    span = request.GET.get("span", "5")
    current = an.fiscal_year(today) if basis == "fy" else today.year

    apps = list(an.submitted_apps().prefetch_related("tags"))
    if request.GET.get("funder"):
        apps = [a for a in apps if str(a.funder_id) == request.GET["funder"]]
    if request.GET.get("role") == "lead":
        apps = [a for a in apps if a.role in ("pi", "mpi")]

    all_years = sorted({y for a in apps if (y := an.submission_year(a, basis))})
    if span == "all" and all_years:
        years = list(range(min(all_years), current + 1))
    else:
        n = int(span) if span.isdigit() else 5
        years = list(range(current - n + 1, current + 1))
    in_range = [a for a in apps if an.submission_year(a, basis) in years]

    # Headline tiles
    decided = [a for a in in_range if a.status in Application.DECIDED]
    awarded = [a for a in decided if a.status == Application.Status.AWARDED]
    times = an.decision_times(in_range)
    percentiles = [float(a.percentile) for a in in_range if a.percentile is not None]
    total_awarded = sum(float(getattr(a, "award", None) and a.award.awarded_total or a.requested_total or 0) for a in awarded)
    tiles = {
        "submitted": len(in_range),
        "awarded": len(awarded),
        "decided": len(decided),
        "rate": round(len(awarded) * 100 / len(decided)) if decided else None,
        "total_awarded": total_awarded,
        "median_time": median(times) if times else None,
        "median_percentile": median(percentiles) if percentiles else None,
        "pending": sum(1 for a in in_range if a.status in Application.PENDING),
    }

    labels = [("FY" if basis == "fy" else "") + str(y) for y in years]
    outcomes = an.outcomes_by_year(in_range, years, basis)
    funder_rows = an.rate_by(in_range, lambda a: a.funder.display_name if a.funder else "")
    mech_rows = an.rate_by(in_range, lambda a: (a.mechanism.upper() if len(a.mechanism) <= 6 else a.mechanism) if a.mechanism else "")
    awarded_money = an.awarded_by_year(years, basis)
    runway_years = list(range(current - 1, current + 5))
    rw = an.runway(runway_years, basis, measure=request.GET.get("measure", "total"))
    pts = an.scores(apps)
    bins = [("< 3 mo", 0, 3), ("3–6", 3, 6), ("6–9", 6, 9), ("9–12", 9, 12), ("12+", 12, 999)]
    time_counts = [sum(1 for t in times if lo <= t < hi) for _, lo, hi in bins]
    by_month = an.submissions_by_month(in_range)
    themes = an.critique_themes()

    charts = {
        "outcomes": {"type": "bar", "stacked": True, "labels": labels, "series": outcomes},
        "funders": {"type": "bar", "horizontal": True, "labels": [f"{r[0]} (n={r[2]})" for r in funder_rows],
                    "series": [{"name": "Success rate", "data": [r[3] for r in funder_rows], "slot": 1}], "format": "pct", "max": 100},
        "mechanisms": {"type": "bar", "horizontal": True, "labels": [f"{r[0]} (n={r[2]})" for r in mech_rows],
                       "series": [{"name": "Success rate", "data": [r[3] for r in mech_rows], "slot": 1}], "format": "pct", "max": 100},
        "awarded": {"type": "bar", "labels": labels, "series": [{"name": "Awarded (total costs)", "data": awarded_money, "slot": 1}], "format": "money"},
        "runway": {"type": "bar", "stacked": True, "labels": rw["labels"], "series": rw["series"], "format": "money"},
        "scores": {"type": "scatter", "labels": [], "series": [
            {"name": "Awarded", "data": pts["awarded"], "slot": 1},
            {"name": "Not funded", "data": pts["not_funded"], "slot": 2},
        ], "xTitle": "Percentile", "yTitle": "Impact score", "yReverse": True, "xReverse": True},
        "times": {"type": "bar", "labels": [b[0] for b in bins], "series": [{"name": "Applications", "data": time_counts, "slot": 1}]},
        "months": {"type": "bar", "labels": MONTHS, "series": [{"name": "Submissions", "data": by_month, "slot": 1}]},
        "themes": {"type": "bar", "horizontal": True, "labels": [t.name for t in themes],
                   "series": [{"name": "Reviews citing theme", "data": [t.n for t in themes], "slot": 1}]},
    }
    return render(request, "grants/analytics.html", {
        "tiles": tiles,
        "charts": {k: json_for_chart(v) for k, v in charts.items()},
        "raw": charts,
        "funder_rows": funder_rows,
        "mech_rows": mech_rows,
        "runway": rw,
        "has_scores": bool(pts["awarded"] or pts["not_funded"]),
        "score_points": pts,
        "themes": themes,
        "time_bins": list(zip([b[0] for b in bins], time_counts)),
        "by_month": list(zip(MONTHS, by_month)),
        "labels": labels,
        "outcomes": outcomes,
        "awarded_money": list(zip(labels, awarded_money)),
        "has_awarded_money": any(awarded_money),
        "basis": basis,
        "span": span,
        "fy_month": settings.FISCAL_YEAR_START_MONTH,
        "funders": Funder.objects.filter(applications__isnull=False).distinct(),
        "has_data": bool(apps),
    })
