"""
Shared utility functions.
"""
import secrets
import string
from django.utils import timezone


def generate_reference(prefix: str, length: int = 8) -> str:
    """Generate a unique reference like GR-20241028-ABC12345."""
    date_part = timezone.localdate().strftime('%Y%m%d')
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(length))
    return f"{prefix}-{date_part}-{random_part}"


def generate_package_token() -> str:
    """Generate a secure opaque token for QR codes (not the package ID itself)."""
    return secrets.token_urlsafe(24)


def generate_sample_id() -> str:
    return generate_reference('SMP', 6)


def generate_issue_number() -> str:
    return generate_reference('ISS', 6)


def generate_receipt_number() -> str:
    return generate_reference('GR', 8)
