"""
Request correlation: every request gets an ID (taken from X-Request-ID when a proxy
supplies one), which appears in the response header, every log line and audit events.
"""
import logging
import re
import threading
import uuid

from django.core.signals import request_finished

_local = threading.local()
_VALID = re.compile(r'^[A-Za-z0-9._-]{1,100}$')


def current_request_id():
    return getattr(_local, 'request_id', '-')


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get('HTTP_X_REQUEST_ID', '')
        request_id = incoming if _VALID.match(incoming) else uuid.uuid4().hex
        request.META['HTTP_X_REQUEST_ID'] = request_id
        _local.request_id = request_id
        response = self.get_response(request)
        response['X-Request-ID'] = request_id
        return response


def _clear_request_id(**kwargs):
    # Cleared when the request is finished, not when the middleware returns: Django logs
    # 4xx / 5xx responses after the middleware chain, and those lines need the ID too.
    _local.request_id = '-'


request_finished.connect(_clear_request_id, dispatch_uid='trackable_clear_request_id')


class RequestIDLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = current_request_id()
        return True
