"""
Master data models: Supplier, Material, UnitOfMeasure, Warehouse,
StorageLocation, LabTestDefinition, Factory.
"""
from django.db import models
from apps.core.mixins import TimeStampedModel


class Supplier(TimeStampedModel):
    code = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=200, db_index=True)
    address = models.TextField(blank=True)
    contact_person = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    gst_number = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = 'master_supplier'
        ordering = ['name']

    def __str__(self):
        return f"{self.code} — {self.name}"


class UnitOfMeasure(TimeStampedModel):
    """Units like kg, tonne, litre, pcs, etc."""
    code = models.CharField(max_length=20, unique=True, db_index=True)
    name = models.CharField(max_length=50)
    symbol = models.CharField(max_length=10)
    unit_type = models.CharField(
        max_length=20,
        choices=[
            ('WEIGHT', 'Weight'),
            ('VOLUME', 'Volume'),
            ('COUNT', 'Count/Pieces'),
            ('LENGTH', 'Length'),
            ('AREA', 'Area'),
            ('OTHER', 'Other'),
        ],
        default='WEIGHT',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'master_unit_of_measure'
        ordering = ['unit_type', 'code']

    def __str__(self):
        return f"{self.symbol} ({self.name})"


class UoMConversion(TimeStampedModel):
    """Explicit unit conversion rules."""
    from_unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='conversions_from')
    to_unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='conversions_to')
    factor = models.DecimalField(max_digits=20, decimal_places=8)  # from * factor = to
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = 'master_uom_conversion'
        unique_together = [('from_unit', 'to_unit')]

    def __str__(self):
        return f"1 {self.from_unit.symbol} = {self.factor} {self.to_unit.symbol}"


class MaterialCategory(TimeStampedModel):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    requires_expiry = models.BooleanField(default=False)
    requires_retest = models.BooleanField(default=False)
    default_shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'master_material_category'
        verbose_name_plural = 'Material Categories'

    def __str__(self):
        return f"{self.code} — {self.name}"


class Material(TimeStampedModel):
    code = models.CharField(max_length=100, unique=True, db_index=True)
    name = models.CharField(max_length=200, db_index=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        MaterialCategory, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='materials',
    )
    default_unit = models.ForeignKey(
        UnitOfMeasure, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    hsn_code = models.CharField(max_length=50, blank=True)
    # Expiry / retest tracking
    requires_expiry = models.BooleanField(default=True)  # business rule: bags carry both expiry and retest dates
    requires_retest = models.BooleanField(default=True)
    shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    retest_interval_days = models.PositiveIntegerField(null=True, blank=True)
    # Quality control
    qc_required = models.BooleanField(default=True)
    # Whether a package may be issued partially (e.g. powder in bags) or only whole (e.g. sealed drums)
    allow_partial_issue = models.BooleanField(default=False)  # business rule: whole bags only
    # Stock thresholds
    min_stock_quantity = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True,
    )
    min_stock_unit = models.ForeignKey(
        UnitOfMeasure, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = 'master_material'
        ordering = ['code']

    def __str__(self):
        return f"{self.code} — {self.name}"


class Warehouse(TimeStampedModel):
    code = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'master_warehouse'

    def __str__(self):
        return self.name


class StorageLocation(TimeStampedModel):
    """Supports hierarchy: Warehouse → Zone → Rack → Shelf/Bin."""

    LOCATION_TYPE_CHOICES = [
        ('WAREHOUSE', 'Warehouse'),
        ('ZONE', 'Zone'),
        ('RACK', 'Rack'),
        ('SHELF', 'Shelf'),
        ('BIN', 'Bin'),
        ('QUARANTINE', 'Quarantine Area'),
        ('DISPATCH', 'Dispatch Area'),
    ]

    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='locations')
    parent = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.PROTECT, related_name='children',
    )
    code = models.CharField(max_length=100, db_index=True)
    name = models.CharField(max_length=200)
    location_type = models.CharField(max_length=20, choices=LOCATION_TYPE_CHOICES, default='BIN')
    is_quarantine = models.BooleanField(default=False, db_index=True)
    capacity_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    capacity_unit = models.ForeignKey(
        UnitOfMeasure, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'master_storage_location'
        unique_together = [('warehouse', 'code')]
        ordering = ['warehouse', 'code']

    def __str__(self):
        return f"{self.warehouse.code}/{self.code}"


class LabTestDefinition(TimeStampedModel):
    """Configurable test types for quality control."""

    RESULT_TYPE_CHOICES = [
        ('NUMERIC', 'Numeric (pass/fail by range)'),
        ('TEXT', 'Text / Qualitative'),
        ('BOOLEAN', 'Pass/Fail Boolean'),
    ]

    code = models.CharField(max_length=100, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    method = models.CharField(max_length=200, blank=True)
    result_type = models.CharField(max_length=20, choices=RESULT_TYPE_CHOICES, default='NUMERIC')
    unit = models.ForeignKey(
        UnitOfMeasure, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    min_value = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    max_value = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    acceptable_text_values = models.JSONField(default=list, blank=True)
    is_required = models.BooleanField(default=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = 'master_lab_test_definition'

    def __str__(self):
        return f"{self.code} — {self.name}"


class MaterialTestRequirement(TimeStampedModel):
    """Which tests are required for which material."""
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name='test_requirements')
    test_definition = models.ForeignKey(LabTestDefinition, on_delete=models.CASCADE, related_name='+')
    is_required = models.BooleanField(default=True)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = 'master_material_test_requirement'
        unique_together = [('material', 'test_definition')]

    def __str__(self):
        return f"{self.material.code} — {self.test_definition.code}"


class Factory(TimeStampedModel):
    code = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'master_factory'
        verbose_name_plural = 'Factories'

    def __str__(self):
        return self.name


class Department(TimeStampedModel):
    factory = models.ForeignKey(Factory, on_delete=models.CASCADE, related_name='departments')
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'master_department'
        unique_together = [('factory', 'code')]

    def __str__(self):
        return f"{self.factory.code}/{self.code}"
