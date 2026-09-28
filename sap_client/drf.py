"""The response for an SAP failure that no view caught.

Most views map SAP errors themselves, but not all of them, and one that forgets
turns "SAP is down" into a 500 the frontend can only call a server crash. This
sits behind DRF's own handler and gives those the answer a caught one would
have had: a 503 while SAP is unreachable, with a ``code`` the frontend can key
on rather than a message it would have to parse.
"""

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback

from .exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPOutcomeUnknown,
    SAPUnavailable,
    SAPValidationError,
)

SAP_UNAVAILABLE = "SAP_UNAVAILABLE"
SAP_OUTCOME_UNKNOWN = "SAP_OUTCOME_UNKNOWN"
SAP_VALIDATION = "SAP_VALIDATION"
SAP_ERROR = "SAP_ERROR"


# For a view that maps an SAP failure itself. ``APIException(code=503)`` does
# not do this: ``code`` is DRF's error-code string, and the status stays 500.
class SAPUnavailableAPIException(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "SAP system is currently unavailable. Please try again later."
    default_code = "sap_unavailable"


class SAPBadGatewayAPIException(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "SAP returned an error."
    default_code = "sap_error"


def sap_error_response(exc):
    """``(status, body)`` for an SAP exception, or None for anything else."""
    if isinstance(exc, SAPOutcomeUnknown):
        return status.HTTP_503_SERVICE_UNAVAILABLE, {
            "detail": (
                f"{exc} SAP did not answer in time, so it may still have saved "
                f"this. Check SAP before trying again."
            ),
            "code": SAP_OUTCOME_UNKNOWN,
        }
    if isinstance(exc, SAPUnavailable):
        return status.HTTP_503_SERVICE_UNAVAILABLE, {
            "detail": (
                f"SAP is not responding right now ({exc}). Nothing was sent to "
                f"SAP; please try again later."
            ),
            "code": SAP_UNAVAILABLE,
        }
    if isinstance(exc, SAPConnectionError):
        return status.HTTP_503_SERVICE_UNAVAILABLE, {
            "detail": f"SAP is currently unavailable ({exc}). Please try again later.",
            "code": SAP_UNAVAILABLE,
        }
    if isinstance(exc, SAPValidationError):
        return status.HTTP_400_BAD_REQUEST, {"detail": str(exc), "code": SAP_VALIDATION}
    if isinstance(exc, SAPDataError):
        return status.HTTP_502_BAD_GATEWAY, {"detail": str(exc), "code": SAP_ERROR}
    return None


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        return response
    mapped = sap_error_response(exc)
    if mapped is None:
        return None
    set_rollback()
    code, body = mapped
    return Response(body, status=code)
