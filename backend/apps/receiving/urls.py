from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    GoodsReceiptViewSet, InvoiceLineViewSet,
    ReceiptDocumentDownloadView, ReceiptLineVerificationViewSet,
)

router = DefaultRouter()
router.register('goods-receipts', GoodsReceiptViewSet, basename='goods-receipt')
router.register('invoice-lines', InvoiceLineViewSet, basename='invoice-line')
router.register('verifications', ReceiptLineVerificationViewSet, basename='verification')

urlpatterns = [
    path('', include(router.urls)),
    path(
        'documents/<uuid:pk>/download/',
        ReceiptDocumentDownloadView.as_view(),
        name='receipt-document-download',
    ),
]
