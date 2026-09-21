"""API for the electricity-meter manager assignments.

Two audiences, two permissions:

* The admin screen reads and writes assignments —
  ``can_manage_user_electricity_meters``.
* The Daily Electricity page reads *its own* user's meters, to disable an Edit
  button or narrow a dropdown. That must NOT need the admin permission, so
  ``/my-electricity-meters/`` is open to any authenticated user and only ever
  answers about themselves.
"""

import logging

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import meter_scope
from .models import ElectricityMeter
from .models_manager import UserElectricityMeter
from .permissions import CanManageUserElectricityMeters
from .serializers_manager import (
    UserElectricityMeterCreateSerializer,
    UserElectricityMeterSerializer,
)

logger = logging.getLogger(__name__)


class MyElectricityMetersAPI(APIView):
    """The acting user's own meters.

    Deliberately available to any authenticated user: a screen cannot correctly
    disable an action it is not allowed to ask about, and the answer is only ever
    about the caller.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        unrestricted = meter_scope.is_unrestricted(request.user)
        meter_ids = sorted(meter_scope.managed_meter_ids(request.user))
        return Response(
            {
                "unrestricted": unrestricted,
                "meter_ids": meter_ids,
                # Names too, so a screen can say "you keep Boiler and Terrace"
                # without a second round trip against the meter master.
                "meters": [
                    {"id": pk, "name": name}
                    for pk, name in ElectricityMeter.objects.filter(
                        pk__in=meter_ids
                    ).values_list("pk", "name")
                ],
            }
        )


class UserElectricityMeterListAPI(APIView):
    """List and create assignments."""

    permission_classes = [IsAuthenticated, CanManageUserElectricityMeters]

    def get(self, request):
        rows = UserElectricityMeter.objects.select_related(
            "user", "meter", "created_by"
        ).order_by("user__full_name", "meter__name")
        user_id = request.query_params.get("user")
        if user_id:
            rows = rows.filter(user_id=user_id)
        meter_id = request.query_params.get("meter")
        if meter_id:
            rows = rows.filter(meter_id=meter_id)
        if request.query_params.get("active_only") == "true":
            rows = rows.filter(is_active=True)
        return Response(UserElectricityMeterSerializer(rows, many=True).data)

    def post(self, request):
        serializer = UserElectricityMeterCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Check the meters exist before writing anything: a bad id would
        # otherwise land as an IntegrityError 500 halfway through the loop.
        wanted = list(data["meters"])
        known = set(
            ElectricityMeter.objects.filter(pk__in=wanted).values_list("pk", flat=True)
        )
        unknown = [m for m in wanted if m not in known]
        if unknown:
            return Response(
                {"meters": [f"No such meter: {', '.join(str(m) for m in unknown)}"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created, reactivated, existing = [], [], []
        with transaction.atomic():
            for meter_id in wanted:
                # Reactivate rather than create a second row: the unique
                # constraint would refuse a duplicate, and an admin re-adding a
                # keeper who was moved away should just work.
                row, was_created = UserElectricityMeter.objects.get_or_create(
                    user_id=data["user"],
                    meter_id=meter_id,
                    defaults={
                        "created_by": request.user,
                        "updated_by": request.user,
                    },
                )
                if was_created:
                    created.append(meter_id)
                elif not row.is_active:
                    row.is_active = True
                    row.updated_by = request.user
                    row.save(update_fields=["is_active", "updated_by", "updated_at"])
                    reactivated.append(meter_id)
                else:
                    existing.append(meter_id)

        rows = (
            UserElectricityMeter.objects.filter(user_id=data["user"])
            .select_related("user", "meter", "created_by")
            .order_by("meter__name")
        )
        return Response(
            {
                "created": created,
                "reactivated": reactivated,
                "already_assigned": existing,
                "assignments": UserElectricityMeterSerializer(rows, many=True).data,
            },
            status=status.HTTP_201_CREATED
            if created or reactivated
            else status.HTTP_200_OK,
        )


class UserElectricityMeterDetailAPI(APIView):
    """Deactivate (or restore) one assignment.

    Deactivates rather than deletes: readings were filed on the strength of this
    row and the record of who was responsible should survive a reassignment.
    """

    permission_classes = [IsAuthenticated, CanManageUserElectricityMeters]

    def _get(self, pk):
        return (
            UserElectricityMeter.objects.filter(pk=pk)
            .select_related("user", "meter", "created_by")
            .first()
        )

    def patch(self, request, pk):
        row = self._get(pk)
        if row is None:
            return Response(
                {"detail": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND
            )
        row.is_active = bool(request.data.get("is_active", True))
        row.updated_by = request.user
        row.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(UserElectricityMeterSerializer(row).data)

    def delete(self, request, pk):
        row = self._get(pk)
        if row is None:
            return Response(
                {"detail": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND
            )
        row.is_active = False
        row.updated_by = request.user
        row.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ElectricityMeterScopeGapsAPI(APIView):
    """Both halves of "configured wrong": locked-out people, unkept meters.

    Surfaced on the admin page as a warning, because "no assignment means no
    access" turns a missing row into a person who cannot work — and, on the
    other side, into a meter that quietly stops being read.
    """

    permission_classes = [IsAuthenticated, CanManageUserElectricityMeters]

    def get(self, request):
        try:
            users = meter_scope.users_missing_assignment()
        except RuntimeError as exc:  # unknown permission codename
            logger.error("Electricity meter scope gap check failed: %s", exc)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            {
                "users_without_meters": [
                    {
                        "id": u.id,
                        "full_name": u.full_name,
                        "email": u.email,
                        "employee_code": u.employee_code,
                    }
                    for u in users
                ],
                "meters_without_managers": [
                    {"id": m.id, "name": m.name, "location": m.location}
                    for m in meter_scope.unmanaged_meters()
                ],
            }
        )
