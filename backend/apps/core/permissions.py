"""
Shared permissions for Trackable.
"""
from rest_framework.permissions import BasePermission


class RolePermission(BasePermission):
    """Allow access if user has any of the specified roles."""
    allowed_roles = []

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        return request.user.role in self.allowed_roles


def role_permission(*roles):
    """Factory: create a permission class allowing given roles."""
    return type('_RolePermission', (RolePermission,), {'allowed_roles': list(roles)})


class IsAdmin(RolePermission):
    allowed_roles = ['ADMIN']


class IsGateOrAbove(RolePermission):
    allowed_roles = ['ADMIN', 'GATE', 'MANAGER', 'AUDITOR']


class IsReceivingOrAbove(RolePermission):
    allowed_roles = ['ADMIN', 'RECEIVING', 'WAREHOUSE', 'MANAGER']


class IsWarehouseOrAbove(RolePermission):
    allowed_roles = ['ADMIN', 'WAREHOUSE', 'MANAGER']


class IsLabTech(RolePermission):
    allowed_roles = ['ADMIN', 'LAB_TECH', 'LAB_REVIEWER']


class IsLabReviewer(RolePermission):
    allowed_roles = ['ADMIN', 'LAB_REVIEWER']


class IsAuditorOrAbove(RolePermission):
    """Read-only auditor access."""
    allowed_roles = ['ADMIN', 'AUDITOR', 'MANAGER', 'LAB_REVIEWER']

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True
        # Allow all authenticated users to GET
        if request.method in ('GET', 'HEAD', 'OPTIONS') and request.user.is_authenticated:
            return True
        return False
