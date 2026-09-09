"""
packing_material/errors.py

Failures this module raises that are neither a SAP outage nor a bad request.
"""


class PlanNotFound(Exception):
    """No such production plan in SAP, or none at all for this company.

    Deliberately not an empty response. A requirement table with no rows and
    no error reads as "the plan needs no packing material", which is the one
    thing this board must never say by accident.
    """
