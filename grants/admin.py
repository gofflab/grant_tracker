from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Activity,
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
    StatusChange,
    Tag,
    Task,
)


class PersonnelInline(admin.TabularInline):
    model = Personnel
    extra = 0
    autocomplete_fields = ["person"]


@admin.register(Application)
class ApplicationAdmin(SimpleHistoryAdmin):
    list_display = ("display_title", "status", "funder", "mechanism", "sponsor_deadline", "submitted_on", "requested_total")
    list_filter = ("status", "funder", "submission_type", "role")
    search_fields = ("title", "short_name", "sponsor_id", "internal_id")
    autocomplete_fields = ["parent", "opportunity", "program_officer", "grants_admin"]
    filter_horizontal = ["tags"]
    inlines = [PersonnelInline]


@admin.register(Opportunity)
class OpportunityAdmin(SimpleHistoryAdmin):
    list_display = ("title", "funder", "mechanism", "deadline", "status")
    list_filter = ("status", "funder")
    search_fields = ("title", "number")


class BudgetPeriodInline(admin.TabularInline):
    model = BudgetPeriod
    extra = 0


@admin.register(Award)
class AwardAdmin(SimpleHistoryAdmin):
    list_display = ("__str__", "application", "status", "start_date", "end_date", "awarded_total")
    list_filter = ("status",)
    inlines = [BudgetPeriodInline]


@admin.register(Task)
class TaskAdmin(SimpleHistoryAdmin):
    list_display = ("title", "application", "category", "status", "due_date", "assignee")
    list_filter = ("status", "category")
    search_fields = ("title",)


@admin.register(Document)
class DocumentAdmin(SimpleHistoryAdmin):
    list_display = ("title", "application", "category", "filename", "size", "restricted", "created_at")
    list_filter = ("category", "restricted", "is_final")
    search_fields = ("title", "filename")
    exclude = ("extracted_text",)


class CriterionInline(admin.TabularInline):
    model = CriterionScore
    extra = 0


@admin.register(ReviewFeedback)
class ReviewFeedbackAdmin(admin.ModelAdmin):
    list_display = ("__str__", "application", "overall_score")
    inlines = [CriterionInline]


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 0


@admin.register(ChecklistTemplate)
class ChecklistTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "applies_to", "is_default")
    inlines = [ChecklistItemInline]


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("full_name", "kind", "institution", "email", "user")
    list_filter = ("kind",)
    search_fields = ("first_name", "last_name", "email")


admin.site.register(Funder)
admin.site.register(Tag)
admin.site.register(StatusChange)
admin.site.register(Comment)
admin.site.register(Activity)
