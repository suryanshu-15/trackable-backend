from django.contrib import admin

from .models import AuditEvent, SystemConfiguration


class ReadOnlyAdmin(admin.ModelAdmin):
    """For append-only records: visible in admin, never editable or deletable."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ['timestamp', 'action', 'entity_type', 'entity_id', 'actor', 'reason']
    list_filter = ['action', 'entity_type']
    search_fields = ['entity_id', 'reason', 'actor__username']
    date_hierarchy = 'timestamp'


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    list_display = ['key', 'value', 'updated_at', 'updated_by']
    search_fields = ['key']

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
