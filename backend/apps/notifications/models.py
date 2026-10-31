"""Notifications models."""
from django.db import models
from django.conf import settings
from apps.core.mixins import TimeStampedModel, UUIDModel


class Notification(UUIDModel, TimeStampedModel):
    LEVEL_CHOICES = [
        ('INFO', 'Info'),
        ('WARNING', 'Warning'),
        ('ERROR', 'Error'),
        ('CRITICAL', 'Critical'),
    ]
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications',
    )
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='INFO')
    title = models.CharField(max_length=200)
    message = models.TextField()
    entity_type = models.CharField(max_length=100, blank=True)
    entity_id = models.CharField(max_length=200, blank=True)
    link_url = models.CharField(max_length=500, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'notifications_notification'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.level}: {self.title}"
