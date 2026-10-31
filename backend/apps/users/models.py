"""
Custom User model with role-based access control.
"""
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    ADMIN = 'ADMIN', _('Administrator')
    GATE = 'GATE', _('Gate Operator')
    RECEIVING = 'RECEIVING', _('Receiving / Unloading Worker')
    WAREHOUSE = 'WAREHOUSE', _('Warehouse Operator')
    LAB_TECH = 'LAB_TECH', _('Lab Technician')
    LAB_REVIEWER = 'LAB_REVIEWER', _('Lab Reviewer / Quality Manager')
    FACTORY = 'FACTORY', _('Factory / Requester')
    AUDITOR = 'AUDITOR', _('Auditor (Read-only)')
    MANAGER = 'MANAGER', _('Manager')


class User(AbstractUser):
    """
    Extended user with a single primary role per account.
    Superusers bypass all role checks.
    """
    email = models.EmailField(_('email address'), unique=True)
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.AUDITOR,
        db_index=True,
    )
    employee_id = models.CharField(max_length=50, blank=True, db_index=True)
    phone = models.CharField(max_length=30, blank=True)
    department = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    profile_notes = models.TextField(blank=True)

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email', 'role']

    class Meta:
        db_table = 'users_user'
        verbose_name = _('User')
        verbose_name_plural = _('Users')
        ordering = ['username']

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    def save(self, *args, **kwargs):
        # Administrators maintain master data and configuration in Django admin.
        if self.role == Role.ADMIN:
            self.is_staff = True
        super().save(*args, **kwargs)

    def has_perm(self, perm, obj=None):
        return (self.is_active and self.role == Role.ADMIN) or super().has_perm(perm, obj)

    def has_module_perms(self, app_label):
        return (self.is_active and self.role == Role.ADMIN) or super().has_module_perms(app_label)

    def has_role(self, *roles: str) -> bool:
        if self.is_superuser:
            return True
        return self.role in roles

    @property
    def display_name(self):
        return self.get_full_name() or self.username
