import json

from django import forms
from django.forms import inlineformset_factory

from accounts.models import User

from .models import (
    Application,
    Award,
    BudgetPeriod,
    ChecklistItem,
    ChecklistTemplate,
    Comment,
    CriterionScore,
    Document,
    Funder,
    Opportunity,
    Person,
    Personnel,
    ReviewFeedback,
    Tag,
    Task,
)
from .services import ALLOWED_EXTENSIONS, file_extension, max_upload_bytes


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class StyledModelForm(forms.ModelForm):
    """Applies consistent widgets: native date pickers, compact textareas, money steps."""

    textarea_rows = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field, forms.DateField):
                field.widget = DateInput()
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("rows", self.textarea_rows)
            elif isinstance(field, forms.DecimalField):
                field.widget.attrs.setdefault("step", "any")
            if isinstance(field, forms.ModelChoiceField) and not isinstance(field, forms.ModelMultipleChoiceField):
                field.empty_label = "—"


class ExtraJSONMixin:
    """Edits the `extra` JSONField through the Alpine key/value editor (hidden input)."""

    def _setup_extra(self):
        if "extra" in self.fields:
            self.fields["extra"].widget = forms.HiddenInput()
            self.fields["extra"].required = False

    def clean_extra(self):
        value = self.cleaned_data.get("extra")
        if isinstance(value, dict):
            return {str(k)[:80]: str(v)[:500] for k, v in value.items()}
        if not value:
            return {}
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            raise forms.ValidationError("Custom fields could not be read.")
        return {str(k)[:80]: str(v)[:500] for k, v in data.items()} if isinstance(data, dict) else {}


class TagFieldMixin:
    """Topic tags as checkboxes plus a free-text box to create new tags."""

    def _setup_tags(self):
        if "tags" in self.fields:
            self.fields["tags"].queryset = Tag.objects.filter(kind=Tag.Kind.TOPIC)
            self.fields["tags"].widget = forms.CheckboxSelectMultiple()
            self.fields["tags"].required = False
        self.fields["new_tags"] = forms.CharField(
            required=False, label="Add tags", help_text="Comma-separated, e.g. cephalopod, neural stem cells"
        )

    def save_tags(self, instance):
        names = [n.strip() for n in (self.cleaned_data.get("new_tags") or "").split(",") if n.strip()]
        for name in names:
            tag, _ = Tag.objects.get_or_create(name__iexact=name, kind=Tag.Kind.TOPIC, defaults={"name": name[:60]})
            instance.tags.add(tag)


class OpportunityForm(TagFieldMixin, ExtraJSONMixin, StyledModelForm):
    class Meta:
        model = Opportunity
        exclude = ["added_by", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_extra()
        self._setup_tags()
        self.fields["contact"].queryset = Person.objects.filter(is_active=True)

    def save(self, commit=True):
        obj = super().save(commit=commit)
        if commit:
            self.save_tags(obj)
        return obj


class ApplicationForm(TagFieldMixin, ExtraJSONMixin, StyledModelForm):
    class Meta:
        model = Application
        exclude = ["created_by", "created_at", "updated_at", "status"]
        widgets = {"abstract": forms.Textarea(attrs={"rows": 5}), "major_goals": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_extra()
        self._setup_tags()
        self.fields["opportunity"].queryset = Opportunity.objects.select_related("funder").order_by("-deadline")
        people = Person.objects.filter(is_active=True)
        self.fields["program_officer"].queryset = people
        self.fields["grants_admin"].queryset = people
        parents = Application.objects.select_related("funder").order_by("-submitted_on", "-created_at")
        if self.instance.pk:
            parents = parents.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = parents
        if not self.instance.pk:
            self.fields["initial_status"] = forms.ChoiceField(
                choices=Application.Status.choices, initial=Application.Status.PLANNING, label="Status"
            )

    def clean(self):
        data = super().clean()
        ps, pe = data.get("proposed_start"), data.get("proposed_end")
        if ps and pe and pe <= ps:
            self.add_error("proposed_end", "Proposed end must be after the start.")
        p = data.get("probability")
        if p is not None and p > 100:
            self.add_error("probability", "Enter a percentage from 0 to 100.")
        return data

    def save(self, commit=True):
        obj = super().save(commit=commit)
        if commit:
            self.save_tags(obj)
        return obj


class StatusChangeForm(forms.Form):
    status = forms.ChoiceField(choices=Application.Status.choices)
    note = forms.CharField(required=False, max_length=300, widget=forms.TextInput(attrs={"placeholder": "Optional note"}))
    effective_date = forms.DateField(required=False, widget=DateInput(), label="Date")
    impact_score = forms.DecimalField(required=False, max_digits=6, decimal_places=2, label="Overall / impact score")
    percentile = forms.DecimalField(required=False, max_digits=5, decimal_places=1)
    review_outcome = forms.ChoiceField(required=False, choices=[("", "—")] + list(Application.ReviewOutcome.choices))


class TaskForm(StyledModelForm):
    class Meta:
        model = Task
        fields = ["title", "application", "category", "status", "priority", "due_date", "assignee", "description"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["application"].queryset = Application.objects.exclude(
            status__in=[Application.Status.NOT_PURSUED, Application.Status.WITHDRAWN]
        ).order_by("title")
        self.fields["application"].required = False
        self.fields["assignee"].queryset = User.objects.filter(is_active=True)


class DocumentForm(StyledModelForm):
    upload = forms.FileField(required=False, label="File")

    class Meta:
        model = Document
        fields = ["title", "category", "application", "version", "is_final", "restricted", "external_url", "notes"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["application"].queryset = Application.objects.order_by("title")
        self.fields["application"].required = False
        self.fields["title"].required = False
        self.fields["title"].help_text = "Defaults to the file name"
        if user is not None and not user.is_owner:
            del self.fields["restricted"]
        exts = ", ".join(sorted(ALLOWED_EXTENSIONS))
        self.fields["upload"].help_text = f"Up to {max_upload_bytes() // (1024 * 1024)} MB. Allowed: {exts}"
        self.fields["upload"].widget.attrs["accept"] = ",".join("." + e for e in sorted(ALLOWED_EXTENSIONS))

    def clean_upload(self):
        f = self.cleaned_data.get("upload")
        if not f:
            return f
        if file_extension(f.name) not in ALLOWED_EXTENSIONS:
            raise forms.ValidationError("That file type isn't allowed.")
        if f.size > max_upload_bytes():
            raise forms.ValidationError(f"File is larger than {max_upload_bytes() // (1024 * 1024)} MB.")
        return f

    def clean(self):
        data = super().clean()
        has_existing = bool(self.instance.pk and (self.instance.filename or self.instance.external_url))
        if not data.get("upload") and not data.get("external_url") and not has_existing:
            raise forms.ValidationError("Upload a file or provide a link.")
        if not data.get("title"):
            if data.get("upload"):
                data["title"] = data["upload"].name.rsplit(".", 1)[0][:200]
            elif data.get("external_url"):
                data["title"] = data["external_url"].rstrip("/").rsplit("/", 1)[-1][:200] or "Linked document"
            elif self.instance.pk:
                data["title"] = self.instance.title
        return data


class PersonnelForm(StyledModelForm):
    new_first_name = forms.CharField(required=False, label="First name")
    new_last_name = forms.CharField(required=False, label="Last name")
    new_kind = forms.ChoiceField(
        required=False, label="Type", initial=Person.Kind.LAB,
        choices=[c for c in Person.Kind.choices if c[0] != Person.Kind.LAB_PI],
    )

    class Meta:
        model = Personnel
        fields = ["person", "role", "person_months", "is_key", "start_date", "end_date", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.filter(is_active=True)
        self.fields["person"].required = False

    def clean(self):
        data = super().clean()
        if not data.get("person") and not (data.get("new_first_name") and data.get("new_last_name")):
            raise forms.ValidationError("Choose a person or enter a new person's first and last name.")
        pm = data.get("person_months")
        if pm is not None and (pm < 0 or pm > 12):
            self.add_error("person_months", "Person-months per year must be between 0 and 12.")
        return data

    def save(self, commit=True):
        if not self.cleaned_data.get("person"):
            self.instance.person = Person.objects.create(
                first_name=self.cleaned_data["new_first_name"],
                last_name=self.cleaned_data["new_last_name"],
                kind=self.cleaned_data.get("new_kind") or Person.Kind.LAB,
            )
        return super().save(commit=commit)


class ReviewFeedbackForm(StyledModelForm):
    textarea_rows = 4

    class Meta:
        model = ReviewFeedback
        fields = ["source", "reviewer_label", "overall_score", "strengths", "weaknesses", "comments", "response_plan", "themes", "document"]

    def __init__(self, *args, application=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["themes"].queryset = Tag.objects.filter(kind=Tag.Kind.CRITIQUE)
        self.fields["themes"].widget = forms.CheckboxSelectMultiple()
        self.fields["themes"].required = False
        docs = Document.objects.none()
        if application is not None:
            docs = application.documents.all()
        self.fields["document"].queryset = docs
        self.fields["new_themes"] = forms.CharField(required=False, label="Add critique themes", help_text="Comma-separated")

    def save(self, commit=True):
        obj = super().save(commit=commit)
        if commit:
            for name in [n.strip() for n in (self.cleaned_data.get("new_themes") or "").split(",") if n.strip()]:
                tag, _ = Tag.objects.get_or_create(
                    name__iexact=name, kind=Tag.Kind.CRITIQUE, defaults={"name": name[:60], "color": Tag.Color.GRAY}
                )
                obj.themes.add(tag)
        return obj


CriterionFormSet = inlineformset_factory(
    ReviewFeedback, CriterionScore, fields=["criterion", "score", "comment"], extra=0, can_delete=True,
    widgets={
        "criterion": forms.TextInput(attrs={"list": "criteria-suggestions", "placeholder": "e.g. Approach"}),
        "score": forms.TextInput(attrs={"placeholder": "e.g. 3"}),
        "comment": forms.TextInput(attrs={"placeholder": "Key point (optional)"}),
    },
)


class AwardForm(StyledModelForm):
    generate_schedule = forms.BooleanField(
        required=False, initial=True, label="Generate budget years and reporting schedule",
        help_text="Creates annual budget periods and progress/final report tasks from the project dates. "
                  "Completed tasks are never changed.",
    )

    class Meta:
        model = Award
        exclude = ["application", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["grants_specialist"].queryset = Person.objects.filter(is_active=True)

    def clean(self):
        data = super().clean()
        s, e = data.get("start_date"), data.get("end_date")
        if s and e and e <= s:
            self.add_error("end_date", "Project end must be after the start.")
        nce = data.get("nce_end_date")
        if nce and e and nce <= e:
            self.add_error("nce_end_date", "Extended end must be after the original end date.")
        return data


class BudgetPeriodForm(StyledModelForm):
    class Meta:
        model = BudgetPeriod
        exclude = ["award"]

    def clean(self):
        data = super().clean()
        if data.get("start_date") and data.get("end_date") and data["end_date"] <= data["start_date"]:
            self.add_error("end_date", "End must be after start.")
        return data


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 2, "placeholder": "Add a note for the team… (Markdown supported)"})}
        labels = {"body": ""}


class FunderForm(StyledModelForm):
    class Meta:
        model = Funder
        exclude = ["created_at", "updated_at"]


class PersonForm(StyledModelForm):
    class Meta:
        model = Person
        exclude = ["created_at", "updated_at"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        kind = self.fields["kind"]
        kind.help_text = (
            "Lab PI is you, the head of this lab: one person only, linked to an Owner login. "
            "Use Principal investigator for other labs' PIs (e.g. MPI partners)."
        )
        if user is not None and not user.is_owner:
            if self.instance.pk and self.instance.is_lab_pi:
                kind.disabled = True
                self.fields["user"].disabled = True
            else:
                kind.choices = [c for c in kind.choices if c[0] != Person.Kind.LAB_PI]

    def clean(self):
        data = super().clean()
        if data.get("kind") == Person.Kind.LAB_PI:
            other = Person.objects.filter(kind=Person.Kind.LAB_PI).exclude(pk=self.instance.pk).first()
            if other:
                self.add_error("kind", f"{other} is already the Lab PI. Change their type first.")
            linked = data.get("user")
            if linked is not None and not linked.is_owner:
                self.add_error("user", "The Lab PI must be linked to an Owner account.")
        return data


class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ["name", "kind", "color"]


class ChecklistTemplateForm(StyledModelForm):
    class Meta:
        model = ChecklistTemplate
        exclude = ["created_at", "updated_at"]


ChecklistItemFormSet = inlineformset_factory(
    ChecklistTemplate, ChecklistItem, fields=["title", "category", "anchor", "offset_days", "order"],
    extra=0, can_delete=True,
    widgets={"offset_days": forms.NumberInput(attrs={"style": "width:90px"}), "order": forms.NumberInput(attrs={"style": "width:70px"})},
)


class ApplyChecklistForm(forms.Form):
    template = forms.ModelChoiceField(queryset=ChecklistTemplate.objects.all(), empty_label=None)


class ImportForm(forms.Form):
    csv_file = forms.FileField(label="CSV file", help_text="UTF-8 CSV with a header row. Download the template for column names.")
    dry_run = forms.BooleanField(required=False, initial=True, label="Preview only (don't save)")


class OpportunityImportForm(forms.Form):
    source = forms.CharField(
        required=False, max_length=1000, label="Link or announcement number",
        widget=forms.TextInput(attrs={"placeholder": "https://grants.nih.gov/grants/guide/pa-files/PAR-25-131.html  or  PAR-25-131", "autofocus": True}),
    )
    upload = forms.FileField(required=False, label="Or upload the announcement",
                             help_text="PDF, Word, HTML or text file, up to 25 MB")
    pasted = forms.CharField(required=False, label="Or paste its text",
                             widget=forms.Textarea(attrs={"rows": 6, "placeholder": "Paste the announcement or the page text here"}))
    use_ai = forms.BooleanField(required=False, initial=True, label="Use AI to read it (Claude)")

    IMPORT_EXTENSIONS = (".pdf", ".docx", ".html", ".htm", ".txt", ".md")

    def clean_upload(self):
        f = self.cleaned_data.get("upload")
        if f:
            if not f.name.lower().endswith(self.IMPORT_EXTENSIONS):
                raise forms.ValidationError("Upload a PDF, Word, HTML or text file.")
            if f.size > 25 * 1024 * 1024:
                raise forms.ValidationError("That file is larger than 25 MB.")
        return f

    def clean(self):
        data = super().clean()
        if not (data.get("source", "").strip() or data.get("upload") or data.get("pasted", "").strip()):
            raise forms.ValidationError("Paste a link or announcement number, upload the announcement, or paste its text.")
        return data
