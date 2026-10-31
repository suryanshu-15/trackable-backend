from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SupplierViewSet, UnitOfMeasureViewSet, UoMConversionViewSet,
    MaterialCategoryViewSet, MaterialViewSet, WarehouseViewSet,
    StorageLocationViewSet, LabTestDefinitionViewSet,
    FactoryViewSet, DepartmentViewSet,
)

router = DefaultRouter()
router.register('suppliers', SupplierViewSet, basename='supplier')
router.register('units', UnitOfMeasureViewSet, basename='unit')
router.register('unit-conversions', UoMConversionViewSet, basename='unit-conversion')
router.register('material-categories', MaterialCategoryViewSet, basename='material-category')
router.register('materials', MaterialViewSet, basename='material')
router.register('warehouses', WarehouseViewSet, basename='warehouse')
router.register('locations', StorageLocationViewSet, basename='location')
router.register('test-definitions', LabTestDefinitionViewSet, basename='test-definition')
router.register('factories', FactoryViewSet, basename='factory')
router.register('departments', DepartmentViewSet, basename='department')

urlpatterns = [
    path('', include(router.urls)),
]
