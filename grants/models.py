"""
Grant Tracker data model.

Opportunity  ->  Application  ->  Award
  (an RFA)       (one submission)    (when funded)

Applications carry tasks, documents, personnel/effort, peer-review feedback,
comments and an activity log. Resubmissions link back to their parent so the
full A0 -> A1 -> renewal lineage stays visible.
"""

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from simple_history.models import HistoricalRecords

USER = settings.AUTH_USER_MODEL
MONEY = {"max_digits": 14, "decimal_places": 2, "null": True, "blank": True}


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


class Tag(models.Model):
    class Kind(models.TextChoices):
        TOPIC = "topic", "Topic / theme"
        CRITIQUE = "critique", "Critique theme"

    class Color(models.TextChoices):
        BLUE = "blue", "Blue"
        TEAL = "teal", "Teal"
        GREEN = "green", "Green"
        AMBER = "amber", "Amber"
        ORANGE = "orange", "Orange"
        RED = "red", "Red"
        PINK = "pink", "Pink"
        PURPLE = "purple", "Purple"
        GRAY = "gray", "Gray"

    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.TOPIC)
    color = models.CharField(max_length=10, choices=Color.choices, default=Color.BLUE)

    class Meta:
        ordering = ["kind", "name"]
        constraints = [models.UniqueConstraint(fields=["name", "kind"], name="unique_tag_per_kind")]

    def __str__(self):
        return self.name


class Funder(TimeStamped):
    class Type(models.TextChoices):
        FEDERAL = "federal", "Federal agency"
        FOUNDATION = "foundation", "Private foundation"
        INSTITUTIONAL = "institutional", "Internal / institutional"
        INDUSTRY = "industry", "Industry"
        STATE = "state", "State / local government"
        SOCIETY = "society", "Professional society"
        INTERNATIONAL = "international", "International"
        OTHER = "other", "Other"

    class Reporting(models.TextChoices):
        NIH_SNAP = "nih_snap", "NIH, annual RPPR (SNAP)"
        NIH_MYPR = "nih_mypr", "NIH, multi-year funded (annual RPPR on anniversary)"
        ANNUAL = "annual", "Annual + final report"
        FINAL = "final", "Final report only"
        NONE = "none", "No standard schedule"

    name = models.CharField(max_length=200, unique=True)
    short_name = models.CharField("Abbreviation", max_length=40, blank=True)
    funder_type = models.CharField("Type", max_length=20, choices=Type.choices, default=Type.FEDERAL)
    website = models.URLField(blank=True)
    default_reporting = models.CharField(
        "Default reporting schedule", max_length=10, choices=Reporting.choices, default=Reporting.ANNUAL
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.short_name or self.name

    @property
    def display_name(self):
        return self.short_name or self.name


class Person(TimeStamped):
    class Kind(models.TextChoices):
        LAB_PI = "lab_pi", "Lab PI (lab head)"
        LAB = "lab", "Lab member"
        PI = "pi", "Principal investigator (other lab)"
        COLLABORATOR = "collaborator", "External collaborator"
        INTERNAL = "internal", "Institutional colleague"
        PROGRAM_OFFICER = "program_officer", "Program officer"
        ADMIN = "admin", "Grants administrator"
        MENTOR = "mentor", "Mentor / advisor"
        OTHER = "other", "Other"

    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    kind = models.CharField("Type", max_length=20, choices=Kind.choices, default=Kind.LAB)
    position = models.CharField(max_length=120, blank=True, help_text="e.g. Postdoctoral fellow, Program Director")
    institution = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    orcid = models.CharField("ORCID", max_length=40, blank=True)
    era_commons_id = models.CharField("eRA Commons ID", max_length=40, blank=True)
    user = models.OneToOneField(
        USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="person",
        help_text="Link to a portal account, if this person signs in.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name_plural = "people"
        constraints = [
            # The lab head is a single, special record: the default person for effort and
            # Current & Pending, and added automatically to every new application.
            models.UniqueConstraint(fields=["kind"], condition=models.Q(kind="lab_pi"), name="single_lab_pi"),
        ]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_lab_pi(self):
        return self.kind == self.Kind.LAB_PI

    @classmethod
    def lab_pi(cls):
        return cls.objects.filter(kind=cls.Kind.LAB_PI).first()


# ---------------------------------------------------------------------------
# Opportunities (RFAs, NOFOs, program announcements, foundation calls)
# ---------------------------------------------------------------------------


class Opportunity(TimeStamped):
    class Status(models.TextChoices):
        WATCHING = "watching", "Watching"
        PLANNING = "planning", "Planning to apply"
        APPLYING = "applying", "Applying"
        PASSED = "passed", "Passed"
        CLOSED = "closed", "Closed / expired"

    ACTIVE_STATUSES = {Status.WATCHING, Status.PLANNING, Status.APPLYING}

    title = models.CharField(max_length=300)
    funder = models.ForeignKey(Funder, null=True, blank=True, on_delete=models.SET_NULL, related_name="opportunities")
    sponsor_unit = models.CharField(
        "Institute / directorate / program", max_length=120, blank=True, help_text="e.g. NICHD, NSF BIO/IOS"
    )
    number = models.CharField("Announcement number", max_length=60, blank=True, help_text="e.g. PAR-25-123")
    mechanism = models.CharField("Mechanism / activity code", max_length=40, blank=True, help_text="e.g. R01, R21, CAREER")
    url = models.URLField("Announcement URL", blank=True, max_length=500)
    summary = models.TextField(blank=True)
    eligibility = models.TextField(blank=True)
    max_award = models.DecimalField("Award ceiling", **MONEY)
    budget_notes = models.CharField(max_length=200, blank=True, help_text="e.g. $250K direct/yr, modular")
    max_duration_years = models.PositiveSmallIntegerField("Max duration (years)", null=True, blank=True)
    limited_submission = models.BooleanField(
        default=False, help_text="Institution may only nominate a limited number of applicants."
    )
    loi_deadline = models.DateField("LOI / pre-proposal due", null=True, blank=True)
    internal_deadline = models.DateField("Internal deadline", null=True, blank=True)
    deadline = models.DateField("Sponsor deadline", null=True, blank=True)
    recurring = models.BooleanField("Recurring due dates", default=False)
    recurrence_notes = models.CharField(max_length=200, blank=True, help_text="e.g. Feb 5 / Jun 5 / Oct 5")
    expires_on = models.DateField("Announcement expires", null=True, blank=True)
    fit_score = models.PositiveSmallIntegerField(
        "Fit (1-5)", null=True, blank=True, choices=[(i, str(i)) for i in range(1, 6)]
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.WATCHING)
    contact = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="opportunity_contacts",
        verbose_name="Program contact",
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="opportunities")
    notes = models.TextField(blank=True)
    extra = models.JSONField("Custom fields", default=dict, blank=True)
    added_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["deadline", "title"]
        verbose_name_plural = "opportunities"

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("grants:opportunity_detail", args=[self.pk])

    @property
    def upcoming_dates(self):
        today = timezone.localdate()
        items = [
            ("LOI", self.loi_deadline),
            ("Internal", self.internal_deadline),
            ("Sponsor", self.deadline),
        ]
        return [(label, d) for label, d in items if d and d >= today]

    @property
    def next_deadline(self):
        dates = self.upcoming_dates
        return min(dates, key=lambda x: x[1]) if dates else None


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


class Application(TimeStamped):
    class Status(models.TextChoices):
        IDEA = "idea", "Idea"
        PLANNING = "planning", "Planning"
        DRAFTING = "drafting", "In preparation"
        ROUTING = "routing", "Internal routing"
        SUBMITTED = "submitted", "Submitted"
        IN_REVIEW = "in_review", "Under review"
        REVIEWED = "reviewed", "Reviewed / scored"
        PENDING_AWARD = "pending_award", "Pending award"
        AWARDED = "awarded", "Awarded"
        NOT_FUNDED = "not_funded", "Not funded"
        WITHDRAWN = "withdrawn", "Withdrawn"
        NOT_PURSUED = "not_pursued", "Not pursued"

    PRE_SUBMISSION = [Status.IDEA, Status.PLANNING, Status.DRAFTING, Status.ROUTING]
    PENDING = [Status.SUBMITTED, Status.IN_REVIEW, Status.REVIEWED, Status.PENDING_AWARD]
    CLOSED = [Status.AWARDED, Status.NOT_FUNDED, Status.WITHDRAWN, Status.NOT_PURSUED]
    DECIDED = [Status.AWARDED, Status.NOT_FUNDED]

    class SubmissionType(models.TextChoices):
        NEW = "new", "New"
        RESUBMISSION = "resubmission", "Resubmission"
        RENEWAL = "renewal", "Renewal / competing continuation"
        REVISION = "revision", "Revision / supplement"
        PREPROPOSAL = "preproposal", "LOI / pre-proposal"
        INTERNAL = "internal", "Internal / pilot"
        OTHER = "other", "Other"

    class Role(models.TextChoices):
        PI = "pi", "PI (contact)"
        MPI = "mpi", "Multiple PI"
        CO_PI = "co_pi", "Co-PI"
        CO_I = "co_i", "Co-investigator"
        KEY = "key", "Senior / key personnel"
        MENTOR = "mentor", "Mentor / sponsor"
        CONSULTANT = "consultant", "Consultant / collaborator"

    class Priority(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    class ReviewOutcome(models.TextChoices):
        SCORED = "scored", "Discussed / scored"
        NOT_DISCUSSED = "not_discussed", "Not discussed"
        RECOMMENDED = "recommended", "Recommended for funding"
        NOT_RECOMMENDED = "not_recommended", "Not recommended"
        OTHER = "other", "Other"

    title = models.CharField(max_length=300)
    short_name = models.CharField("Nickname", max_length=60, blank=True, help_text="Short label used on boards and calendars")
    opportunity = models.ForeignKey(
        Opportunity, null=True, blank=True, on_delete=models.SET_NULL, related_name="applications"
    )
    funder = models.ForeignKey(Funder, null=True, blank=True, on_delete=models.SET_NULL, related_name="applications")
    sponsor_unit = models.CharField("Institute / directorate / program", max_length=120, blank=True)
    mechanism = models.CharField("Mechanism / activity code", max_length=40, blank=True)
    announcement_number = models.CharField(max_length=60, blank=True)
    submission_type = models.CharField(max_length=14, choices=SubmissionType.choices, default=SubmissionType.NEW)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="resubmissions",
        verbose_name="Previous submission",
    )
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.PLANNING, db_index=True)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.MEDIUM)
    role = models.CharField("My role", max_length=12, choices=Role.choices, default=Role.PI)
    contact_pi = models.CharField("Contact PI (if not me)", max_length=120, blank=True)
    lead_institution = models.CharField(max_length=200, blank=True)
    probability = models.PositiveSmallIntegerField(
        "Estimated chance of funding (%)", null=True, blank=True,
        help_text="Your gut estimate. Drives the weighted pipeline value.",
    )
    is_starred = models.BooleanField("Starred", default=False)

    loi_deadline = models.DateField("LOI / pre-proposal due", null=True, blank=True)
    internal_deadline = models.DateField("Internal deadline", null=True, blank=True,
                                         help_text="Institutional routing deadline")
    sponsor_deadline = models.DateField("Sponsor deadline", null=True, blank=True)
    submitted_on = models.DateField("Submitted on", null=True, blank=True)
    review_date = models.DateField("Review / study section date", null=True, blank=True)
    council_date = models.DateField("Council / decision expected", null=True, blank=True)
    decision_on = models.DateField("Decision received", null=True, blank=True)
    proposed_start = models.DateField(null=True, blank=True)
    proposed_end = models.DateField(null=True, blank=True)

    sponsor_id = models.CharField("Sponsor application ID", max_length=60, blank=True,
                                  help_text="e.g. 1R01HD123456-01A1")
    internal_id = models.CharField("Institutional proposal no.", max_length=60, blank=True)
    tracking_number = models.CharField("Portal tracking no.", max_length=60, blank=True,
                                       help_text="Grants.gov, Research.gov or foundation portal ID")

    requested_direct_y1 = models.DecimalField("Requested direct (year 1)", **MONEY)
    requested_direct_total = models.DecimalField("Requested direct (all years)", **MONEY)
    requested_total = models.DecimalField("Requested total (direct + F&A)", **MONEY)
    fa_rate = models.DecimalField("F&A rate (%)", max_digits=5, decimal_places=2, null=True, blank=True)
    duration_years = models.PositiveSmallIntegerField("Duration (years)", null=True, blank=True)

    review_panel = models.CharField("Study section / panel", max_length=120, blank=True)
    review_outcome = models.CharField(max_length=16, choices=ReviewOutcome.choices, blank=True)
    impact_score = models.DecimalField("Overall / impact score", max_digits=6, decimal_places=2, null=True, blank=True)
    percentile = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    payline_note = models.CharField("Payline / funding context", max_length=200, blank=True)

    abstract = models.TextField("Abstract / summary", blank=True)
    major_goals = models.TextField(
        "Major goals", blank=True, help_text="Used in Current & Pending / Other Support reports."
    )
    notes = models.TextField(blank=True)
    lessons_learned = models.TextField("Lessons learned / resubmission strategy", blank=True)

    folder_url = models.URLField("Working folder link", blank=True, max_length=500,
                                 help_text="Google Drive, OneDrive or Box folder")
    portal_url = models.URLField("Sponsor portal link", blank=True, max_length=500)
    program_officer = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="po_applications"
    )
    grants_admin = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="admin_applications",
        verbose_name="Grants administrator",
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="applications")
    extra = models.JSONField("Custom fields", default=dict, blank=True)
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    history = HistoricalRecords(m2m_fields=[tags])

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.display_title

    def get_absolute_url(self):
        return reverse("grants:application_detail", args=[self.pk])

    @property
    def display_title(self):
        return self.short_name or self.title

    @property
    def status_group(self):
        if self.status in self.PRE_SUBMISSION:
            return "preparing"
        if self.status in self.PENDING:
            return "pending"
        if self.status == self.Status.AWARDED:
            return "awarded"
        return "closed"

    @property
    def is_open(self):
        return self.status not in self.CLOSED

    @property
    def upcoming_deadlines(self):
        if self.status not in self.PRE_SUBMISSION:
            return []
        today = timezone.localdate()
        items = [("LOI", self.loi_deadline), ("Internal", self.internal_deadline), ("Sponsor", self.sponsor_deadline)]
        return [(label, d) for label, d in items if d and d >= today]

    @property
    def next_deadline(self):
        dates = self.upcoming_deadlines
        return min(dates, key=lambda x: x[1]) if dates else None

    @property
    def expected_value(self):
        if self.requested_total and self.probability is not None:
            return self.requested_total * Decimal(self.probability) / Decimal(100)
        return None

    @property
    def resubmission_label(self):
        """A0 for an original submission, A1 for its first resubmission, etc."""
        depth, node, seen = 0, self, set()
        while node.parent_id and node.parent_id not in seen and node.submission_type == self.SubmissionType.RESUBMISSION:
            seen.add(node.pk)
            depth += 1
            node = node.parent
        return f"A{depth}"

    def lineage(self):
        """All applications in this resubmission/renewal family, oldest first."""
        root, seen = self, set()
        while root.parent_id and root.parent_id not in seen:
            seen.add(root.pk)
            root = root.parent
        family, frontier = [root], [root]
        while frontier:
            children = list(Application.objects.filter(parent__in=frontier).order_by("submitted_on", "created_at"))
            children = [c for c in children if c not in family]
            family.extend(children)
            frontier = children
        return family


class StatusChange(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="status_changes")
    from_status = models.CharField(max_length=14, choices=Application.Status.choices, blank=True)
    to_status = models.CharField(max_length=14, choices=Application.Status.choices)
    changed_at = models.DateTimeField(default=timezone.now)
    changed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-changed_at"]


class Personnel(models.Model):
    class Role(models.TextChoices):
        PI = "pi", "PI / PD"
        MPI = "mpi", "Multiple PI"
        CO_PI = "co_pi", "Co-PI"
        CO_I = "co_i", "Co-investigator"
        KEY = "key", "Senior / key personnel"
        OSC = "osc", "Other significant contributor"
        POSTDOC = "postdoc", "Postdoctoral fellow"
        GRAD = "grad", "Graduate student"
        UNDERGRAD = "undergrad", "Undergraduate"
        STAFF = "staff", "Research staff / technician"
        CONSULTANT = "consultant", "Consultant"
        COLLABORATOR = "collaborator", "Collaborator"

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="personnel")
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="assignments")
    role = models.CharField(max_length=14, choices=Role.choices, default=Role.KEY)
    person_months = models.DecimalField(
        "Person-months / year", max_digits=4, decimal_places=2, null=True, blank=True,
        help_text="Calendar months per year. 12 = 100% effort, 1.2 = 10%.",
    )
    start_date = models.DateField(null=True, blank=True, help_text="Leave blank to use the project dates")
    end_date = models.DateField(null=True, blank=True)
    is_key = models.BooleanField("Key personnel", default=False)
    notes = models.CharField(max_length=300, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["application", "role", "person__last_name"]
        verbose_name_plural = "personnel"

    def __str__(self):
        return f"{self.person} on {self.application}"

    @property
    def effort_percent(self):
        if self.person_months is None:
            return None
        return (self.person_months / Decimal(12) * 100).quantize(Decimal("0.1"))

    def period(self):
        """Effective (start, end) for this commitment, falling back to award or proposed dates."""
        app = self.application
        award = getattr(app, "award", None)
        start = self.start_date or (award.start_date if award else None) or app.proposed_start
        end = self.end_date or (award.effective_end if award else None) or app.proposed_end
        return start, end


# ---------------------------------------------------------------------------
# Tasks and checklist templates
# ---------------------------------------------------------------------------


class Task(TimeStamped):
    class Category(models.TextChoices):
        WRITING = "writing", "Writing"
        BUDGET = "budget", "Budget"
        DOCUMENTS = "documents", "Documents & forms"
        LETTERS = "letters", "Letters & collaborators"
        ADMIN = "admin", "Routing & admin"
        REPORTING = "reporting", "Progress report"
        FINANCIAL = "financial", "Financial report"
        COMPLIANCE = "compliance", "Compliance (IRB / IACUC / DMS)"
        EFFORT = "effort", "Effort certification"
        PERSONNEL = "personnel", "Personnel / hiring"
        MEETING = "meeting", "Meeting / call"
        OTHER = "other", "Other"

    POST_AWARD = [Category.REPORTING, Category.FINANCIAL, Category.COMPLIANCE, Category.EFFORT, Category.PERSONNEL]

    class Status(models.TextChoices):
        TODO = "todo", "To do"
        IN_PROGRESS = "in_progress", "In progress"
        WAITING = "waiting", "Waiting on others"
        DONE = "done", "Done"
        SKIPPED = "skipped", "Not needed"

    OPEN = [Status.TODO, Status.IN_PROGRESS, Status.WAITING]

    class Priority(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    application = models.ForeignKey(
        Application, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks"
    )
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.OTHER)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TODO, db_index=True)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.MEDIUM)
    due_date = models.DateField(null=True, blank=True, db_index=True)
    assignee = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    auto_key = models.CharField(
        max_length=80, blank=True, db_index=True, editable=False,
        help_text="Set on tasks generated from award dates so schedules can be regenerated safely.",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = [models.F("due_date").asc(nulls_last=True), "title"]

    def __str__(self):
        return self.title

    @property
    def is_done(self):
        return self.status in (self.Status.DONE, self.Status.SKIPPED)

    @property
    def is_overdue(self):
        return bool(self.due_date and not self.is_done and self.due_date < timezone.localdate())

    @property
    def days_left(self):
        if not self.due_date:
            return None
        return (self.due_date - timezone.localdate()).days

    def mark(self, status, user=None):
        self.status = status
        if self.is_done:
            self.completed_at = self.completed_at or timezone.now()
            self.completed_by = self.completed_by or user
        else:
            self.completed_at = None
            self.completed_by = None


class ChecklistTemplate(TimeStamped):
    class AppliesTo(models.TextChoices):
        SUBMISSION = "submission", "Submission preparation"
        AWARD = "award", "Award setup"

    name = models.CharField(max_length=120, unique=True)
    description = models.CharField(max_length=300, blank=True)
    applies_to = models.CharField(max_length=12, choices=AppliesTo.choices, default=AppliesTo.SUBMISSION)
    mechanisms = models.CharField(
        max_length=200, blank=True, help_text="Comma-separated mechanisms this template suits, e.g. R01, R21"
    )
    is_default = models.BooleanField(
        default=False, help_text="Applied automatically (award templates run when an application is awarded)."
    )

    class Meta:
        ordering = ["applies_to", "name"]

    def __str__(self):
        return self.name


class ChecklistItem(models.Model):
    class Anchor(models.TextChoices):
        SPONSOR = "sponsor", "Sponsor deadline"
        INTERNAL = "internal", "Internal deadline"
        AWARD_START = "award_start", "Award start"
        AWARD_END = "award_end", "Award end"
        NONE = "none", "No due date"

    template = models.ForeignKey(ChecklistTemplate, on_delete=models.CASCADE, related_name="items")
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=12, choices=Task.Category.choices, default=Task.Category.WRITING)
    anchor = models.CharField(max_length=12, choices=Anchor.choices, default=Anchor.SPONSOR)
    offset_days = models.IntegerField(default=0, help_text="Negative = before the anchor date")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "offset_days", "id"]

    def __str__(self):
        return self.title


# ---------------------------------------------------------------------------
# Documents (stored in PostgreSQL)
# ---------------------------------------------------------------------------


class Document(TimeStamped):
    class Category(models.TextChoices):
        AIMS = "aims", "Specific Aims"
        STRATEGY = "strategy", "Research Strategy"
        SUMMARY = "summary", "Project Summary / Abstract"
        NARRATIVE = "narrative", "Project Narrative"
        INTRO = "intro", "Introduction to resubmission"
        BIOSKETCH = "biosketch", "Biosketch"
        BUDGET = "budget", "Budget"
        JUSTIFICATION = "justification", "Budget justification"
        LETTERS = "letters", "Letters of support"
        FACILITIES = "facilities", "Facilities & resources"
        EQUIPMENT = "equipment", "Equipment"
        DMS = "dms", "Data management & sharing plan"
        OTHER_SUPPORT = "other_support", "Other support / current & pending"
        PROTOCOLS = "protocols", "Animal / human subjects"
        FULL = "full", "Full application (assembled)"
        REVIEW = "review", "Summary statement / reviews"
        NOA = "noa", "Notice of award"
        JIT = "jit", "Just-in-time"
        PROGRESS = "progress", "Progress report"
        FINANCIAL = "financial", "Financial report"
        CORRESPONDENCE = "correspondence", "Correspondence"
        OTHER = "other", "Other"

    application = models.ForeignKey(
        Application, null=True, blank=True, on_delete=models.CASCADE, related_name="documents",
        help_text="Leave empty for library documents (e.g. a current biosketch).",
    )
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.OTHER)
    title = models.CharField(max_length=200)
    version = models.CharField(max_length=40, blank=True, help_text="e.g. v3, Final, A1")
    is_final = models.BooleanField("Final / as submitted", default=False)
    restricted = models.BooleanField(
        "Owner-only", default=False, help_text="Hide from editors and viewers (e.g. salary details)."
    )
    external_url = models.URLField("Link instead of upload", blank=True, max_length=500)
    filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    extracted_text = models.TextField(blank=True, editable=False)
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    history = HistoricalRecords(excluded_fields=["extracted_text"])

    class Meta:
        ordering = ["category", "-created_at"]

    def __str__(self):
        return self.title

    @property
    def has_file(self):
        return bool(self.filename)

    @property
    def extension(self):
        if "." in self.filename:
            return self.filename.rsplit(".", 1)[1].lower()
        return "link" if self.external_url else ""


class DocumentBlob(models.Model):
    """File bytes, split out so listing documents never loads file contents."""

    document = models.OneToOneField(Document, on_delete=models.CASCADE, primary_key=True, related_name="blob")
    data = models.BinaryField()


# ---------------------------------------------------------------------------
# Peer review feedback
# ---------------------------------------------------------------------------


class ReviewFeedback(TimeStamped):
    class Source(models.TextChoices):
        SUMMARY = "summary", "Summary statement / panel summary"
        REVIEWER = "reviewer", "Individual reviewer"
        PROGRAM = "program", "Program officer"
        INTERNAL = "internal", "Internal / mock review"
        OTHER = "other", "Other"

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="feedback")
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.REVIEWER)
    reviewer_label = models.CharField("Reviewer", max_length=60, blank=True, help_text="e.g. Reviewer 2, Panel")
    overall_score = models.CharField(max_length=30, blank=True, help_text="Number or rating, e.g. 3, Very Good")
    strengths = models.TextField(blank=True)
    weaknesses = models.TextField(blank=True)
    comments = models.TextField("Other comments", blank=True)
    response_plan = models.TextField("Planned response", blank=True)
    themes = models.ManyToManyField(
        Tag, blank=True, related_name="feedback", limit_choices_to={"kind": Tag.Kind.CRITIQUE},
        verbose_name="Critique themes",
    )
    document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Source document",
    )

    class Meta:
        ordering = ["application", "source", "reviewer_label"]
        verbose_name = "review feedback"
        verbose_name_plural = "review feedback"

    def __str__(self):
        return f"{self.get_source_display()} {self.reviewer_label}".strip()


class CriterionScore(models.Model):
    feedback = models.ForeignKey(ReviewFeedback, on_delete=models.CASCADE, related_name="criteria")
    criterion = models.CharField(max_length=80)
    score = models.CharField(max_length=30, blank=True)
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.criterion}: {self.score}"


# ---------------------------------------------------------------------------
# Awards
# ---------------------------------------------------------------------------


class Award(TimeStamped):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        NCE = "nce", "No-cost extension"
        ENDED = "ended", "Ended"
        CLOSING = "closing", "Close-out"
        CLOSED = "closed", "Closed"
        SUSPENDED = "suspended", "Suspended"
        TERMINATED = "terminated", "Terminated"

    ACTIVE_STATUSES = [Status.ACTIVE, Status.NCE]

    application = models.OneToOneField(Application, on_delete=models.CASCADE, related_name="award")
    award_number = models.CharField(max_length=60, blank=True, help_text="e.g. R01HD123456")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    notice_date = models.DateField("Notice of award date", null=True, blank=True)
    start_date = models.DateField("Project start", null=True, blank=True)
    end_date = models.DateField("Project end", null=True, blank=True)
    nce_end_date = models.DateField("Extended end (NCE)", null=True, blank=True)
    awarded_direct_total = models.DecimalField("Awarded direct (all years)", **MONEY)
    awarded_total = models.DecimalField("Awarded total (direct + F&A)", **MONEY)
    fa_rate = models.DecimalField("F&A rate (%)", max_digits=5, decimal_places=2, null=True, blank=True)
    account_number = models.CharField("Internal account / cost center", max_length=60, blank=True)
    reporting = models.CharField(
        "Reporting schedule", max_length=10, choices=Funder.Reporting.choices, default=Funder.Reporting.ANNUAL
    )
    grants_specialist = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="specialist_awards",
        verbose_name="Grants management specialist",
    )
    terms = models.TextField("Terms, restrictions & carry-over notes", blank=True)
    notes = models.TextField(blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.award_number or f"Award for {self.application}"

    def get_absolute_url(self):
        return reverse("grants:award_detail", args=[self.pk])

    @property
    def effective_end(self):
        return self.nce_end_date or self.end_date

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def days_remaining(self):
        end = self.effective_end
        if not end:
            return None
        return (end - timezone.localdate()).days

    @property
    def percent_elapsed(self):
        if not (self.start_date and self.effective_end):
            return None
        total = (self.effective_end - self.start_date).days or 1
        done = (timezone.localdate() - self.start_date).days
        return max(0, min(100, round(done * 100 / total)))

    def current_period(self, on=None):
        on = on or timezone.localdate()
        return self.periods.filter(start_date__lte=on, end_date__gte=on).first()


class BudgetPeriod(models.Model):
    class Status(models.TextChoices):
        PROJECTED = "projected", "Projected"
        AWARDED = "awarded", "Awarded (NoA received)"

    award = models.ForeignKey(Award, on_delete=models.CASCADE, related_name="periods")
    number = models.PositiveSmallIntegerField("Year")
    start_date = models.DateField()
    end_date = models.DateField()
    direct_costs = models.DecimalField(**MONEY)
    indirect_costs = models.DecimalField("F&A costs", **MONEY)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PROJECTED)
    noa_date = models.DateField("NoA date", null=True, blank=True)
    spent_to_date = models.DecimalField("Spent to date", **MONEY, help_text="Optional, from your financial reports")
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["award", "number"]
        constraints = [models.UniqueConstraint(fields=["award", "number"], name="unique_budget_year")]

    def __str__(self):
        return f"Year {self.number}"

    @property
    def total(self):
        if self.direct_costs is None and self.indirect_costs is None:
            return None
        return (self.direct_costs or 0) + (self.indirect_costs or 0)

    @property
    def is_current(self):
        today = timezone.localdate()
        return self.start_date <= today <= self.end_date

    @property
    def burn_percent(self):
        if self.spent_to_date is None or not self.total:
            return None
        return round(self.spent_to_date * 100 / self.total)


# ---------------------------------------------------------------------------
# Collaboration and audit
# ---------------------------------------------------------------------------


class Comment(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Activity(models.Model):
    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        EDITED = "edited", "Edited"
        STATUS = "status", "Status"
        TASK = "task", "Task"
        DOCUMENT = "document", "Document"
        REVIEW = "review", "Review"
        AWARD = "award", "Award"
        COMMENT = "comment", "Comment"
        PERSONNEL = "personnel", "Personnel"

    application = models.ForeignKey(
        Application, null=True, blank=True, on_delete=models.CASCADE, related_name="activities"
    )
    actor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    description = models.CharField(max_length=300)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "activities"

    def __str__(self):
        return self.description


def log_activity(application, user, kind, description):
    return Activity.objects.create(
        application=application,
        actor=user if getattr(user, "is_authenticated", False) else None,
        kind=kind,
        description=description[:300],
    )


def add_months(d: date, months: int) -> date:
    from dateutil.relativedelta import relativedelta

    return d + relativedelta(months=months)
