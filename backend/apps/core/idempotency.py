"""
Retry-safe execution for creation endpoints (spec §9: idempotency protection).
"""
import hashlib
import json
from datetime import timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.exceptions import BusinessConflict
from apps.core.models import IdempotencyKey

KEY_TTL = timedelta(hours=24)
HEADER = 'HTTP_IDEMPOTENCY_KEY'


def key_from_request(request):
    key = (request.META.get(HEADER) or '').strip()
    return key[:200] or None


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def run_idempotent(user, key, operation, payload, fn):
    """
    Run `fn()` (which must return JSON-serializable data) at most once per
    (user, operation, key). Returns (result, replayed).

    The key row is inserted *before* the work, in the same transaction, so a
    concurrent duplicate blocks on the unique index until the first commits and
    then replays its stored result.
    """
    if not key:
        return fn(), False
    request_hash = _hash(payload)
    try:
        with transaction.atomic():
            record = IdempotencyKey.objects.create(
                user=user, operation=operation, key=key, request_hash=request_hash,
                expires_at=timezone.now() + KEY_TTL,
            )
            # Normalise (UUIDs, decimals, dates) so the stored replay equals the first response
            result = json.loads(json.dumps(fn(), cls=DjangoJSONEncoder))
            record.response = result
            record.save(update_fields=['response'])
            return result, False
    except IntegrityError:
        existing = IdempotencyKey.objects.filter(user=user, operation=operation, key=key).first()
        if existing is None:
            raise  # the IntegrityError came from fn(), not from the key
    if existing.request_hash != request_hash:
        raise BusinessConflict(
            'This Idempotency-Key was already used with a different request.', code='idempotency_key_reused',
        )
    return existing.response, True
