from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin
from .models import Package, InventoryMovement, PackageLabelEvent


@admin.register(Package)
class PackageAdmin(ReadOnlyAdmin):
    list_display = ['package_id', 'material', 'current_quantity', 'unit', 'status', 'qc_status', 'current_location']
    list_filter = ['status', 'qc_status']
    search_fields = ['package_id', 'batch_number']


@admin.register(InventoryMovement)
class InventoryMovementAdmin(ReadOnlyAdmin):
    list_display = ['created_at', 'package', 'reason', 'from_location', 'to_location', 'quantity', 'performed_by']
    list_filter = ['reason']


@admin.register(PackageLabelEvent)
class PackageLabelEventAdmin(ReadOnlyAdmin):
    list_display = ['created_at', 'package', 'event_type', 'printed_by', 'reprint_reason']
