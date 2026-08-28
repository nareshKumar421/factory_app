"""API for the warehouse-manager assignments.

Two audiences, two permissions:

* The admin screen reads and writes assignments — `can_manage_user_warehouses`.
* Every warehouse screen reads *its own* user's warehouses, to fill a dropdown
  or hide an Approve button. That must NOT need the admin permission, so
  `/mine/` is open to any authenticated user and only ever answers about
  themselves.
"""

import logging

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models_manager import UserWarehouse
from .permissions import CanManageUserWarehouses
from .serializers_manager import (
    UserWarehouseCreateSerializer,
    UserWarehouseSerializer,
)
from .services import warehouse_scope

logger = logging.getLogger(__name__)


class MyWarehousesAPI(APIView):
    """The acting user's own warehouses, for the active company.

    Deliberately available to any authenticated user: a screen cannot correctly
    disable an action it is not allowed to ask about, and the answer is only ever
    about the caller.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        company_code = request.company.company.code
        return Response(
            {
                "unrestricted": warehouse_scope.is_unrestricted(request.user),
                "warehouse_codes": sorted(
                    warehouse_scope.managed_warehouses(request.user, company_code)
                ),
            }
        )


class UserWarehouseListAPI(APIView):
    """List and create assignments for the active company."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageUserWarehouses]

    def get(self, request):
        company_code = request.company.company.code
        rows = (
            UserWarehouse.objects.filter(company__code=company_code)
            .select_related("user", "company", "assigned_by")
            .order_by("user__full_name", "warehouse_code")
        )
        user_id = request.query_params.get("user")
        if user_id:
            rows = rows.filter(user_id=user_id)
        warehouse = request.query_params.get("warehouse_code")
        if warehouse:
            rows = rows.filter(warehouse_code=warehouse.strip().upper())
        if request.query_params.get("active_only") == "true":
            rows = rows.filter(is_active=True)
        return Response(UserWarehouseSerializer(rows, many=True).data)

    def post(self, request):
        serializer = UserWarehouseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        company = request.company.company

        created, reactivated, existing = [], [], []
        with transaction.atomic():
            for code in data["warehouse_codes"]:
                # Reactivate rather than create a second row: the unique
                # constraint would refuse a duplicate, and an admin re-adding a
                # manager who was moved away should just work.
                row, was_created = UserWarehouse.objects.get_or_create(
                    user_id=data["user"],
                    company=company,
                    warehouse_code=code,
                    defaults={"assigned_by": request.user},
                )
                if was_created:
                    created.append(code)
                elif not row.is_active:
                    row.is_active = True
                    row.assigned_by = request.user
                    row.save(update_fields=["is_active", "assigned_by", "updated_at"])
                    reactivated.append(code)
                else:
                    existing.append(code)

        rows = (
            UserWarehouse.objects.filter(user_id=data["user"], company=company)
            .select_related("user", "company", "assigned_by")
            .order_by("warehouse_code")
        )
        return Response(
            {
                "created": created,
                "reactivated": reactivated,
                "already_assigned": existing,
                "assignments": UserWarehouseSerializer(rows, many=True).data,
            },
            status=status.HTTP_201_CREATED if created or reactivated else status.HTTP_200_OK,
        )


class UserWarehouseDetailAPI(APIView):
    """Deactivate (or restore) one assignment.

    Deactivates rather than deletes: past transfers were raised on the strength
    of this row and the record of who was responsible should survive a
    reassignment.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageUserWarehouses]

    def _get(self, request, pk):
        return UserWarehouse.objects.filter(
            pk=pk, company__code=request.company.company.code
        ).select_related("user", "company", "assigned_by").first()

    def patch(self, request, pk):
        row = self._get(request, pk)
        if row is None:
            return Response(
                {"detail": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND
            )
        row.is_active = bool(request.data.get("is_active", True))
        row.save(update_fields=["is_active", "updated_at"])
        return Response(UserWarehouseSerializer(row).data)

    def delete(self, request, pk):
        row = self._get(request, pk)
        if row is None:
            return Response(
                {"detail": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND
            )
        row.is_active = False
        row.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class WarehouseScopeGapsAPI(APIView):
    """Users who can move stock but manage no warehouse — i.e. who is locked out.

    Surfaced on the admin page as a warning, because "no assignment means no
    access" turns a missing row into a person who cannot work, and the only
    thing worse than that is not being able to see it coming.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageUserWarehouses]

    def get(self, request):
        company_code = request.company.company.code
        try:
            users = warehouse_scope.users_missing_assignment(company_code)
        except RuntimeError as exc:  # unknown permission codename
            logger.error("Warehouse scope gap check failed: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(
            [
                {
                    "id": u.id,
                    "full_name": u.full_name,
                    "email": u.email,
                    "employee_code": u.employee_code,
                }
                for u in users
            ]
        )
