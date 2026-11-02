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
    # Write: the full list of lab tests this material needs (replaces the current set).
    test_ids = serializers.PrimaryKeyRelatedField(
        queryset=LabTestDefinition.objects.all(), many=True, write_only=True, required=False)

    class Meta:
        model = Material
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']

    def _sync_tests(self, material, tests):
        if tests is None:
            return
        wanted = {t.pk for t in tests}
        material.test_requirements.exclude(test_definition_id__in=wanted).delete()
        have = set(material.test_requirements.values_list('test_definition_id', flat=True))
        MaterialTestRequirement.objects.bulk_create(
            [MaterialTestRequirement(material=material, test_definition_id=pk) for pk in wanted - have])

    def create(self, validated_data):
        tests = validated_data.pop('test_ids', None)
        material = super().create(validated_data)
        self._sync_tests(material, tests)
        return material

    def update(self, instance, validated_data):
        tests = validated_data.pop('test_ids', None)
        material = super().update(instance, validated_data)
        self._sync_tests(material, tests)
        return material


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
