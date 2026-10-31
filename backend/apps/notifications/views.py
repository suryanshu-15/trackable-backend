from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view

from .models import Notification
from .serializers import NotificationSerializer


@extend_schema_view(
    list=extend_schema(tags=['notifications']),
    retrieve=extend_schema(tags=['notifications']),
)
class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer
    queryset = Notification.objects.none()  # schema introspection; real queryset is per-user
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['is_read', 'level', 'entity_type']
    ordering = ['-created_at']

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user)

    @action(detail=True, methods=['post'], url_path='mark-read')
    @extend_schema(tags=['notifications'])
    def mark_read(self, request, pk=None):
        n = self.get_object()
        n.is_read = True
        n.read_at = timezone.now()
        n.save(update_fields=['is_read', 'read_at'])
        return Response({'message': 'Marked as read.'})

    @action(detail=False, methods=['post'], url_path='mark-all-read')
    @extend_schema(tags=['notifications'])
    def mark_all_read(self, request):
        self.get_queryset().filter(is_read=False).update(is_read=True, read_at=timezone.now())
        return Response({'message': 'All notifications marked as read.'})

    @action(detail=False, methods=['get'], url_path='unread-count')
    @extend_schema(tags=['notifications'])
    def unread_count(self, request):
        count = self.get_queryset().filter(is_read=False).count()
        return Response({'unread_count': count})
