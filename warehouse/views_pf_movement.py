"""API for the godown outward-movement register.

Two permissions, matching the two audiences: the dashboard side reads the
register (`can_view_pf_movement`), only the keeper of a floor declares what is
leaving it (`can_record_pf_movement`) — and the permission is necessary but not
sufficient, since the service also insists they manage the source warehouse.

The two pickers are separate endpoints on purpose: they are the only paths here
that touch SAP HANA, and keeping them off the register's own read and write paths
means a HANA outage cannot stop the keeper filing what he already knows.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanRecordPFMovement, CanViewPFMovement
from .serializers_pf_movement import (
    PFStockMovementCreateSerializer,
    PFStockMovementEventSerializer,
    PFStockMovementSerializer,
    PFStockMovementUpdateSerializer,
)
from .services import pf_movement_service, warehouse_scope

logger = logging.getLogger(__name__)


def _parse_date(value):
    """A blank filter is no filter. Anything unparseable is left to DRF later."""
    from django.utils.dateparse import parse_date

    return parse_date(value) if value else None


class PFMovementItemSearchAPI(APIView):
    """Finished-goods items from SAP, for the item picker.

    ``warehouse_code`` is optional and only enriches the answer with SAP's own
    on-hand for that warehouse, so the keeper sees what SAP thinks is on the
    floor beside the boxes he is declaring.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPFMovement]

    def get(self, request):
        try:
            items = pf_movement_service.search_fg_items(
                company_code=request.company.company.code,
                search=request.query_params.get("search", ""),
                warehouse_code=request.query_params.get("warehouse_code") or None,
                limit=int(request.query_params.get("limit") or 50),
            )
        except (SAPConnectionError, SAPDataError) as exc:
            logger.error("FG item search failed: %s", exc)
            return Response(
                {"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except ValueError:
            return Response(
                {"detail": "limit must be a number."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"items": items})


class PFMovementDestinationsAPI(APIView):
    """Warehouses to send stock to, grouped by company.

    Spans companies because the move this page exists to record does: the PF
    floor pushes finished stock into Mart's Gupta godown while the floor itself
    is Oil's. A company HANA cannot answer for comes back with an empty list and
    its error rather than taking the whole picker down.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPFMovement]

    def get(self, request):
        return Response({"companies": pf_movement_service.list_destinations()})


class PFMovementListAPI(APIView):
    """The register for the active company, and filing a movement onto it."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanRecordPFMovement()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewPFMovement()]

    def get(self, request):
        company_code = request.company.company.code
        # `all_companies=true` widens the list past the active company — what
        # somebody asking "what left the plant today" means, and what the
        # dashboard will want. Reads are unscoped by design (see the service).
        all_companies = request.query_params.get("all_companies") == "true"

        movements = pf_movement_service.list_movements(
            company_code=None if all_companies else company_code,
            from_warehouse=request.query_params.get("from_warehouse") or None,
            to_warehouse=request.query_params.get("to_warehouse") or None,
            date_from=_parse_date(request.query_params.get("date_from")),
            date_to=_parse_date(request.query_params.get("date_to")),
            search=request.query_params.get("search", ""),
            include_cancelled=request.query_params.get("include_cancelled") == "true",
        )
        return Response(
            {
                # The warehouse the form opens on. Sent rather than hardcoded in
                # the client so the two cannot disagree when the setting changes.
                "default_from_warehouse": pf_movement_service.default_source_warehouse(),
                # The screen needs to know which floors this user may declare
                # for, and it cannot work that out from the rows alone. Fails
                # open the same way `my-warehouses/` does — the service is the
                # enforcement point, this only decides what to offer.
                "unrestricted": warehouse_scope.is_unrestricted(request.user),
                "managed_warehouse_codes": sorted(
                    warehouse_scope.managed_warehouses(request.user, company_code)
                ),
                "summary": pf_movement_service.summarise(movements),
                "movements": PFStockMovementSerializer(movements, many=True).data,
            }
        )

    def post(self, request):
        serializer = PFStockMovementCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        movement = pf_movement_service.create_movement(
            user=request.user,
            company=request.company.company,
            from_warehouse=data.get("from_warehouse", ""),
            to_warehouse=data["to_warehouse"],
            to_company=data["to_company"],
            lines=data["lines"],
            movement_date=data.get("movement_date"),
            from_warehouse_name=data.get("from_warehouse_name", ""),
            to_warehouse_name=data.get("to_warehouse_name", ""),
            vehicle_no=data.get("vehicle_no", ""),
            remarks=data.get("remarks", ""),
        )
        return Response(
            PFStockMovementSerializer(movement).data,
            status=status.HTTP_201_CREATED,
        )


class PFMovementDetailAPI(APIView):
    """Read one movement with its trail, correct it, or retract it."""

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated(), HasCompanyContext(), CanViewPFMovement()]
        return [IsAuthenticated(), HasCompanyContext(), CanRecordPFMovement()]

    def _get(self, request, pk):
        # Scoped to the active company: editing another company's declaration
        # from this company's context would apply this company's manager check
        # to the wrong floor.
        return pf_movement_service.get_movement(
            pk=pk, company_code=request.company.company.code
        )

    def get(self, request, pk):
        movement = self._get(request, pk)
        if movement is None:
            return Response(
                {"detail": "Movement not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(
            {
                "movement": PFStockMovementSerializer(movement).data,
                "history": PFStockMovementEventSerializer(
                    movement.events.all(), many=True
                ).data,
            }
        )

    def patch(self, request, pk):
        movement = self._get(request, pk)
        if movement is None:
            return Response(
                {"detail": "Movement not found."}, status=status.HTTP_404_NOT_FOUND
            )
        serializer = PFStockMovementUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        movement = pf_movement_service.update_movement(
            user=request.user,
            movement=movement,
            # `None` means "leave it alone", so only keys the client actually
            # sent are passed through — a missing vehicle number must not blank
            # the one already stored.
            to_warehouse=data.get("to_warehouse"),
            to_company=data.get("to_company"),
            to_warehouse_name=data.get("to_warehouse_name"),
            movement_date=data.get("movement_date"),
            vehicle_no=data.get("vehicle_no"),
            remarks=data.get("remarks"),
            lines=data.get("lines"),
            note=data.get("note", ""),
        )
        # Re-read so the response carries the replaced lines rather than the
        # ones prefetched before the edit.
        movement = pf_movement_service.get_movement(
            pk=movement.pk, company_code=request.company.company.code
        )
        return Response(PFStockMovementSerializer(movement).data)

    def delete(self, request, pk):
        movement = self._get(request, pk)
        if movement is None:
            return Response(
                {"detail": "Movement not found."}, status=status.HTTP_404_NOT_FOUND
            )
        reason = ""
        if request.data:
            reason = request.data.get("reason", "") or request.data.get("remarks", "")
        pf_movement_service.cancel_movement(
            user=request.user, movement=movement, reason=reason
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class PFMovementRestoreAPI(APIView):
    """Put back a movement retracted by mistake."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordPFMovement]

    def post(self, request, pk):
        movement = pf_movement_service.get_movement(
            pk=pk, company_code=request.company.company.code
        )
        if movement is None:
            return Response(
                {"detail": "Movement not found."}, status=status.HTTP_404_NOT_FOUND
            )
        note = request.data.get("note", "") if request.data else ""
        movement = pf_movement_service.restore_movement(
            user=request.user, movement=movement, note=note
        )
        return Response(PFStockMovementSerializer(movement).data)
