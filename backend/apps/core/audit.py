"""
Single entry point for writing AuditEvent records.
"""
from apps.core.models import AuditEvent


def client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def record_audit(actor, action, entity_type, entity_id, *, reason='', before=None, after=None,
                 extra=None, request=None):
    """Append an immutable audit event. Call inside the same transaction as the change."""
    return AuditEvent.objects.create(
        actor=actor if actor is not None and actor.is_authenticated else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        reason=reason or '',
        before_state=before,
        after_state=after,
        extra=extra,
        ip_address=client_ip(request),
        correlation_id=(request.META.get('HTTP_X_REQUEST_ID', '')[:100] if request is not None else ''),
    )
