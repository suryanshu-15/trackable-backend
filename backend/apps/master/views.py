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


def _manage_roles():
    return get_config('master.manage_roles') or ['ADMIN']


class CanCreateMaster(permissions.BasePermission):
    """Master-data maintainers, plus roles listed in `master.create_roles` for this kind, may add records."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        allowed = set(_manage_roles()) | set((get_config('master.create_roles') or {}).get(view.master_kind, []))
        return user.is_superuser or user.role in allowed


class CanManageMaster(permissions.BasePermission):
    """Edit, activate / deactivate and delete: roles in `master.manage_roles` (default admin + manager)."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_superuser or user.role in _manage_roles()))


class MasterWriteMixin:
    """Anyone signed in reads; permitted roles add; maintainers edit or delete. All changes are audited."""
    master_kind = None

    def get_permissions(self):
        if self.action == 'create':
            return [CanCreateMaster()]
        if self.action in ('update', 'partial_update', 'destroy'):
            return [CanManageMaster()]
        return [permissions.IsAuthenticated()]

    def _snapshot(self, obj):
        return {f.name: str(getattr(obj, f.attname)) for f in obj._meta.concrete_fields
                if f.name not in ('created_at', 'updated_at')}

    def perform_update(self, serializer):
        before = self._snapshot(serializer.instance)
        obj = serializer.save()
        after = self._snapshot(obj)
        changed = {k for k in after if after[k] != before.get(k)}
        record_audit(self.request.user, AuditEvent.ACTION_UPDATE, type(obj).__name__, obj.pk,
                     before={k: before[k] for k in changed}, after={k: after[k] for k in changed},
                     extra={'master_data': self.master_kind}, request=self.request)

    def perform_destroy(self, instance):
        snapshot = self._snapshot(instance)
        pk, name = instance.pk, type(instance).__name__
        instance.delete()  # ProtectedError (record in use) becomes a 409 telling the user to deactivate instead
        record_audit(self.request.user, AuditEvent.ACTION_DELETE, name, pk, before=snapshot,
                     extra={'master_data': self.master_kind}, request=self.request)

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
    search_fields = ['code', 'name', 'contact_person', 'phone', 'email', 'gst_number', 'address', 'notes']
    filterset_fields = ['is_active']



@master_schema('uom')
class UnitOfMeasureViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'unit'
    search_fields = ['code', 'name', 'symbol', 'unit_type']
    queryset = UnitOfMeasure.objects.all()
    serializer_class = UnitOfMeasureSerializer
    filterset_fields = ['unit_type', 'is_active']



@master_schema('uom-conversion')
class UoMConversionViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'unit_conversion'
    search_fields = ['from_unit__code', 'from_unit__symbol', 'to_unit__code', 'to_unit__symbol', 'notes']
    queryset = UoMConversion.objects.select_related('from_unit', 'to_unit').all()
    serializer_class = UoMConversionSerializer


@master_schema('material-category')
class MaterialCategoryViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'category'
    search_fields = ['code', 'name', 'notes']
    queryset = MaterialCategory.objects.all()
    serializer_class = MaterialCategorySerializer



@master_schema('material')
class MaterialViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'material'
    queryset = Material.objects.select_related('category', 'default_unit').prefetch_related('test_requirements__test_definition').all()
    serializer_class = MaterialSerializer
    search_fields = ['code', 'name', 'description', 'hsn_code', 'notes', 'category__name', 'default_unit__symbol', 'test_requirements__test_definition__code', 'test_requirements__test_definition__name']
    filterset_fields = ['is_active', 'category', 'qc_required']



@master_schema('warehouse')
class WarehouseViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'warehouse'
    search_fields = ['code', 'name', 'address']
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    filterset_fields = ['is_active']



@master_schema('storage-location')
class StorageLocationViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'location'
    queryset = StorageLocation.objects.select_related('warehouse', 'parent').all()
    serializer_class = StorageLocationSerializer
    search_fields = ['code', 'name', 'location_type', 'notes', 'warehouse__code', 'warehouse__name', 'parent__code']
    filterset_fields = ['warehouse', 'location_type', 'is_quarantine', 'is_active']



@master_schema('lab-test-definition')
class LabTestDefinitionViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'test_definition'
    search_fields = ['code', 'name', 'method', 'description', 'result_type', 'unit__symbol']
    queryset = LabTestDefinition.objects.select_related('unit').all()
    serializer_class = LabTestDefinitionSerializer
    filterset_fields = ['result_type', 'is_active', 'is_required']



@master_schema('factory')
class FactoryViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'factory'
    search_fields = ['code', 'name']
    queryset = Factory.objects.all()
    serializer_class = FactorySerializer
    filterset_fields = ['is_active']



@master_schema('department')
class DepartmentViewSet(MasterWriteMixin, viewsets.ModelViewSet):
    master_kind = 'department'
    search_fields = ['code', 'name', 'factory__code', 'factory__name']
    queryset = Department.objects.select_related('factory').all()
    serializer_class = DepartmentSerializer
    filterset_fields = ['factory', 'is_active']

