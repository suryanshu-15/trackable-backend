"""
User management views.
"""
from django.db import transaction
from rest_framework import viewsets, generics, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView
from drf_spectacular.utils import extend_schema, extend_schema_view

from .models import User, Role
from .serializers import (
    UserSerializer, UserCreateSerializer, UserUpdateSerializer,
    ChangePasswordSerializer, TrackableTokenObtainPairSerializer,
)
from apps.core.audit import record_audit
from apps.core.models import AuditEvent
from apps.core.permissions import IsAdmin


class TrackableTokenObtainPairView(TokenObtainPairView):
    """Login — returns JWT access + refresh + user info."""
    serializer_class = TrackableTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    @extend_schema(tags=['auth'])
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class MeView(generics.RetrieveUpdateAPIView):
    """Current authenticated user profile."""
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return UserUpdateSerializer
        return UserSerializer

    def get_object(self):
        return self.request.user

    @extend_schema(tags=['auth'])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(tags=['auth'])
    def patch(self, request, *args, **kwargs):
        return super().patch(request, *args, **kwargs)

    @extend_schema(tags=['auth'])
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)


class ChangePasswordView(generics.GenericAPIView):
    """Change own password."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ChangePasswordSerializer

    @extend_schema(tags=['auth'])
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data['old_password']):
            return Response(
                {'error': 'bad_request', 'message': 'Old password is incorrect.', 'details': {}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.set_password(serializer.validated_data['new_password'])
        user.save()
        return Response({'message': 'Password changed successfully.'})


@extend_schema_view(
    list=extend_schema(tags=['users']),
    create=extend_schema(tags=['users']),
    retrieve=extend_schema(tags=['users']),
    update=extend_schema(tags=['users']),
    partial_update=extend_schema(tags=['users']),
)
class UserViewSet(viewsets.ModelViewSet):
    """User CRUD — admin only. Users are deactivated, never deleted (they own history)."""
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']
    queryset = User.objects.all().order_by('username')
    permission_classes = [IsAdmin]
    search_fields = ['username', 'email', 'first_name', 'last_name', 'employee_id']
    filterset_fields = ['role', 'is_active']

    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        if self.action in ('update', 'partial_update'):
            return UserUpdateSerializer
        return UserSerializer

    @action(detail=True, methods=['post'], url_path='set-role')
    def set_role(self, request, pk=None):
        """Change a user's role."""
        user = self.get_object()
        role = request.data.get('role')
        if role not in Role.values:
            return Response(
                {'error': 'bad_request', 'message': f'Invalid role. Valid roles: {Role.values}', 'details': {}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        before = user.role
        with transaction.atomic():
            user.role = role
            user.save(update_fields=['role'])
            record_audit(request.user, AuditEvent.ACTION_UPDATE, 'User', user.id,
                         reason=request.data.get('reason', ''), before={'role': before}, after={'role': role},
                         request=request)
        return Response(UserSerializer(user).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        user = self.get_object()
        if user == request.user:
            return Response(
                {'error': 'bad_request', 'message': 'You cannot deactivate your own account.', 'details': {}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            user.is_active = False
            user.save(update_fields=['is_active'])
            record_audit(request.user, AuditEvent.ACTION_UPDATE, 'User', user.id,
                         after={'is_active': False}, request=request)
        return Response({'message': 'User deactivated.'})

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        user = self.get_object()
        with transaction.atomic():
            user.is_active = True
            user.save(update_fields=['is_active'])
            record_audit(request.user, AuditEvent.ACTION_UPDATE, 'User', user.id,
                         after={'is_active': True}, request=request)
        return Response({'message': 'User activated.'})
