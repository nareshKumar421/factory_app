"""Errors this module raises, each carrying the HTTP status it should become.

`views.PlanningBaseView.handle_exception` maps them, so a service never needs to
know it is being called over HTTP.
"""


class PlanningError(Exception):
    status_code = 400
    code = "planning_error"

    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class PlanNotFound(PlanningError):
    status_code = 404
    code = "plan_not_found"


class PurchaseOrderStateError(PlanningError):
    """The order is not in a state that allows what was asked of it."""

    status_code = 409
    code = "invalid_state"


class SAPPostError(PlanningError):
    status_code = 502
    code = "sap_post_failed"
