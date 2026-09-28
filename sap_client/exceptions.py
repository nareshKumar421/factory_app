class SAPValidationError(Exception):
    """Raised when SAP validation fails"""
    pass


class SAPConnectionError(Exception):
    """Raised when SAP connection fails.

    Catch this for "SAP did not answer". Raise one of the two subclasses where
    it is known which way the failure went: whoever retries a posting has to
    tell "nothing was sent" apart from "sent, and never heard back".
    """
    pass


class SAPUnavailable(SAPConnectionError):
    """SAP was never reached, so nothing was created and retrying is safe.

    A login that failed, a refused connection, a connect timeout, a 401 on a
    session that expired. The document was not written.
    """
    pass


class SAPOutcomeUnknown(SAPConnectionError):
    """The request went out and no answer came back.

    SAP may well have committed it: a timeout on our side does not roll back
    the SAP-side commit. Read back by reference before sending it again, or a
    retry is a second document.
    """
    pass


class SAPDataError(Exception):
    """Raised when SAP data operation fails"""
    pass
