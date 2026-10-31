from django.db import connection
from django.http import JsonResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.config import PUBLIC_KEYS, get_config
from apps.core.models import AuditEvent
from apps.core.permissions import role_permission


def health(request):
    """Liveness + database reachability for load balancers / monitoring."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        db_ok = True
    except Exception:
        db_ok = False
    return JsonResponse({'status': 'ok' if db_ok else 'degraded', 'database': db_ok}, status=200 if db_ok else 503)



@extend_schema_view(list=extend_schema(tags=['audit']), retrieve=extend_schema(tags=['audit']))
class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only audit trail for administrators, auditors and managers."""
    permission_classes = [role_permission('ADMIN', 'AUDITOR', 'MANAGER')]
    queryset = AuditEvent.objects.select_related('actor')
    filterset_fields = {
        'action': ['exact'], 'entity_type': ['exact'], 'entity_id': ['exact'], 'actor': ['exact'],
        'timestamp': ['gte', 'lte'],
    }
    search_fields = ['entity_id', 'reason', 'actor__username', 'correlation_id']
    ordering = ['-timestamp']

    def get_serializer_class(self):
        from apps.receiving.serializers import AuditEventSerializer
        return AuditEventSerializer


class PolicyView(APIView):
    """Business rules the UI needs to shape its forms (e.g. whole-package issue). Enforcement stays server-side."""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(tags=['core'], responses=OpenApiTypes.OBJECT, operation_id='policy_retrieve')
    def get(self, request):
        return Response({key: get_config(key) for key in PUBLIC_KEYS})
