from django.contrib import admin

from .models import (
    Supplier, UnitOfMeasure, UoMConversion, MaterialCategory, Material,
    MaterialTestRequirement, Warehouse, StorageLocation, LabTestDefinition,
    Factory, Department,
)


class MaterialTestRequirementInline(admin.TabularInline):
    model = MaterialTestRequirement
    extra = 0


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'category', 'default_unit', 'qc_required', 'requires_expiry', 'is_active']
    list_filter = ['category', 'qc_required', 'requires_expiry', 'is_active']
    search_fields = ['code', 'name']
    inlines = [MaterialTestRequirementInline]


@admin.register(StorageLocation)
class StorageLocationAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'warehouse', 'parent', 'location_type', 'is_quarantine', 'is_active']
    list_filter = ['warehouse', 'location_type', 'is_quarantine', 'is_active']
    search_fields = ['code', 'name']


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'gst_number', 'is_active']
    search_fields = ['code', 'name', 'gst_number']


for model in (UnitOfMeasure, UoMConversion, MaterialCategory, Warehouse, LabTestDefinition, Factory, Department):
    admin.site.register(model)
