class SupplyChainError(Exception):
    """A supply-chain failure with an API-shaped code and status."""

    def __init__(self, message, code="ERROR", status_code=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
