"""API for the raw-material stock register.

Two permissions, matching the two audiences: everyone in the warehouse can read
the register (`can_view_rm_stock`), only a store keeper sets a quantity
(`can_set_rm_stock`) — and the permission is necessary but not sufficient, since
the service also insists they manage the warehouse in question.

The item picker is a separate endpoint on purpose: it is the only thing here
that touches SAP HANA, and keeping it off the register's own read path means a
HANA outage cannot take the register down with it.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .models_rm_stock import RawMaterialStock
from .permissions import CanSetRMStock, CanViewRMStock
from .serializers_rm_stock import (
    RawMaterialStockEntrySerializer,
    RawMaterialStockSerializer,
    SetRawMaterialStockSerializer,
)
from .services import rm_stock_import, rm_stock_service, warehouse_scope

logger = logging.getLogger(__name__)


class RawMaterialItemSearchAPI(APIView):
    """Raw-material items from SAP, for the item picker.

    `warehouse_code` is optional and only enriches the answer with SAP's own
    on-hand for that warehouse, so the keeper can see what SAP thinks beside
    what they are about to type.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewRMStock]

    def get(self, request):
        try:
            items = rm_stock_service.search_rm_items(
                company_code=request.company.company.code,
                search=request.query_params.get("search", ""),
                warehouse_code=request.query_params.get("warehouse_code") or None,
                limit=int(request.query_params.get("limit") or 50),
            )
        except (SAPConnectionError, SAPDataError) as exc:
            logger.error("RM item search failed: %s", exc)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except ValueError:
            return Response(
                {"detail": "limit must be a number."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"items": items})


class RawMaterialStockListAPI(APIView):
    """The register for the active company, and setting a quantity on it."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanSetRMStock()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewRMStock()]

    def get(self, request):
        company_code = request.company.company.code
        rows = rm_stock_service.list_stock(
            company_code=company_code,
            warehouse_code=request.query_params.get("warehouse_code") or None,
            search=request.query_params.get("search", ""),
            include_inactive=request.query_params.get("include_inactive") == "true",
        )
        return Response(
            {
                # The one warehouse this register covers. Sent rather than
                # hardcoded in the client so the two cannot disagree when the
                # setting changes.
                "register_warehouse": rm_stock_service.register_warehouse(),
                # The screen needs to know which rows this user may edit, and it
                # cannot work that out from the rows alone. Fails open the same
                # way `my-warehouses/` does — the service is the enforcement
                # point, this only decides what to offer.
                "unrestricted": warehouse_scope.is_unrestricted(request.user),
                "managed_warehouse_codes": sorted(
                    warehouse_scope.managed_warehouses(request.user, company_code)
                ),
                "rows": RawMaterialStockSerializer(rows, many=True).data,
            }
        )

    def post(self, request):
        serializer = SetRawMaterialStockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        row = rm_stock_service.set_quantity(
            user=request.user,
            company=request.company.company,
            warehouse_code=data["warehouse_code"],
            item_code=data["item_code"],
            item_name=data.get("item_name", ""),
            uom=data.get("uom", ""),
            qty=data["qty"],
            as_of_date=data.get("as_of_date"),
            remarks=data.get("remarks", ""),
        )
        return Response(
            RawMaterialStockSerializer(row).data, status=status.HTTP_200_OK
        )


class RawMaterialStockImportAPI(APIView):
    """Read the warehouse's shift-wise issue sheet into the register.

    Takes either an uploaded ``file`` or ``text`` pasted straight out of Excel;
    both are read by the same parser.

    Two passes over one endpoint. Without ``commit`` it parses and answers with
    what it found, writing nothing — the keeper sees the totals, the rows it
    could not use and any line the two issuers disagreed on, and decides. With
    ``commit=true`` the same parse is applied through the normal
    ``set_quantity`` path, so the manager check and the history entry happen
    exactly as they do for a typed figure.

    Mismatched rows do not silently pass: the confirm has to acknowledge them.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanSetRMStock]

    def post(self, request):
        upload = request.FILES.get("file")
        pasted = (request.data.get("text") or "").strip()
        if upload is None and not pasted:
            return Response(
                {"detail": "Attach the sheet as `file`, or paste the rows as `text`."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            if upload is not None:
                parsed = rm_stock_import.parse_sheet(upload)
            else:
                parsed = rm_stock_import.parse_pasted(pasted)
        except rm_stock_import.SheetError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        commit = str(request.data.get("commit", "")).lower() == "true"
        accept_mismatches = (
            str(request.data.get("accept_mismatches", "")).lower() == "true"
        )

        if not commit:
            return Response({"committed": False, **parsed})

        if parsed["mismatches"] and not accept_mismatches:
            return Response(
                {
                    "detail": (
                        "The two issuer columns disagree on "
                        f"{len(parsed['mismatches'])} row(s). Confirm to import "
                        "anyway — the later column is the figure used."
                    ),
                    "committed": False,
                    **parsed,
                },
                status=status.HTTP_409_CONFLICT,
            )

        result = rm_stock_import.apply_sheet(
            user=request.user,
            company=request.company.company,
            parsed=parsed,
            source_name=getattr(upload, "name", "") if upload is not None else "pasted rows",
        )
        return Response({"committed": True, **parsed, **result})


class RawMaterialStockDetailAPI(APIView):
    """Read one register row, or take it off the register."""

    def get_permissions(self):
        if self.request.method in ("GET",):
            return [IsAuthenticated(), HasCompanyContext(), CanViewRMStock()]
        return [IsAuthenticated(), HasCompanyContext(), CanSetRMStock()]

    def _get(self, request, pk):
        return (
            RawMaterialStock.objects.filter(
                pk=pk, company__code=request.company.company.code
            )
            .select_related("company", "set_by")
            .first()
        )

    def get(self, request, pk):
        row = self._get(request, pk)
        if row is None:
            return Response(
                {"detail": "Stock row not found."}, status=status.HTTP_404_NOT_FOUND
            )
        history = rm_stock_service.history_for(
            company_code=request.company.company.code, row=row
        )
        return Response(
            {
                "row": RawMaterialStockSerializer(row).data,
                "history": RawMaterialStockEntrySerializer(history, many=True).data,
            }
        )

    def delete(self, request, pk):
        row = self._get(request, pk)
        if row is None:
            return Response(
                {"detail": "Stock row not found."}, status=status.HTTP_404_NOT_FOUND
            )
        rm_stock_service.remove_row(
            user=request.user,
            company=request.company.company,
            row=row,
            remarks=request.data.get("remarks", "") if request.data else "",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
