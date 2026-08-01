"""
Activity Center API.

Scoping rule: ``/me/*`` always reads ``request.user`` — the user id is never taken
from the request, so holding ``can_view_my_activities`` can only ever expose your own
work. Reading somebody else requires ``can_view_all_activities``; asking for your own
id through the supervisor endpoint is allowed so the frontend can reuse one component.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.permissions import HasRequiredDjangoPermission

from .daily import board_for_day, sheet_for_user
from .serializers import (
    ActivityDefinitionSerializer,
    ActivitySummarySerializer,
    CompletedActivitySerializer,
    DailyBoardSerializer,
    DailySheetSerializer,
    PendingActivitySerializer,
    UserActivityRowSerializer,
)
from .services import (
    completed_for_user,
    definitions,
    overview_all_users,
    pending_for_user,
    summary_for_user,
)

User = get_user_model()

PERM_VIEW_MINE = "activity_center.can_view_my_activities"
PERM_VIEW_ALL = "activity_center.can_view_all_activities"
PERM_VIEW_REPORTS = "activity_center.can_view_activity_reports"

MAX_DAYS = 90


def _window_start(request):
    """
    Resolve ``?days=N`` into a start timestamp. ``days=0`` (the default) means
    "since midnight today", which is what the daily job sheet is measured against.
    """
    raw = request.query_params.get("days", "0")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({"days": ["Must be a whole number of days."]})
    if days < 0 or days > MAX_DAYS:
        raise ValidationError({"days": ["Must be between 0 and %d." % MAX_DAYS]})

    midnight = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days)


def _module_filter(request):
    raw = request.query_params.get("module")
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


class MyActivitySummaryView(APIView):
    """GET /api/v1/activity-center/me/summary/?days=0 — header cards + module breakdown."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"GET": PERM_VIEW_MINE}

    def get(self, request):
        data = summary_for_user(
            request.user,
            company=request.company.company,
            since=_window_start(request),
        )
        return Response(ActivitySummarySerializer(data).data)


class MyPendingActivitiesView(APIView):
    """GET /api/v1/activity-center/me/pending/?module=A,B — what is still outstanding."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"GET": PERM_VIEW_MINE}

    def get(self, request):
        items = pending_for_user(
            request.user,
            company=request.company.company,
            modules=_module_filter(request),
        )
        return Response(PendingActivitySerializer(items, many=True).data)


class MyCompletedActivitiesView(APIView):
    """GET /api/v1/activity-center/me/completed/?days=0 — what the user actually finished."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"GET": PERM_VIEW_MINE}

    def get(self, request):
        items = completed_for_user(
            request.user,
            company=request.company.company,
            since=_window_start(request),
            modules=_module_filter(request),
        )
        return Response(CompletedActivitySerializer(items, many=True).data)


class ActivityDefinitionsView(APIView):
    """
    GET /api/v1/activity-center/definitions/

    The registry as data: every tracked job, the permission that makes someone
    responsible for it, and whether the caller is one of those people. Drives the UI
    filters and lets an administrator see which permission to grant for a given job.
    """

    permission_classes = [IsAuthenticated, HasRequiredDjangoPermission]
    required_permissions = {"GET": PERM_VIEW_MINE}

    def get(self, request):
        return Response(ActivityDefinitionSerializer(definitions(request.user), many=True).data)


class AllUsersActivityView(APIView):
    """
    GET /api/v1/activity-center/users/?days=0

    Supervisor overview — one row per active user. ``queue_pending`` is a shared
    backlog counted for every permission holder, so it must not be summed across users.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"GET": PERM_VIEW_ALL}

    def get(self, request):
        data = overview_all_users(
            company=request.company.company,
            since=_window_start(request),
        )
        return Response(
            {
                "since": data["since"],
                "users": UserActivityRowSerializer(data["users"], many=True).data,
            }
        )


class UserActivityDetailView(APIView):
    """
    GET /api/v1/activity-center/users/<user_id>/?days=0

    One user's full pending and completed lists. Requires ``can_view_all_activities``
    unless the caller is asking about themselves.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, user_id):
        is_self = request.user.pk == user_id
        if is_self:
            if not request.user.has_perm(PERM_VIEW_MINE):
                raise PermissionDenied("You do not have access to the Activity Center.")
        elif not request.user.has_perm(PERM_VIEW_ALL):
            raise PermissionDenied("You can only view your own activities.")

        target = get_object_or_404(User, pk=user_id, is_active=True)
        company = request.company.company
        since = _window_start(request)

        return Response(
            {
                "user": {
                    "user_id": target.pk,
                    "full_name": target.full_name or target.email,
                    "email": target.email,
                    "employee_code": target.employee_code,
                    "groups": sorted(group.name for group in target.groups.all()),
                },
                "summary": ActivitySummarySerializer(
                    summary_for_user(target, company=company, since=since)
                ).data,
                "pending": PendingActivitySerializer(
                    pending_for_user(target, company=company), many=True
                ).data,
                "completed": CompletedActivitySerializer(
                    completed_for_user(target, company=company, since=since), many=True
                ).data,
            }
        )


# ---------------------------------------------------------------------------
# Daily job sheet
# ---------------------------------------------------------------------------


def _requested_day(request):
    """Resolve ``?date=YYYY-MM-DD`` into a local date, defaulting to today.

    Future dates are rejected outright — there is no sheet for a day that has not
    happened — and history is capped at the same window as ``?days``.
    """
    raw = request.query_params.get("date")
    today = timezone.localtime().date()
    if not raw:
        return today

    parsed = parse_date(raw)
    if parsed is None:
        raise ValidationError({"date": ["Must be a date in YYYY-MM-DD form."]})
    if parsed > today:
        raise ValidationError({"date": ["Cannot be in the future."]})
    if (today - parsed).days > MAX_DAYS:
        raise ValidationError({"date": ["Cannot be more than %d days ago." % MAX_DAYS]})
    return parsed


class MyDailySheetView(APIView):
    """This user's job sheet for one day. Always reads ``request.user``."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"GET": PERM_VIEW_MINE}

    def get(self, request):
        day = _requested_day(request)
        company = request.company.company if request.company else None
        sheet = sheet_for_user(request.user, company=company, day=day)
        return Response(DailySheetSerializer(sheet).data)


class AllUsersDailyBoardView(APIView):
    """Every active user's recorded activity for one day.

    Today needs the supervisor permission. Any earlier day additionally needs the
    reports permission: checking on a live day is supervision, trawling back through
    history is reporting, and the two deserve different grants.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"GET": PERM_VIEW_ALL}

    def get(self, request):
        day = _requested_day(request)
        if day != timezone.localtime().date() and not request.user.has_perm(PERM_VIEW_REPORTS):
            raise PermissionDenied("Viewing earlier days requires the activity reports permission.")

        company = request.company.company if request.company else None
        board = board_for_day(company=company, day=day)
        return Response(DailyBoardSerializer(board).data)
