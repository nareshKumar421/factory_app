"""
URLs for the leave module.

Order matters in the usual place: the static routes (``pending/``,
``calendar/``, ``types/``) are declared before ``requests/<int:...>`` so a
literal path can never be captured as an id. Everything is keyed by primary key
rather than by employee code, for the same reason
:mod:`employee_hierarchy.urls` gives -- codes are edited when somebody was
entered wrong, and a URL that changes underneath a bookmark is worse than one
that is not pretty.

The decision endpoints put approve and reject in the **path** rather than in
the payload. A body field deciding between "allow" and "refuse" is one typo
away from the wrong outcome, and the two are separately auditable actions.
"""

from django.urls import path

from .views import (
    HolidayDetailAPI,
    HolidayListAPI,
    LeaveBalanceAPI,
    LeaveCalendarAPI,
    LeaveCancelAPI,
    LeaveDecisionAPI,
    LeaveEmployeePickerAPI,
    LeaveRequestDetailAPI,
    LeaveRequestHistoryAPI,
    LeaveRequestListAPI,
    LeaveTypeDetailAPI,
    LeaveTypeListAPI,
    LeaveWithdrawAPI,
    PendingLeaveAPI,
    PendingLeaveCountAPI,
)

urlpatterns = [
    # Masters.
    path("types/", LeaveTypeListAPI.as_view(), name="leave-type-list"),
    path("types/<int:type_id>/", LeaveTypeDetailAPI.as_view(), name="leave-type-detail"),
    path("holidays/", HolidayListAPI.as_view(), name="leave-holiday-list"),
    path(
        "holidays/<int:holiday_id>/",
        HolidayDetailAPI.as_view(),
        name="leave-holiday-detail",
    ),
    # The approver's screens. Declared before the id routes.
    path("pending/", PendingLeaveAPI.as_view(), name="leave-pending"),
    path("pending/count/", PendingLeaveCountAPI.as_view(), name="leave-pending-count"),
    path("calendar/", LeaveCalendarAPI.as_view(), name="leave-calendar"),
    # What is left, per type, for a year. Computed, never stored.
    path("balance/", LeaveBalanceAPI.as_view(), name="leave-balance"),
    # Who the time office may raise an application for.
    path("employees/", LeaveEmployeePickerAPI.as_view(), name="leave-employees"),
    # The applications.
    path("requests/", LeaveRequestListAPI.as_view(), name="leave-request-list"),
    path(
        "requests/<int:request_id>/",
        LeaveRequestDetailAPI.as_view(),
        name="leave-request-detail",
    ),
    path(
        "requests/<int:request_id>/history/",
        LeaveRequestHistoryAPI.as_view(),
        name="leave-request-history",
    ),
    path(
        "requests/<int:request_id>/approve/",
        LeaveDecisionAPI.as_view(),
        {"decision": "approve"},
        name="leave-approve",
    ),
    path(
        "requests/<int:request_id>/reject/",
        LeaveDecisionAPI.as_view(),
        {"decision": "reject"},
        name="leave-reject",
    ),
    path(
        "requests/<int:request_id>/withdraw/",
        LeaveWithdrawAPI.as_view(),
        name="leave-withdraw",
    ),
    path(
        "requests/<int:request_id>/cancel/",
        LeaveCancelAPI.as_view(),
        name="leave-cancel",
    ),
]
