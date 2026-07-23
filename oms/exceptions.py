"""Domain exceptions for the OMS invoice-approval proxy.

Mirrors ``sap_client/exceptions.py``. Views translate these into HTTP responses:
``OMSValidationError`` -> 400, ``OMSConnectionError`` -> 503, ``OMSDataError`` -> 502.
"""


class OMSValidationError(Exception):
    """Bad request (invalid input, or OMS returned 400 e.g. REJECTED without a reason)."""
    pass


class OMSConnectionError(Exception):
    """OMS unreachable, timed out, or authentication failed."""
    pass


class OMSDataError(Exception):
    """OMS reachable but returned an unexpected/invalid response."""
    pass
