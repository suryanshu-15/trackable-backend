"""
Laboratory models: LabSample, SamplePackage, LabTestRun, LabTestResult,
LabSampleAttachment, QualityDisposition, QualityHold.

Results and decisions are append-only history: a retest is a new LabTestRun,
a new decision is a new QualityDisposition, and nothing is deleted.
"""
import os
import uuid

from django.db import models
from django.conf import settings
from apps.core.mixins import TimeStampedModel, AuditedModel, UUIDModel
from apps.core.utils import generate_sample_id
from apps.master.models import Material, LabTestDefinition, UnitOfMeasure
from apps.inventory.models import Package


class SampleStatus(models.TextChoices):
    REQUESTED = 'REQUESTED', 'Requested (awaiting collection)'
    COLLECTED = 'COLLECTED', 'Collected'
    RECEIVED_BY_LAB = 'RECEIVED_BY_LAB', 'Received by Lab'
    TESTING = 'TESTING', 'Testing'
    RESULTS_SUBMITTED = 'RESULTS_SUBMITTED', 'Results awaiting review'
    RETEST_REQUIRED = 'RETEST_REQUIRED', 'Retest Required'
    COMPLETED = 'COMPLETED', 'Completed'
    CANCELLED = 'CANCELLED', 'Cancelled'


ACTIVE_SAMPLE_STATUSES = (
    SampleStatus.REQUESTED, SampleStatus.COLLECTED, SampleStatus.RECEIVED_BY_LAB,
    SampleStatus.TESTING, SampleStatus.RESULTS_SUBMITTED, SampleStatus.RETEST_REQUIRED,
)


class TestResultStatus(models.TextChoices):
    PASS = 'PASS', 'Pass'
    FAIL = 'FAIL', 'Fail'
    INCONCLUSIVE = 'INCONCLUSIVE', 'Inconclusive'


class TestRunStatus(models.TextChoices):
    OPEN = 'OPEN', 'Open (results being entered)'
    SUBMITTED = 'SUBMITTED', 'Submitted for review'
    REVIEWED = 'REVIEWED', 'Reviewed'


class DispositionDecision(models.TextChoices):
    RELEASED = 'RELEASED', 'Released / Approved'
    REJECTED = 'REJECTED', 'Rejected'
    ON_HOLD = 'ON_HOLD', 'On Hold'
    RETEST = 'RETEST', 'Retest Required'
    PARTIAL = 'PARTIAL', 'Partially Accepted'


class LabSample(UUIDModel, AuditedModel):
    """One sample, covering an explicit set of packages of one material / lot."""
    sample_id = models.CharField(max_length=50, unique=True, db_index=True)
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='samples')
    batch_number = models.CharField(max_length=200, blank=True, db_index=True)
    packages = models.ManyToManyField(Package, through='SamplePackage', related_name='samples')
    # Sampling details
    sampled_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    sampled_unit = models.ForeignKey(UnitOfMeasure, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    sampled_at = models.DateTimeField(null=True, blank=True)
    sampled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='samples_collected',
    )
    received_by_lab_at = models.DateTimeField(null=True, blank=True)
    # Lab details
    laboratory_name = models.CharField(max_length=200, blank=True)
    analyst_name = models.CharField(max_length=200, blank=True)
    sample_condition = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=SampleStatus.choices, default=SampleStatus.REQUESTED, db_index=True)
    requested_tests = models.ManyToManyField(LabTestDefinition, blank=True, related_name='+')
    specification = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    required_by = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)

    class Meta:
        db_table = 'laboratory_lab_sample'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.sample_id:
            self.sample_id = generate_sample_id()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sample_id} — {self.material.code}"

    @property
    def current_run(self):
        return self.test_runs.filter(is_current=True).first()


class SamplePackage(UUIDModel, TimeStampedModel):
    """
    Which packages a sample's result covers. `drawn_quantity` is set for packages
    the sample was physically taken from (deducted from stock via the ledger).
    """
    sample = models.ForeignKey(LabSample, on_delete=models.PROTECT, related_name='sample_packages')
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='sample_packages')
    drawn_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = 'laboratory_sample_package'
        unique_together = [('sample', 'package')]

    def __str__(self):
        return f"{self.sample.sample_id} / {self.package.package_id}"


class LabTestRun(UUIDModel, AuditedModel):
    """
    One testing attempt for a sample. A retest creates a new run; earlier runs
    (including failed ones) are kept with is_current=False.
    """
    sample = models.ForeignKey(LabSample, on_delete=models.PROTECT, related_name='test_runs')
    run_number = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=20, choices=TestRunStatus.choices, default=TestRunStatus.OPEN, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    analyst = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='test_runs',
    )
    notes = models.TextField(blank=True)
    is_current = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = 'laboratory_test_run'
        unique_together = [('sample', 'run_number')]
        ordering = ['sample', '-run_number']

    def __str__(self):
        return f"{self.sample.sample_id} / Run {self.run_number}"


class LabTestResult(UUIDModel, AuditedModel):
    """
    Result for one test definition within a run. Limits are snapshotted at entry.
    Corrections while the run is open bump `revision`; every revision is audited.
    """
    test_run = models.ForeignKey(LabTestRun, on_delete=models.PROTECT, related_name='results')
    test_definition = models.ForeignKey(LabTestDefinition, on_delete=models.PROTECT, related_name='+')
    numeric_result = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    text_result = models.CharField(max_length=500, blank=True)
    boolean_result = models.BooleanField(null=True, blank=True)
    result_unit = models.ForeignKey(UnitOfMeasure, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    min_limit = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    max_limit = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    acceptable_values = models.JSONField(default=list, blank=True)
    result_status = models.CharField(max_length=20, choices=TestResultStatus.choices, db_index=True)
    interpretation = models.TextField(blank=True)
    remarks = models.TextField(blank=True)
    revision = models.PositiveSmallIntegerField(default=1)
    entered_at = models.DateTimeField(auto_now_add=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='test_results',
    )

    class Meta:
        db_table = 'laboratory_test_result'
        unique_together = [('test_run', 'test_definition')]

    def __str__(self):
        return f"{self.test_run} / {self.test_definition.code}"


class LabDocumentType(models.TextChoices):
    REPORT = 'REPORT', 'Lab Report'
    COA = 'COA', 'Certificate of Analysis'
    OTHER = 'OTHER', 'Other'


def _lab_attachment_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    return f"lab/{instance.sample.sample_id}/{instance.document_type}-{uuid.uuid4().hex}{ext}"


class LabSampleAttachment(UUIDModel, TimeStampedModel):
    sample = models.ForeignKey(LabSample, on_delete=models.PROTECT, related_name='attachments')
    file = models.FileField(upload_to=_lab_attachment_path)
    original_filename = models.CharField(max_length=255)
    file_size_bytes = models.PositiveIntegerField()
    mime_type = models.CharField(max_length=100, blank=True)
    document_type = models.CharField(max_length=30, choices=LabDocumentType.choices, default=LabDocumentType.REPORT)
    notes = models.CharField(max_length=500, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )

    class Meta:
        db_table = 'laboratory_sample_attachment'


class QualityDisposition(UUIDModel, AuditedModel):
    """
    A quality decision on a sample. History is kept: a RETEST or ON_HOLD decision is
    followed by further dispositions. Package IDs affected are recorded explicitly.
    """
    sample = models.ForeignKey(LabSample, on_delete=models.PROTECT, related_name='dispositions')
    test_run = models.ForeignKey(LabTestRun, null=True, blank=True, on_delete=models.PROTECT, related_name='dispositions')
    decision = models.CharField(max_length=20, choices=DispositionDecision.choices, db_index=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='dispositions',
    )
    decided_at = models.DateTimeField()
    reason = models.TextField()
    notes = models.TextField(blank=True)
    # Explicit record of which packages received which QC status
    released_package_ids = models.JSONField(default=list, blank=True)
    affected_package_ids = models.JSONField(default=list, blank=True)
    remainder_decision = models.CharField(max_length=20, choices=DispositionDecision.choices, blank=True)
    new_retest_date = models.DateField(null=True, blank=True)
    # Expiry extended after a successful retest (business rule: expired stock may be re-released)
    new_expiry_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'laboratory_quality_disposition'
        ordering = ['-decided_at']

    def __str__(self):
        return f"{self.sample.sample_id} — {self.decision}"


class HoldType(models.TextChoices):
    HOLD = 'HOLD', 'Quality hold'
    RECALL = 'RECALL', 'Recall'


class QualityHold(UUIDModel, AuditedModel):
    """Quality hold or recall on packages, independent of sampling. Sets the package blocking flags."""
    hold_type = models.CharField(max_length=10, choices=HoldType.choices, default=HoldType.HOLD, db_index=True)
    reference = models.CharField(max_length=50, unique=True)
    packages = models.ManyToManyField(Package, related_name='quality_holds')
    # Optional lot scope used to select the packages (kept for traceability)
    material = models.ForeignKey(Material, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    batch_number = models.CharField(max_length=200, blank=True)
    reason = models.TextField()
    held_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+',
    )
    held_at = models.DateTimeField()
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    released_at = models.DateTimeField(null=True, blank=True)
    release_notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = 'laboratory_quality_hold'
        ordering = ['-held_at']

    def __str__(self):
        return f"{self.reference} ({self.hold_type})"
