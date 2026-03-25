from django.contrib import admin

from .models import CorrectiveAction, Defect, RootCause


@admin.register(Defect)
class DefectAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "severity", "status", "assignee", "created_at", "updated_at")
    list_filter = ("severity", "status", "created_at")
    search_fields = ("title", "description")
    raw_id_fields = ("assignee", "reported_by")


@admin.register(RootCause)
class RootCauseAdmin(admin.ModelAdmin):
    list_display = ("id", "defect", "status", "identified_by", "identified_at", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("summary", "analysis")
    raw_id_fields = ("defect", "identified_by")


@admin.register(CorrectiveAction)
class CorrectiveActionAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "defect", "status", "owner", "due_date", "completed_at")
    list_filter = ("status", "due_date", "created_at")
    search_fields = ("title", "description")
    raw_id_fields = ("defect", "root_cause", "owner")
