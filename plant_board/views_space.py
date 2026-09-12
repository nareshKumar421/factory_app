"""
plant_board/views_space.py

``GET  /api/v1/dashboards/plant-board/space/``
``PUT  /api/v1/dashboards/plant-board/space/``

One number: the floor a single pallet stands on. 15 sq ft, as measured.

WHY IT HAS TO BE TYPED
----------------------
The packaging stores are measured in square feet and their contents are counted
in pieces, and SAP cannot bridge the two. Verified on 12 September 2026 against
all 878 packaging items in Oil: ``OITM`` holds no volume (``SVolume`` and
``BVolume`` are zero on every one), no dimensions (``SLength1``, ``SWidth1``,
``SHeight1`` likewise) and a gross weight on 155 of them. There is no footprint
to derive, so somebody measures the rate once.

THE OTHER HALF IS ALREADY MEASURED
----------------------------------
Pieces become pallets on the factory's own stacking sheet, PER ITEM -- 300
five-litre bottles to a pallet, 60,000 caps to a pallet -- which lives in
``plant_board/data/stacking.json`` and is re-imported with
``manage.py import_stacking``. This endpoint holds only the last step, the
pallet's own footprint, because that is the part that varies by site rather
than by material.

Clearing it is a real edit: the board goes back to reporting the floor and the
stock separately, which is the honest state when nobody stands behind the
figure. Leaving it untouched uses the measured default of 15 sq ft, so an
unconfigured board is right rather than silent.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from stock_dashboard.models import PlantBoardSettings

from .constants import (
    DEFAULT_SQFT_PER_PALLET,
    PACKAGING_FLOOR_BLOCKS,
    PACKAGING_FLOOR_SQFT,
)
from .permissions import CanViewPlantBoard

logger = logging.getLogger(__name__)


def _payload(company_code: str):
    """The factor, and the floor it is measured against."""
    row = PlantBoardSettings.objects.filter(company_code=company_code).first()
    return {
        "sqft_per_pallet": (
            float(row.sqft_per_pallet)
            if row and row.sqft_per_pallet is not None
            else DEFAULT_SQFT_PER_PALLET
        ),
        # Returned so the settings page can show what the factor is measured
        # against without a second request, and so the areas are visible
        # somewhere an operator can check them.
        "floor_sqft": PACKAGING_FLOOR_SQFT,
        "blocks": [
            {"label": block["label"], "sqft": block["sqft"]}
            for block in PACKAGING_FLOOR_BLOCKS
        ],
        "updated_at": row.updated_at.isoformat() if row else None,
    }


class PlantBoardSpaceAPI(APIView):
    """Read and write the floor-area factor."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPlantBoard]

    def get(self, request):
        return Response({"data": _payload(request.company.company.code)})

    def put(self, request):
        company_code = request.company.company.code
        raw = request.data.get("sqft_per_pallet")

        # Empty clears it, and clearing is a real edit: it puts the tile back
        # to reporting the floor and the stock separately, which is the honest
        # state when nobody stands behind the rate any more.
        if raw in (None, ""):
            value = None
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "Square feet per pallet must be a number."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if value <= 0:
                return Response(
                    {"detail": "Square feet per pallet must be more than zero."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        PlantBoardSettings.objects.update_or_create(
            company_code=company_code,
            defaults={
                "sqft_per_pallet": value,
                "updated_by": request.user if request.user.is_authenticated else None,
            },
        )
        logger.info(
            "plant_board: space factor set to %s for %s by %s",
            value,
            company_code,
            getattr(request.user, "username", "anonymous"),
        )
        return Response({"data": _payload(company_code)})
