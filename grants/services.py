"""Business logic shared by views, management commands and tests."""

import hashlib
import io
import logging
import mimetypes
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .models import (
    Activity,
    Application,
    Award,
    BudgetPeriod,
    ChecklistItem,
    ChecklistTemplate,
    Document,
    DocumentBlob,
    Funder,
    Opportunity,
    Person,
    Personnel,
    StatusChange,
    Task,
    log_activity,
)

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "rtf", "odt", "txt", "md", "tex", "bib",
    "xls", "xlsx", "csv", "ods", "ppt", "pptx", "odp",
    "png", "jpg", "jpeg", "gif", "tif", "tiff", "zip",
}
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif"}
MAX_TEXT_CHARS = 400_000


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def previous_weekday(d):
    """Shift Saturday/Sunday back to Friday so generated due dates land on workdays."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Status workflow
# ---------------------------------------------------------------------------


@transaction.atomic
def change_status(application, new_status, user=None, note=""):
    """Move an application to a new status, recording history and side effects.

    Returns the Award if this change created one, else None.
    """
    old_status = application.status
    if new_status == old_status:
        return None
    today = timezone.localdate()

    application.status = new_status
    fields = ["status", "updated_at"]
    if new_status in Application.PENDING + [Application.Status.AWARDED] and not application.submitted_on:
        application.submitted_on = today
        fields.append("submitted_on")
    if new_status in Application.DECIDED and not application.decision_on:
        application.decision_on = today
        fields.append("decision_on")
    application.save(update_fields=fields)

    StatusChange.objects.create(
        application=application,
        from_status=old_status,
        to_status=new_status,
        changed_by=user if getattr(user, "is_authenticated", False) else None,
        note=note[:300],
    )
    label = Application.Status(new_status).label
    log_activity(
        application, user, Activity.Kind.STATUS,
        f"Status: {Application.Status(old_status).label} → {label}" + (f" ({note})" if note else ""),
    )

    opp = application.opportunity
    if opp and new_status not in (Application.Status.NOT_PURSUED, Application.Status.WITHDRAWN):
        if opp.status in (Opportunity.Status.WATCHING, Opportunity.Status.PLANNING):
            opp.status = Opportunity.Status.APPLYING
            opp.save(update_fields=["status", "updated_at"])

    if new_status == Application.Status.AWARDED and not hasattr(application, "award"):
        return create_award(application, user)
    return None


@transaction.atomic
def create_award(application, user=None):
    funder = application.funder
    award = Award.objects.create(
        application=application,
        start_date=application.proposed_start,
        end_date=application.proposed_end,
        awarded_direct_total=application.requested_direct_total,
        awarded_total=application.requested_total,
        fa_rate=application.fa_rate,
        reporting=funder.default_reporting if funder else Funder.Reporting.ANNUAL,
        notice_date=timezone.localdate(),
    )
    log_activity(application, user, Activity.Kind.AWARD, "Award record created")
    for template in ChecklistTemplate.objects.filter(applies_to=ChecklistTemplate.AppliesTo.AWARD, is_default=True):
        apply_checklist(application, template, user)
    if award.start_date and award.end_date:
        generate_budget_periods(award)
        generate_reporting_tasks(award, user)
    return award


def award_generated_tasks(award):
    """Tasks the award created: its reporting schedule and the award-setup checklist."""
    app = award.application
    setup_titles = ChecklistItem.objects.filter(
        template__applies_to=ChecklistTemplate.AppliesTo.AWARD
    ).values_list("title", flat=True)
    return app.tasks.filter(
        models.Q(auto_key__startswith=f"award:{award.pk}:")
        | models.Q(title__in=list(setup_titles), created_at__gte=award.created_at - timedelta(minutes=1))
    )


def status_before_award(application):
    """The status the application had before it was last marked Awarded."""
    change = application.status_changes.filter(to_status=Application.Status.AWARDED).order_by("-changed_at").first()
    if change and change.from_status and change.from_status != Application.Status.AWARDED:
        return change.from_status
    return Application.Status.PENDING_AWARD


@transaction.atomic
def delete_award(award, user=None, move_to=None, delete_tasks=True, reason=""):
    """Delete an award record (budget years go with it) and optionally its generated tasks.

    The application moves to `move_to` unless that is Awarded, in which case it stays Awarded with
    no award record so a fresh one can be created. Returns a summary dict for the confirmation message.
    """
    app = award.application
    label = award.award_number or "award record"
    summary = {"periods": award.periods.count(), "tasks": 0, "status": None}
    if delete_tasks:
        summary["tasks"] = award_generated_tasks(award).delete()[1].get("grants.Task", 0)
    award.delete()
    note = f"Award record deleted{': ' + reason if reason else ''}"
    log_activity(app, user, Activity.Kind.AWARD, f"Deleted {label}" + (f" ({reason})" if reason else ""))
    if move_to and move_to != app.status:
        change_status(app, move_to, user, note)
        app.refresh_from_db()
        fields = []
        if move_to not in Application.DECIDED and app.decision_on:
            app.decision_on = None
            fields.append("decision_on")
        if move_to in Application.PRE_SUBMISSION and app.submitted_on:
            app.submitted_on = None
            fields.append("submitted_on")
        if fields:
            app.save(update_fields=fields + ["updated_at"])
        summary["status"] = app.get_status_display()
    return summary


# ---------------------------------------------------------------------------
# Checklists
# ---------------------------------------------------------------------------


def _anchor_date(application, anchor):
    award = getattr(application, "award", None)
    return {
        ChecklistItem.Anchor.SPONSOR: application.sponsor_deadline,
        ChecklistItem.Anchor.INTERNAL: application.internal_deadline or application.sponsor_deadline,
        ChecklistItem.Anchor.AWARD_START: award.start_date if award else application.proposed_start,
        ChecklistItem.Anchor.AWARD_END: award.effective_end if award else application.proposed_end,
        ChecklistItem.Anchor.NONE: None,
    }.get(anchor)


@transaction.atomic
def apply_checklist(application, template, user=None):
    """Create one task per checklist item, skipping titles that already exist on the application."""
    existing = set(application.tasks.values_list("title", flat=True))
    created = []
    for item in template.items.all():
        if item.title in existing:
            continue
        anchor = _anchor_date(application, item.anchor)
        due = previous_weekday(anchor + timedelta(days=item.offset_days)) if anchor else None
        created.append(
            Task(
                application=application,
                title=item.title,
                category=item.category,
                due_date=due,
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
        )
    Task.objects.bulk_create(created)
    if created:
        log_activity(application, user, Activity.Kind.TASK, f"Applied checklist “{template.name}” ({len(created)} tasks)")
    return created


# ---------------------------------------------------------------------------
# Awards: budget periods and reporting schedule
# ---------------------------------------------------------------------------


def _split(total, n):
    if total is None or n <= 0:
        return [None] * n
    share = (Decimal(total) / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    parts = [share] * n
    parts[-1] = Decimal(total) - share * (n - 1)
    return parts


@transaction.atomic
def generate_budget_periods(award, replace=False):
    """Create annual budget periods covering the project period.

    Awarded totals are split evenly as a starting point; edit years individually as NoAs arrive.
    """
    if not (award.start_date and award.end_date) or award.end_date <= award.start_date:
        return []
    if award.periods.exists():
        if not replace:
            return list(award.periods.all())
        award.periods.all().delete()

    spans = []
    start = award.start_date
    while start <= award.end_date:
        end = min(start + relativedelta(years=1) - timedelta(days=1), award.end_date)
        spans.append((start, end))
        start = end + timedelta(days=1)

    directs = _split(award.awarded_direct_total, len(spans))
    totals = _split(award.awarded_total, len(spans))
    periods = []
    for i, (s, e) in enumerate(spans):
        indirect = (totals[i] - directs[i]) if (totals[i] is not None and directs[i] is not None) else None
        periods.append(
            BudgetPeriod(
                award=award,
                number=i + 1,
                start_date=s,
                end_date=e,
                direct_costs=directs[i],
                indirect_costs=indirect,
                status=BudgetPeriod.Status.AWARDED if i == 0 else BudgetPeriod.Status.PROJECTED,
                noa_date=award.notice_date if i == 0 else None,
            )
        )
    return BudgetPeriod.objects.bulk_create(periods)


def reporting_schedule(award):
    """Return (key, title, category, due_date, description) tuples for the award's reporting style.

    NIH rules encoded here (verify against each Notice of Award):
      * SNAP: annual RPPR due the 15th of the month before the budget period's final month
        (about 45 days before the next budget period starts).
      * Multi-year funded: annual RPPR due on each anniversary of the project start.
      * Final RPPR, final FFR and Final Invention Statement: 120 days after the project ends.
    """
    style = award.reporting
    start, end = award.start_date, award.effective_end
    if not (start and end) or style == Funder.Reporting.NONE:
        return []
    items = []
    years = []
    s = start
    while s <= end:
        e = min(s + relativedelta(years=1) - timedelta(days=1), end)
        years.append((s, e))
        s = e + timedelta(days=1)

    if style == Funder.Reporting.NIH_SNAP:
        for i, (_, e) in enumerate(years[:-1], start=1):
            due = (e.replace(day=1) - relativedelta(months=1)).replace(day=15)
            items.append((
                f"rppr:{i + 1}", f"Annual RPPR (Year {i + 1} continuation)", Task.Category.REPORTING, due,
                "NIH SNAP RPPR. Due date is approximate; confirm in eRA Commons.",
            ))
    elif style == Funder.Reporting.NIH_MYPR:
        for i in range(1, len(years)):
            due = start + relativedelta(years=i)
            items.append((
                f"rppr:{i + 1}", f"Annual RPPR (Year {i})", Task.Category.REPORTING, due,
                "NIH multi-year funded RPPR, due on the project anniversary.",
            ))
    elif style == Funder.Reporting.ANNUAL:
        for i in range(1, len(years)):
            due = start + relativedelta(years=i)
            items.append((
                f"annual:{i}", f"Annual progress report (Year {i})", Task.Category.REPORTING, due,
                "Check the award terms for the exact due date.",
            ))

    if style in (Funder.Reporting.NIH_SNAP, Funder.Reporting.NIH_MYPR):
        final_due = end + timedelta(days=120)
        items += [
            ("final_rppr", "Final RPPR", Task.Category.REPORTING, final_due, "Due 120 days after the period of performance ends."),
            ("final_ffr", "Final Federal Financial Report (FFR)", Task.Category.FINANCIAL, final_due,
             "Usually submitted by the institution's sponsored projects office; confirm they have it."),
            ("final_fis", "Final Invention Statement", Task.Category.REPORTING, final_due, "Due 120 days after the period of performance ends."),
        ]
    else:
        items.append((
            "final_report", "Final report", Task.Category.REPORTING, end + timedelta(days=90),
            "Check the award terms for the exact due date.",
        ))
    return items


@transaction.atomic
def generate_reporting_tasks(award, user=None):
    """Create (or refresh) reporting tasks. Completed tasks are never touched."""
    application = award.application
    prefix = f"award:{award.pk}:"
    wanted = {prefix + key: (title, cat, due, desc) for key, title, cat, due, desc in reporting_schedule(award)}
    existing = {t.auto_key: t for t in application.tasks.filter(auto_key__startswith=prefix)}

    created = updated = 0
    for key, (title, cat, due, desc) in wanted.items():
        task = existing.get(key)
        if task is None:
            Task.objects.create(
                application=application, title=title, category=cat, due_date=due, description=desc,
                auto_key=key,
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
            created += 1
        elif not task.is_done and (task.due_date != due or task.title != title):
            task.due_date, task.title = due, title
            task.save(update_fields=["due_date", "title", "updated_at"])
            updated += 1
    stale = [t for k, t in existing.items() if k not in wanted and not t.is_done]
    for task in stale:
        task.delete()
    if created or updated or stale:
        log_activity(
            application, user, Activity.Kind.AWARD,
            f"Reporting schedule updated ({created} added, {updated} rescheduled, {len(stale)} removed)",
        )
    return created, updated, len(stale)


# ---------------------------------------------------------------------------
# Lab PI
# ---------------------------------------------------------------------------

# "My role" on an application -> the Lab PI's personnel role on that application.
LAB_PI_ROLES = {
    Application.Role.PI: Personnel.Role.PI,
    Application.Role.MPI: Personnel.Role.MPI,
    Application.Role.CO_PI: Personnel.Role.CO_PI,
    Application.Role.CO_I: Personnel.Role.CO_I,
    Application.Role.KEY: Personnel.Role.KEY,
    Application.Role.MENTOR: Personnel.Role.KEY,
    Application.Role.CONSULTANT: Personnel.Role.CONSULTANT,
}


def add_lab_pi(application):
    """Put the Lab PI on the application's team (no effort yet). Returns the new line, or None."""
    lab_pi = Person.lab_pi()
    if lab_pi is None or application.personnel.filter(person=lab_pi).exists():
        return None
    return Personnel.objects.create(
        application=application, person=lab_pi, is_key=True,
        role=LAB_PI_ROLES.get(application.role, Personnel.Role.KEY),
    )


def sync_lab_pi_role(application, old_role):
    """When "My role" changes, update the Lab PI's line if it still carries the role implied by the old value."""
    lab_pi = Person.lab_pi()
    if lab_pi is None:
        return 0
    return application.personnel.filter(person=lab_pi, role=LAB_PI_ROLES.get(old_role)).update(
        role=LAB_PI_ROLES.get(application.role, Personnel.Role.KEY)
    )


@transaction.atomic
def claim_lab_pi(user):
    """Make (or create) the user's own person record the Lab PI. Returns (person, error)."""
    current = Person.lab_pi()
    mine = getattr(user, "person", None)
    if current and current != mine:
        return None, f"{current} is already the Lab PI. Change their type in Settings → People first."
    if not user.is_owner:
        return None, "Only an Owner account can be the Lab PI."
    if mine is None:
        mine = Person.objects.create(
            first_name=user.first_name or user.username, last_name=user.last_name, email=user.email,
            kind=Person.Kind.LAB_PI, user=user,
        )
    elif not mine.is_lab_pi:
        mine.kind = Person.Kind.LAB_PI
        mine.save(update_fields=["kind", "updated_at"])
    return mine, None


# ---------------------------------------------------------------------------
# Resubmissions and renewals
# ---------------------------------------------------------------------------

CLONE_FIELDS = [
    "title", "short_name", "funder", "sponsor_unit", "mechanism", "announcement_number", "opportunity",
    "role", "contact_pi", "lead_institution", "priority", "abstract", "major_goals", "folder_url",
    "program_officer", "grants_admin", "requested_direct_y1", "requested_direct_total", "requested_total",
    "fa_rate", "duration_years", "extra",
]


@transaction.atomic
def clone_application(source, submission_type, user=None):
    new = Application(parent=source, submission_type=submission_type, status=Application.Status.PLANNING)
    for field in CLONE_FIELDS:
        setattr(new, field, getattr(source, field))
    new.created_by = user if getattr(user, "is_authenticated", False) else None
    if submission_type == Application.SubmissionType.RESUBMISSION and source.short_name:
        base = source.short_name.rsplit(" A", 1)[0]
        new.short_name = f"{base} A{int(source.resubmission_label[1:]) + 1}"[:60]
    new.save()
    new.tags.set(source.tags.all())
    Personnel.objects.bulk_create(
        Personnel(application=new, person=p.person, role=p.role, person_months=p.person_months, is_key=p.is_key)
        for p in source.personnel.all()
    )
    add_lab_pi(new)
    if submission_type == Application.SubmissionType.RESUBMISSION:
        Task.objects.create(
            application=new, title="Write Introduction to the resubmission", category=Task.Category.WRITING,
            description=f"Respond to the critiques of the previous submission ({source.display_title}).",
            created_by=new.created_by,
        )
    log_activity(new, user, Activity.Kind.CREATED, f"Created as {new.get_submission_type_display().lower()} of “{source.display_title}”")
    log_activity(source, user, Activity.Kind.CREATED, f"{new.get_submission_type_display()} started: “{new.display_title}”")
    return new


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def file_extension(name):
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def extract_text(data: bytes, filename: str) -> str:
    ext = file_extension(filename)
    try:
        if ext == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:300])
        elif ext == "docx":
            import docx

            document = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in document.paragraphs)
        elif ext in {"txt", "md", "tex", "bib", "csv"}:
            text = data.decode("utf-8", errors="replace")
        else:
            return ""
    except Exception:  # noqa: BLE001 - extraction is best-effort
        logger.warning("Text extraction failed for %s", filename, exc_info=True)
        return ""
    return text.replace("\x00", "")[:MAX_TEXT_CHARS]


def store_upload(document: Document, upload):
    """Read an uploaded file into the database, replacing any previous file on this document."""
    data = upload.read()
    document.filename = upload.name[:255]
    document.content_type = (
        mimetypes.guess_type(upload.name)[0] or getattr(upload, "content_type", "") or "application/octet-stream"
    )[:120]
    document.size = len(data)
    document.sha256 = hashlib.sha256(data).hexdigest()
    document.extracted_text = extract_text(data, upload.name)
    document.save()
    DocumentBlob.objects.update_or_create(document=document, defaults={"data": data})
    return document


def max_upload_bytes():
    return settings.MAX_UPLOAD_MB * 1024 * 1024
