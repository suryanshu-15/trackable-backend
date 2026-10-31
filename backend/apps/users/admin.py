from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ['username', 'email', 'first_name', 'last_name', 'role', 'is_active']
    list_filter = ['role', 'is_active', 'is_staff']
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Trackable', {'fields': ('role', 'employee_id', 'phone', 'department', 'profile_notes')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Trackable', {'fields': ('email', 'role')}),
    )
