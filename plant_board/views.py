"""
plant_board/views.py

One endpoint for one screen.

``GET /api/v1/dashboards/plant-board/board/``

Read-only. Requires JWT authentication, a company context header, and any one
of the four rights the board's own reports are gated on.

WHY THERE IS NO 500 HERE
------------------------
A wall board has nobody standing at it. An error page on a factory TV stays up
until someone notices, so a band that cannot be read is reported *inside* a
200 — named in ``meta.degraded``, with that band's key set to null — and the
screen renders the rest. The only failures that get a status code are the ones
where there is no board to show at all: no company context, or no rights.

Two consequences worth knowing before changing this. The service catches per
band, so a HANA outage costs three bands and keeps the fourth, which is the
half of the screen that comes out of Postgres. And ``degraded`` is part of the
contract: the front end paints those tiles with the reason on their face, so
removing it would turn a visibly stale band into a silently wrong one.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .permissions import CanViewPlantBoard
from .services import PlantBoardService

logger = logging.getLogger(__name__)


class PlantBoardAPI(APIView):
    """The whole plant control board, in one read."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPlantBoard]

    def get(self, request):
        company_code = request.company.company.code
        try:
            board = PlantBoardService(company_code=company_code).build()
        except Exception as exc:  # noqa: BLE001
            # Only reached if the composition itself fails — every band already
            # catches its own. Logged with the company so a single company's
            # broken SAP config is distinguishable from a code fault.
            logger.exception("plant_board: board could not be composed for %s", company_code)
            return Response(
                {
                    "detail": "The plant board could not be read.",
                    "error": str(exc),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(board, status=status.HTTP_200_OK)
