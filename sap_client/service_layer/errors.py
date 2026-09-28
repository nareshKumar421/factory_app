"""Which way a Service Layer request went when no answer came back.

A request that never reached SAP created nothing, and trying again is safe. A
request that reached SAP and then timed out may have committed, and trying
again without reading back is how a second GRPO or a second return gets made.
The ``requests`` exception says which of the two happened; this turns it into
the SAP error that says so.
"""

import requests
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from ..exceptions import SAPOutcomeUnknown, SAPUnavailable


def never_sent(exc: Exception) -> bool:
    """True when the request provably never reached the Service Layer.

    A connect timeout, a refused connection and a failed TLS handshake all
    happen before a byte of the request is written. Anything else — a read
    timeout, a connection dropped mid-response — happened after SAP had it.
    """
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.SSLError)):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        # requests wraps urllib3's MaxRetryError; its ``reason`` is the failure
        # that exhausted the (zero) retries.
        inner = exc.args[0] if exc.args else None
        reason = getattr(inner, "reason", None)
        return isinstance(reason, (NewConnectionError, ConnectTimeoutError))
    return False


def unanswered(exc: Exception, message: str):
    """The SAP error for a document request that got no answer.

    Returned, not raised, so the caller keeps its own ``raise ... from e``.
    """
    if never_sent(exc):
        return SAPUnavailable(message)
    return SAPOutcomeUnknown(message)
