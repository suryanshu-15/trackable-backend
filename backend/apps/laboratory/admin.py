from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin
from .models import LabSample, LabTestRun, LabTestResult, QualityDisposition, QualityHold


@admin.register(LabSample)
class LabSampleAdmin(ReadOnlyAdmin):
    """Read-only: samples change only through the audited API workflow."""
    list_display = ['sample_id', 'material', 'batch_number', 'status', 'required_by', 'created_at']
    list_filter = ['status']
    search_fields = ['sample_id', 'batch_number']


@admin.register(LabTestRun)
class LabTestRunAdmin(ReadOnlyAdmin):
    list_display = ['sample', 'run_number', 'status', 'is_current', 'submitted_at', 'reviewed_at']


@admin.register(LabTestResult)
class LabTestResultAdmin(ReadOnlyAdmin):
    list_display = ['test_run', 'test_definition', 'result_status', 'revision', 'entered_by']


@admin.register(QualityDisposition)
class QualityDispositionAdmin(ReadOnlyAdmin):
    list_display = ['sample', 'decision', 'decided_by', 'decided_at']


@admin.register(QualityHold)
class QualityHoldAdmin(ReadOnlyAdmin):
    list_display = ['reference', 'hold_type', 'is_active', 'held_by', 'held_at']
