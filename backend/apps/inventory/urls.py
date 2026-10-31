from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    PackageViewSet, InventoryMovementViewSet, StockViewSet, StockAdjustmentViewSet, MaterialRequestViewSet,
    MaterialIssueViewSet, MaterialReturnViewSet, DisposalRecordViewSet,
)

router = DefaultRouter()
router.register('packages', PackageViewSet, basename='package')
router.register('movements', InventoryMovementViewSet, basename='movement')
router.register('stock', StockViewSet, basename='stock')
router.register('adjustments', StockAdjustmentViewSet, basename='adjustment')
router.register('requests', MaterialRequestViewSet, basename='material-request')
router.register('issues', MaterialIssueViewSet, basename='issue')
router.register('returns', MaterialReturnViewSet, basename='return')
router.register('disposals', DisposalRecordViewSet, basename='disposal')

urlpatterns = [
    path('', include(router.urls)),
]
