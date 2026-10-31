"""
URL configuration for Trackable.
"""
from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.core.search import GlobalSearchView
from apps.core.views import health, AuditEventViewSet, PolicyView

audit_router = DefaultRouter()
audit_router.include_root_view = False
audit_router.register('audit-events', AuditEventViewSet, basename='audit-event')
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health, name='health'),

    # API v1
    path('api/v1/', include([
        # Auth
        path('auth/', include('apps.users.urls')),
        # Master data
        path('master/', include('apps.master.urls')),
        # Receiving / Gate
        path('receiving/', include('apps.receiving.urls')),
        # Inventory / Packages
        path('inventory/', include('apps.inventory.urls')),
        # Laboratory / QC
        path('laboratory/', include('apps.laboratory.urls')),
        # Reports
        path('reports/', include('apps.reports.urls')),
        # Notifications
        path('notifications/', include('apps.notifications.urls')),
        # Global search & audit trail
        path('search/', GlobalSearchView.as_view(), name='global-search'),
        path('policy/', PolicyView.as_view(), name='policy'),
        path('', include(audit_router.urls)),
    ])),

    # OpenAPI schema
    path('api/v1/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/v1/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/v1/schema/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

# Uploaded media is deliberately NOT served by URL; files are downloaded through
# permission-checked, audited endpoints (e.g. receiving/documents/<id>/download/).
