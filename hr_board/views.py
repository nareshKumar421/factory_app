"""
hr_board/views.py

One endpoint for one screen.

``GET /api/v1/dashboards/hr-board/board/``

Read-only. Requires JWT authentication, a company context header, and either
the employee directory right, a labour gate right, or the matching board feed
right.

WHY THERE IS NO 500 HERE
------------------------
The same contract the other control boards keep: a section that cannot be read
is reported *inside* a 200 -- named in ``meta.degraded``, with that tile's key
set to null -- and the screen renders the rest. Only a failure that leaves no
board at all gets a status code.

``meta.withheld`` is its sibling and means something else: the tile was not read
because this reader may not see it. The two never share a list -- one sends an
operator to the server room, the other to an administrator.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from control_boards.permissions import CanReadBoard

from .permissions import CanViewHrBoard
from .services import HrBoardService

logger = logging.getLogger(__name__)


class HrBoardAPI(APIView):
    """The whole HR control board, in one read."""

    # Either the operational rights that already open these two registers, or
    # the board feed rights a dashboard-only login holds. `|` only ever widens,
    # so nobody who can read this today loses it.
    #
    # Safe to widen HERE specifically because this view IS the board: the whole
    # thing is composed server-side behind this one read, so a feed right buys
    # exactly this board and reaches no operational endpoint. The service then
    # withholds, per band, whichever feed the reader does not hold -- so this
    # class answers only "may you open it", and `meta.withheld` answers "what
    # may you see".
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewHrBoard
        | CanReadBoard("workforce", "labour", board="HR Control"),
    ]

    def get(self, request):
        company_code = request.company.company.code
        try:
            board = HrBoardService(
                company_code=company_code, user=request.user
            ).build()
        except Exception as exc:  # noqa: BLE001
            # Only reached if the composition itself fails -- every tile already
            # catches its own. Logged with the company so one company's bad data
            # is distinguishable from a code fault.
            logger.exception(
                "hr_board: board could not be composed for %s", company_code
            )
            return Response(
                {
                    "detail": "The HR board could not be read.",
                    "error": str(exc),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(board, status=status.HTTP_200_OK)
