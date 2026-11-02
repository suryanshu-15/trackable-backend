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
from apps.core.audit import record_audit
from apps.core.config import get_config
from apps.core.models import AuditEvent
from apps.core.permissions import IsAdmin


class CanCreateMaster(permissions.BasePermission):
    """Roles listed in `master.create_roles` for this kind may add records from their forms."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        allowed = (get_config('master.create_roles') or {}).get(view.master_kind, ['ADMIN'])
        return user.is_superuser or user.role in allowed


class MasterWriteMixin:
    """Anyone signed in reads; permitted roles add; only administrators edit or delete. Additions are audited."""
    master_kind = None

    def get_permissions(self):
        if self.action == 'create':
            return [CanCreateMaster()]
        if self.action in ('update', 'partial_update', 'destroy'):
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]

    def perform_create(self, serializer):
        obj = serializer.save()
        record_audit(self.request.user, AuditEvent.ACTION_CREATE, type(obj).__name__, obj.pk,
                     after={'code': getattr(obj, 'code', ''), 'name': getattr(obj, 'name', '')},
                     extra={'master_data': self.master_kind}, request=self.request)


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
class SupplierViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'supplier'
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    search_fields = ['code', 'name', 'gst_number']
    filterset_fields = ['is_active']



@master_schema('uom')
class UnitOfMeasureViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'unit'
    queryset = UnitOfMeasure.objects.all()
    serializer_class = UnitOfMeasureSerializer
    filterset_fields = ['unit_type', 'is_active']



@master_schema('uom-conversion')
class UoMConversionViewSet(viewsets.ModelViewSet):
    queryset = UoMConversion.objects.select_related('from_unit', 'to_unit').all()
    serializer_class = UoMConversionSerializer
    permission_classes = [IsAdmin]


@master_schema('material-category')
class MaterialCategoryViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'category'
    queryset = MaterialCategory.objects.all()
    serializer_class = MaterialCategorySerializer



@master_schema('material')
class MaterialViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'material'
    queryset = Material.objects.select_related('category', 'default_unit').prefetch_related('test_requirements__test_definition').all()
    serializer_class = MaterialSerializer
    search_fields = ['code', 'name', 'hsn_code']
    filterset_fields = ['is_active', 'category', 'qc_required']



@master_schema('warehouse')
class WarehouseViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'warehouse'
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    filterset_fields = ['is_active']



@master_schema('storage-location')
class StorageLocationViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'location'
    queryset = StorageLocation.objects.select_related('warehouse', 'parent').all()
    serializer_class = StorageLocationSerializer
    search_fields = ['code', 'name']
    filterset_fields = ['warehouse', 'location_type', 'is_quarantine', 'is_active']



@master_schema('lab-test-definition')
class LabTestDefinitionViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'test_definition'
    queryset = LabTestDefinition.objects.select_related('unit').all()
    serializer_class = LabTestDefinitionSerializer
    filterset_fields = ['result_type', 'is_active', 'is_required']



@master_schema('factory')
class FactoryViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'factory'
    queryset = Factory.objects.all()
    serializer_class = FactorySerializer
    filterset_fields = ['is_active']



@master_schema('department')
class DepartmentViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'department'
    queryset = Department.objects.select_related('factory').all()
    serializer_class = DepartmentSerializer
    filterset_fields = ['factory', 'is_active']

