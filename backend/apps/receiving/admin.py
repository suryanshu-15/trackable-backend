from django.contrib import admin

from .models import GoodsReceipt, InvoiceLine, ReceiptDocument, ReceiptLineVerification


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    fields = ['line_number', 'material', 'material_name_declared', 'declared_quantity', 'declared_unit', 'batch_number']
    readonly_fields = fields
    can_delete = False


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(admin.ModelAdmin):
    """Read-only in admin: receipts change only through the audited API workflow."""
    list_display = ['receipt_number', 'supplier', 'invoice_number', 'vehicle_number', 'arrival_datetime', 'status']
    list_filter = ['status', 'supplier']
    search_fields = ['receipt_number', 'invoice_number', 'vehicle_number']
    inlines = [InvoiceLineInline]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReceiptDocument)
class ReceiptDocumentAdmin(admin.ModelAdmin):
    list_display = ['receipt', 'document_type', 'original_filename', 'file_size_bytes', 'uploaded_by', 'created_at']
    readonly_fields = [f.name for f in ReceiptDocument._meta.fields]

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(ReceiptLineVerification)
