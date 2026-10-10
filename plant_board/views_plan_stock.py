"""
plant_board/views_plan_stock.py

``GET  /api/v1/dashboards/plant-board/plan-stock-warehouses/``
``PUT  /api/v1/dashboards/plant-board/plan-stock-warehouses/``

Which warehouses the month-plan SKU drill counts as stock.

GET answers the choice and the catalogue to choose from: every warehouse
holding finished goods, with its tonnage on the board's litre basis. A ticked
warehouse that holds nothing today is still listed, so it can be unticked.

PUT takes ``{"warehouses": [...]}``. An empty list or null goes back to every
warehouse -- "count nothing" is not a setting anybody means.
"""

import logging

from django.db import DatabaseError
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.context import CompanyContext
from stock_dashboard.models import PlantBoardPlanStock

from .hana_reader import PlantBoardReader
from .permissions import CanViewPlantBoard
from .services import _tons, plan_stock_warehouses

logger = logging.getLogger(__name__)


def _payload(company_code: str):
    selected = plan_stock_warehouses(company_code)
    catalogue = {}
    for row in PlantBoardReader(CompanyContext(company_code)).finished_goods_warehouses():
        code = row.get("Warehouse") or ""
        if not code:
            continue
        catalogue[code] = {
            "code": code,
            "name": row.get("WarehouseName") or "",
            "items": int(row.get("Items") or 0),
            "pieces": round(float(row.get("Pieces") or 0), 2),
            "tons": _tons(row.get("Litres")),
        }
    for code in selected or ():
        catalogue.setdefault(
            code, {"code": code, "name": "", "items": 0, "pieces": 0.0, "tons": 0.0}
        )
    return {
        # Null is every warehouse.
        "selected": sorted(selected) if selected is not None else None,
        "warehouses": sorted(catalogue.values(), key=lambda w: (-w["tons"], w["code"])),
    }


class PlantBoardPlanStockAPI(APIView):
    """Read and write the month-plan drill's stock warehouses."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPlantBoard]

    def get(self, request):
        return Response({"data": _payload(request.company.company.code)})

    def put(self, request):
        company_code = request.company.company.code
        raw = request.data.get("warehouses")
        if raw is not None and not isinstance(raw, list):
            return Response(
                {"detail": "`warehouses` must be a list of warehouse codes."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        codes = sorted({str(code).strip().upper() for code in raw or [] if str(code).strip()})

        try:
            PlantBoardPlanStock.objects.update_or_create(
                company_code=company_code,
                defaults={
                    "warehouses": codes or None,
                    "updated_by": request.user if request.user.is_authenticated else None,
                },
            )
        except DatabaseError as exc:
            logger.warning("plant_board: plan stock warehouses not saved: %s", exc)
            return Response(
                {"detail": "The setting could not be saved: the database is not migrated yet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        logger.info(
            "plant_board: plan stock warehouses set to %s for %s by %s",
            codes or "all",
            company_code,
            getattr(request.user, "username", "anonymous"),
        )
        return Response({"data": _payload(company_code)})
