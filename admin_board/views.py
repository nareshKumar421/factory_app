"""
admin_board/views.py

One endpoint for one screen, and one for the bills under its dispatch tile.

``GET /api/v1/dashboards/admin-board/board/``
``GET /api/v1/dashboards/admin-board/dispatch-bills/?company=<code>``

The board is read-only. Requires JWT authentication, a company context header, and any one
of the four rights the board's own reports are gated on.

WHY THERE IS NO 500 HERE
------------------------
Copied from ``plant_board``, for the same reason: a section that cannot be read
is reported *inside* a 200 — named in ``meta.degraded``, with that tile's key
set to null — and the screen renders the rest. The only failures that get a
status code are the ones where there is no board to show at all: no company
context, or no rights.

Two consequences worth knowing before changing this. The service catches per
tile, so a HANA outage costs the output and storage tiles and keeps the cost
tile, which is the part that comes out of Postgres. And ``degraded`` is part of
the contract: the front end paints those tiles with the reason on their face,
so removing it would turn a visibly stale tile into a silently wrong one.

``meta.withheld`` is its sibling and means something else entirely: the tile was
not read because this reader may not see it. The two never share a list -- one
sends an operator to the server room, the other to an administrator.
"""

import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.models import UserCompany
from company.permissions import HasCompanyContext

from control_boards.permissions import CanReadBoard

from .carousel import CanViewBoardCarousel
from .constants import DISPATCH_COMPANIES
from .dispatch_bills import company_bills
from .permissions import CanViewAdminBoard
from .services import AdminBoardService

logger = logging.getLogger(__name__)


class AdminBoardAPI(APIView):
    """The whole admin control board, in one read."""

    # The wall screen is let in alongside the people who could always read
    # this, never instead of them: `|` only ever widens, so no existing
    # login loses anything. The settings views are untouched: the carousel never writes.
    #
    # Safe to widen HERE specifically because this view is the board --
    # the whole thing is composed server-side behind this one read, so the
    # carousel right buys exactly this board and nothing adjacent. See
    # admin_board/carousel.py for why that limit is the point.
    #
    # The third operand is the dashboard-only route: a login holding the board
    # READ rights for these feeds and nothing else opens this screen and reaches
    # no operational endpoint. The service then withholds, per band, whichever
    # feeds that reader does not hold -- so this class answers only "may you
    # open it", and `meta.withheld` answers "what may you see".
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewAdminBoard
        | CanViewBoardCarousel
        | CanReadBoard(
            "production_plan",
            "dispatch_plans",
            "stock",
            "factory_expense",
            board="Admin Control",
        ),
    ]

    def get(self, request):
        company_code = request.company.company.code
        try:
            board = AdminBoardService(
                company_code=company_code, user=request.user
            ).build()
        except Exception as exc:  # noqa: BLE001
            # Only reached if the composition itself fails — every tile already
            # catches its own. Logged with the company so one company's broken
            # SAP config is distinguishable from a code fault.
            logger.exception(
                "admin_board: board could not be composed for %s", company_code
            )
            return Response(
                {
                    "detail": "The admin board could not be read.",
                    "error": str(exc),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(board, status=status.HTTP_200_OK)


class AdminDispatchBillsAPI(APIView):
    """The bills behind one company's row on the Total dispatch panel.

    ``GET /api/v1/dashboards/admin-board/dispatch-bills/?company=JIVO_OIL``

    Same window as the board — the calendar month to today — and the same
    gate-out rows as the tile, one row per bill per truck.

    NARROWER THAN THE BOARD, ON PURPOSE. Only the dispatch feed opens it: the
    board's stock or expense rights do not, because those readers never see the
    dispatch tile at all. The carousel right does not either — it is a key cut
    for the board's one read, and a wall screen has no hand to click a row
    with (see ``carousel.py``). And the company must be one the reader belongs
    to, the same rule as the Logistics board's bill list: the tile's totals are
    shown to the whole room, a customer-by-customer list is not.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanReadBoard("dispatch_plans", board="Admin Control"),
    ]

    def get(self, request):
        code = (request.query_params.get("company") or "").strip().upper()
        if code not in DISPATCH_COMPANIES:
            return Response(
                {"detail": f"`company` must be one of {', '.join(DISPATCH_COMPANIES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company_id = (
            UserCompany.objects.filter(user=request.user, company__code=code, is_active=True)
            .values_list("company_id", flat=True)
            .first()
        )
        if company_id is None:
            return Response(
                {"detail": f"You are not a member of {code}, so its bills are not shown here."},
                status=status.HTTP_403_FORBIDDEN,
            )

        today = timezone.localdate()
        return Response(company_bills(company_id, code, today.replace(day=1), today))
