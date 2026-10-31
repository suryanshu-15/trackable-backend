"""
Master data views.
"""
from rest_framework import viewsets, permissions
from drf_spectacular.utils import extend_schema_view, extend_schema

from .models import (
    Supplier, UnitOfMeasure, UoMConversion, MaterialCategory,
    Material, Warehouse, StorageLocation, LabTestDefinition,
    Factory, Department,
)
from .serializers import (
    SupplierSerializer, UnitOfMeasureSerializer, UoMConversionSerializer,
    MaterialCategorySerializer, MaterialSerializer, WarehouseSerializer,
    StorageLocationSerializer, LabTestDefinitionSerializer,
    FactorySerializer, DepartmentSerializer,
)
from apps.core.permissions import IsAdmin


def master_schema(name):
    return extend_schema_view(
        list=extend_schema(tags=['master']),
        create=extend_schema(tags=['master']),
        retrieve=extend_schema(tags=['master']),
        update=extend_schema(tags=['master']),
        partial_update=extend_schema(tags=['master']),
        destroy=extend_schema(tags=['master']),
    )


@master_schema('supplier')
class SupplierViewSet(viewsets.ModelViewSet):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    search_fields = ['code', 'name', 'gst_number']
    filterset_fields = ['is_active']

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]


@master_schema('uom')
class UnitOfMeasureViewSet(viewsets.ModelViewSet):
    queryset = UnitOfMeasure.objects.all()
    serializer_class = UnitOfMeasureSerializer
    filterset_fields = ['unit_type', 'is_active']

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]


@master_schema('uom-conversion')
class UoMConversionViewSet(viewsets.ModelViewSet):
    queryset = UoMConversion.objects.select_related('from_unit', 'to_unit').all()
    serializer_class = UoMConversionSerializer
    permission_classes = [IsAdmin]


@master_schema('material-category')
class MaterialCategoryViewSet(viewsets.ModelViewSet):
    queryset = MaterialCategory.objects.all()
    serializer_class = MaterialCategorySerializer

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]


@master_schema('material')
class MaterialViewSet(viewsets.ModelViewSet):
    queryset = Material.objects.select_related('category', 'default_unit').prefetch_related('test_requirements__test_definition').all()
    serializer_class = MaterialSerializer
    search_fields = ['code', 'name', 'hsn_code']
    filterset_fields = ['is_active', 'category', 'qc_required']

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]


@master_schema('warehouse')
class WarehouseViewSet(viewsets.ModelViewSet):
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    filterset_fields = ['is_active']

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]


@master_schema('storage-location')
class StorageLocationViewSet(viewsets.ModelViewSet):
    queryset = StorageLocation.objects.select_related('warehouse', 'parent').all()
    serializer_class = StorageLocationSerializer
    search_fields = ['code', 'name']
    filterset_fields = ['warehouse', 'location_type', 'is_quarantine', 'is_active']

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]


@master_schema('lab-test-definition')
class LabTestDefinitionViewSet(viewsets.ModelViewSet):
    queryset = LabTestDefinition.objects.select_related('unit').all()
    serializer_class = LabTestDefinitionSerializer
    filterset_fields = ['result_type', 'is_active', 'is_required']

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]


@master_schema('factory')
class FactoryViewSet(viewsets.ModelViewSet):
    queryset = Factory.objects.all()
    serializer_class = FactorySerializer
    filterset_fields = ['is_active']

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]


@master_schema('department')
class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = Department.objects.select_related('factory').all()
    serializer_class = DepartmentSerializer
    filterset_fields = ['factory', 'is_active']

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]
