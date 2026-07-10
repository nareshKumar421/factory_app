"""Domain errors for the marketplace app, mapped to HTTP status codes in views."""


class MarketplaceError(Exception):
    """A business-rule violation surfaced to the client as {code, error, detail}."""

    def __init__(self, message, code="MARKETPLACE_ERROR", status_code=400, detail=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.detail = detail

    def to_response(self):
        body = {"code": self.code, "error": self.message}
        if self.detail is not None:
            body["detail"] = self.detail
        return body
