"""
Serializers for master data.
"""
from rest_framework import serializers
from .models import (
    Supplier, UnitOfMeasure, UoMConversion, MaterialCategory,
    Material, MaterialTestRequirement, Warehouse, StorageLocation,
    LabTestDefinition, Factory, Department,
)


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class UnitOfMeasureSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitOfMeasure
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class UoMConversionSerializer(serializers.ModelSerializer):
    from_unit_display = serializers.CharField(source='from_unit.symbol', read_only=True)
    to_unit_display = serializers.CharField(source='to_unit.symbol', read_only=True)

    class Meta:
        model = UoMConversion
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class MaterialCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = MaterialCategory
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class MaterialTestRequirementSerializer(serializers.ModelSerializer):
    test_name = serializers.CharField(source='test_definition.name', read_only=True)

    class Meta:
        model = MaterialTestRequirement
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class MaterialSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    default_unit_symbol = serializers.CharField(source='default_unit.symbol', read_only=True)
    test_requirements = MaterialTestRequirementSerializer(many=True, read_only=True)

    class Meta:
        model = Material
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class StorageLocationSerializer(serializers.ModelSerializer):
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    full_path = serializers.SerializerMethodField()

    class Meta:
        model = StorageLocation
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']

    def get_full_path(self, obj) -> str:
        parts = [obj.code]
        parent = obj.parent
        while parent:
            parts.insert(0, parent.code)
            parent = parent.parent
        return ' > '.join(parts)


class LabTestDefinitionSerializer(serializers.ModelSerializer):
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)

    class Meta:
        model = LabTestDefinition
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class FactorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Factory
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class DepartmentSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source='factory.name', read_only=True)

    class Meta:
        model = Department
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']
