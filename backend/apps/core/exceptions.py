"""
Custom exception handler for consistent error responses.
"""
import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
from django.db.models import ProtectedError
from django_fsm import TransitionNotAllowed

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    """Return consistent error response shape."""
    if isinstance(exc, BusinessRuleViolation):
        return Response(
            {'error': exc.code, 'message': exc.message, 'details': exc.details},
            status=exc.status_code,
        )
    if isinstance(exc, TransitionNotAllowed):
        return Response(
            {'error': 'invalid_transition', 'message': str(exc) or 'Transition not allowed from the current state.', 'details': {}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, ProtectedError):
        return Response(
            {'error': 'conflict', 'message': 'This record is referenced by other records and cannot be deleted. Deactivate it instead.', 'details': {}},
            status=status.HTTP_409_CONFLICT,
        )

    response = exception_handler(exc, context)

    if response is None:
        logger.exception('Unhandled exception in %s', context.get('view'))
        return Response(
            {
                'error': 'internal_server_error',
                'message': 'An unexpected error occurred. Please try again.',
                'details': {},
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    error_data = {
        'error': _get_error_code(response.status_code),
        'message': _extract_message(response.data),
        'details': response.data if isinstance(response.data, dict) else {'non_field_errors': response.data},
    }
    response.data = error_data
    return response


def _get_error_code(status_code):
    codes = {
        400: 'bad_request',
        401: 'unauthorized',
        403: 'forbidden',
        404: 'not_found',
        405: 'method_not_allowed',
        409: 'conflict',
        422: 'unprocessable_entity',
        429: 'too_many_requests',
        500: 'internal_server_error',
    }
    return codes.get(status_code, 'error')


def _extract_message(data):
    if isinstance(data, list):
        return str(data[0]) if data else 'Error'
    if isinstance(data, dict):
        for key in ('detail', 'message', 'non_field_errors'):
            if key in data:
                val = data[key]
                return str(val[0]) if isinstance(val, list) else str(val)
        # Return first field error
        for val in data.values():
            return str(val[0]) if isinstance(val, list) else str(val)
    return str(data)


class BusinessRuleViolation(Exception):
    """
    Raised by the service layer when a domain rule rejects an operation.
    Mapped to a 400 (or the given status) by the exception handler.
    """
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message, code='business_rule_violation', details=None, status_code=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code


class BusinessConflict(BusinessRuleViolation):
    """A rule violation the caller may resolve (e.g. acknowledge a duplicate)."""
    status_code = status.HTTP_409_CONFLICT
