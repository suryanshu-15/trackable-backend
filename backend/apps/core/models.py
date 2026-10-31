"""
AuditEvent — immutable audit log for all sensitive actions.
"""
from django.db import models
from django.conf import settings
from apps.core.mixins import UUIDModel


class AuditEvent(UUIDModel):
    """Immutable audit trail entry. Never delete or update."""

    ACTION_CREATE = 'CREATE'
    ACTION_UPDATE = 'UPDATE'
    ACTION_DELETE = 'DELETE'
    ACTION_TRANSITION = 'TRANSITION'
    ACTION_PRINT = 'PRINT'
    ACTION_ISSUE = 'ISSUE'
    ACTION_RETURN = 'RETURN'
    ACTION_ADJUST = 'ADJUST'
    ACTION_APPROVE = 'APPROVE'
    ACTION_REJECT = 'REJECT'
    ACTION_OVERRIDE = 'OVERRIDE'
    ACTION_UPLOAD = 'UPLOAD'
    ACTION_DOWNLOAD = 'DOWNLOAD'
    ACTION_LOGIN = 'LOGIN'
    ACTION_LOGOUT = 'LOGOUT'
    ACTION_SCAN = 'SCAN'

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_events',
        db_index=True,
    )
    action = models.CharField(max_length=50, db_index=True)
    entity_type = models.CharField(max_length=100, db_index=True)
    entity_id = models.CharField(max_length=200, db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    reason = models.TextField(blank=True)
    # Serialized safe summary — no sensitive PII
    before_state = models.JSONField(null=True, blank=True)
    after_state = models.JSONField(null=True, blank=True)
    extra = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    correlation_id = models.CharField(max_length=100, blank=True, db_index=True)

    class Meta:
        db_table = 'core_audit_event'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['entity_type', 'entity_id']),
            models.Index(fields=['actor', 'timestamp']),
        ]

    def __str__(self):
        return f"[{self.action}] {self.entity_type}:{self.entity_id} by {self.actor_id}"

    def save(self, *args, **kwargs):
        # Prevent updates to audit records
        if not self._state.adding:
            raise ValueError("AuditEvent records are immutable and cannot be updated.")
        super().save(*args, **kwargs)


class IdempotencyKey(UUIDModel):
    """
    Makes creation endpoints retry-safe. A client sends an Idempotency-Key header;
    a retry with the same key (same user + operation) returns the original result
    instead of creating duplicates. Reusing a key with a different payload is rejected.
    """
    key = models.CharField(max_length=200)
    operation = models.CharField(max_length=100)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    request_hash = models.CharField(max_length=64)
    response = models.JSONField(null=True, blank=True)
    result_id = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'core_idempotency_key'
        constraints = [
            models.UniqueConstraint(fields=['user', 'operation', 'key'], name='idempotency_key_unique_per_user_op'),
        ]

    def __str__(self):
        return f"{self.operation}:{self.key}"


class SystemConfiguration(models.Model):
    """Key-value configuration store for runtime-configurable settings."""
    key = models.CharField(max_length=200, unique=True)
    value = models.TextField()
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    class Meta:
        db_table = 'core_system_configuration'

    def __str__(self):
        return f"{self.key} = {self.value[:50]}"
