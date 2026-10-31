from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    LabSampleViewSet, LabTestRunViewSet, QualityDispositionViewSet, QualityHoldViewSet,
    LabAttachmentDownloadView,
)

router = DefaultRouter()
router.register('samples', LabSampleViewSet, basename='sample')
router.register('test-runs', LabTestRunViewSet, basename='test-run')
router.register('dispositions', QualityDispositionViewSet, basename='disposition')
router.register('holds', QualityHoldViewSet, basename='hold')

urlpatterns = [
    path('', include(router.urls)),
    path('attachments/<uuid:pk>/download/', LabAttachmentDownloadView.as_view(), name='lab-attachment-download'),
]
