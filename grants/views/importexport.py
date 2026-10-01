import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.permissions import editor_required

from .. import services
from ..forms import ImportForm
from ..models import Activity, Application, Award, Funder, Tag, log_activity
from .applications import filter_applications

EXPORT_FIELDS = [
    ("id", lambda a: a.pk),
    ("title", lambda a: a.title),
    ("short_name", lambda a: a.short_name),
    ("funder", lambda a: a.funder.name if a.funder else ""),
    ("sponsor_unit", lambda a: a.sponsor_unit),
    ("mechanism", lambda a: a.mechanism),
    ("announcement_number", lambda a: a.announcement_number),
    ("submission_type", lambda a: a.get_submission_type_display()),
    ("status", lambda a: a.get_status_display()),
    ("role", lambda a: a.get_role_display()),
    ("contact_pi", lambda a: a.contact_pi),
    ("internal_deadline", lambda a: a.internal_deadline),
    ("sponsor_deadline", lambda a: a.sponsor_deadline),
    ("submitted_on", lambda a: a.submitted_on),
    ("review_date", lambda a: a.review_date),
    ("decision_on", lambda a: a.decision_on),
    ("proposed_start", lambda a: a.proposed_start),
    ("proposed_end", lambda a: a.proposed_end),
    ("requested_direct_total", lambda a: a.requested_direct_total),
    ("requested_total", lambda a: a.requested_total),
    ("probability", lambda a: a.probability),
    ("review_panel", lambda a: a.review_panel),
    ("review_outcome", lambda a: a.get_review_outcome_display()),
    ("impact_score", lambda a: a.impact_score),
    ("percentile", lambda a: a.percentile),
    ("sponsor_id", lambda a: a.sponsor_id),
    ("internal_id", lambda a: a.internal_id),
    ("award_number", lambda a: a.award.award_number if hasattr(a, "award") else ""),
    ("awarded_total", lambda a: a.award.awarded_total if hasattr(a, "award") else ""),
    ("award_start", lambda a: a.award.start_date if hasattr(a, "award") else ""),
    ("award_end", lambda a: a.award.effective_end if hasattr(a, "award") else ""),
    ("tags", lambda a: "; ".join(t.name for t in a.tags.all())),
    ("notes", lambda a: a.notes),
]

TEMPLATE_EXAMPLE = {
    "title": "Neural stem cell commitment in the cephalopod brain",
    "short_name": "Squid NSC R01",
    "funder": "NIH",
    "sponsor_unit": "NICHD",
    "mechanism": "R01",
    "submission_type": "New",
    "status": "Not funded",
    "role": "PI",
    "sponsor_deadline": "2025-06-05",
    "submitted_on": "2025-06-04",
    "decision_on": "2025-11-20",
    "proposed_start": "2026-04-01",
    "proposed_end": "2031-03-31",
    "requested_total": "3250000",
    "impact_score": "35",
    "percentile": "28",
    "review_panel": "DEV2",
    "tags": "cephalopod; neural stem cells",
}


def export_applications(request):
    qs = filter_applications(request.GET, Application.objects.select_related("funder", "award").prefetch_related("tags"))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([name for name, _ in EXPORT_FIELDS])
    for app in qs.order_by("-submitted_on", "title"):
        writer.writerow(["" if (v := fn(app)) is None else v for _, fn in EXPORT_FIELDS])
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="applications-{timezone.localdate():%Y%m%d}.csv"'
    return resp


def import_template(request):
    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = [name for name, _ in EXPORT_FIELDS if name != "id"]
    writer.writerow(cols)
    writer.writerow([TEMPLATE_EXAMPLE.get(c, "") for c in cols])
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="grant-tracker-import-template.csv"'
    return resp


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d"]


def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date “{value}”")


def parse_decimal(value):
    value = re.sub(r"[,$\s]", "", value or "")
    if not value:
        return None
    multiplier = 1
    if value[-1:].lower() in ("k", "m"):
        multiplier = 1000 if value[-1].lower() == "k" else 1_000_000
        value = value[:-1]
    try:
        return Decimal(value) * multiplier
    except InvalidOperation:
        raise ValueError(f"not a number “{value}”")


def match_choice(choices, value, default=None):
    value = (value or "").strip().lower()
    if not value:
        return default
    for key, label in choices:
        if value in (key.lower(), label.lower()):
            return key
    for key, label in choices:
        if label.lower().startswith(value) or value.replace(" ", "_") == key:
            return key
    raise ValueError(f"unknown value “{value}”")


def find_funder(name, create):
    name = (name or "").strip()
    if not name:
        return None
    funder = Funder.objects.filter(name__iexact=name).first() or Funder.objects.filter(short_name__iexact=name).first()
    if funder or not create:
        return funder or Funder(name=name)
    return Funder.objects.create(name=name, short_name=name if len(name) <= 12 else "")


def import_rows(rows, user, dry_run=True):
    results = []
    with transaction.atomic():
        for i, row in enumerate(rows, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            errors = []
            title = row.get("title")
            if not title:
                results.append({"line": i, "ok": False, "title": "", "errors": ["title is required"]})
                continue
            app = Application(title=title[:300], created_by=user)
            app.short_name = row.get("short_name", "")[:60]
            app.sponsor_unit = row.get("sponsor_unit", "")[:120]
            app.mechanism = row.get("mechanism", "")[:40]
            app.announcement_number = row.get("announcement_number", "")[:60]
            app.contact_pi = row.get("contact_pi", "")[:120]
            app.review_panel = row.get("review_panel", "")[:120]
            app.sponsor_id = row.get("sponsor_id", "")[:60]
            app.internal_id = row.get("internal_id", "")[:60]
            app.notes = row.get("notes", "")
            for field, choices, default in (
                ("status", Application.Status.choices, Application.Status.SUBMITTED),
                ("submission_type", Application.SubmissionType.choices, Application.SubmissionType.NEW),
                ("role", Application.Role.choices, Application.Role.PI),
                ("review_outcome", Application.ReviewOutcome.choices, ""),
            ):
                try:
                    setattr(app, field, match_choice(choices, row.get(field), default))
                except ValueError as e:
                    errors.append(f"{field}: {e}")
            for field in ("internal_deadline", "sponsor_deadline", "submitted_on", "review_date", "decision_on",
                          "proposed_start", "proposed_end"):
                try:
                    setattr(app, field, parse_date(row.get(field)))
                except ValueError as e:
                    errors.append(f"{field}: {e}")
            for field in ("requested_direct_total", "requested_total", "impact_score", "percentile"):
                try:
                    setattr(app, field, parse_decimal(row.get(field)))
                except ValueError as e:
                    errors.append(f"{field}: {e}")
            if row.get("probability"):
                try:
                    app.probability = int(parse_decimal(row["probability"]))
                except (ValueError, TypeError) as e:
                    errors.append(f"probability: {e}")
            award_data = {}
            try:
                award_data = {
                    "award_number": row.get("award_number", "")[:60],
                    "awarded_total": parse_decimal(row.get("awarded_total")),
                    "start_date": parse_date(row.get("award_start")),
                    "end_date": parse_date(row.get("award_end")),
                }
            except ValueError as e:
                errors.append(f"award: {e}")
            funder = find_funder(row.get("funder"), create=not dry_run)

            if errors:
                results.append({"line": i, "ok": False, "title": title, "errors": errors})
                continue
            results.append({"line": i, "ok": True, "title": title, "errors": [],
                            "status": app.get_status_display(), "funder": funder.name if funder else ""})
            if dry_run:
                continue
            app.funder = funder
            app.save()
            for name in [t.strip() for t in re.split(r"[;,]", row.get("tags", "")) if t.strip()]:
                tag, _ = Tag.objects.get_or_create(name__iexact=name, kind=Tag.Kind.TOPIC, defaults={"name": name[:60]})
                app.tags.add(tag)
            log_activity(app, user, Activity.Kind.CREATED, "Imported from CSV")
            if app.status == Application.Status.AWARDED:
                end = award_data.get("end_date") or app.proposed_end
                award = Award.objects.create(
                    application=app,
                    award_number=award_data.get("award_number", ""),
                    awarded_total=award_data.get("awarded_total") or app.requested_total,
                    start_date=award_data.get("start_date") or app.proposed_start,
                    end_date=end,
                    status=Award.Status.CLOSED if end and end < timezone.localdate() else Award.Status.ACTIVE,
                    reporting=funder.default_reporting if funder and funder.pk else Funder.Reporting.ANNUAL,
                )
                services.generate_budget_periods(award)
        if dry_run:
            transaction.set_rollback(True)
    return results


@editor_required
def import_view(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    results = None
    dry_run = True
    if request.method == "POST" and form.is_valid():
        dry_run = form.cleaned_data["dry_run"]
        raw = form.cleaned_data["csv_file"].read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "title" not in [f.strip().lower() for f in reader.fieldnames]:
            form.add_error("csv_file", "The file needs a header row with at least a 'title' column.")
        else:
            results = import_rows(list(reader), request.user, dry_run=dry_run)
    return render(request, "grants/import.html", {
        "form": form, "results": results, "dry_run": dry_run,
        "ok_count": sum(1 for r in results if r["ok"]) if results else 0,
        "err_count": sum(1 for r in results if not r["ok"]) if results else 0,
        "columns": [name for name, _ in EXPORT_FIELDS if name != "id"],
    })
