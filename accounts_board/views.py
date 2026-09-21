"""
accounts_board/views.py

One endpoint for one screen.

``GET /api/v1/dashboards/accounts-board/board/?period=latest``
``GET /api/v1/dashboards/accounts-board/board/?year=2026&month=9``

Read-only. Requires JWT authentication, a company context header, and either a
cash book right or the matching board feed right.

WHY THERE IS NO 500 HERE
------------------------
The contract the other control boards keep: a section that cannot be read is
reported *inside* a 200 -- named in ``meta.degraded``, with that tile's key set
to null -- and the screen renders the rest. Only a failure that leaves no board
at all gets a status code.

``meta.withheld`` is its sibling and means something else: the tile was not read
because this reader may not see it. The two never share a list -- one sends an
operator to the server room, the other to an administrator.

THE PERIOD IS VALIDATED, NOT TRUSTED
-------------------------------------
``month=13`` and ``year=0`` are rejected with a 400 rather than being clamped.
Clamping would answer a question nobody asked and label the answer with the
month they did ask for, which on a finance screen is worse than an error. A
request with no period at all is not an error: it means the whole book.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from admin_board.carousel import CanViewBoardCarousel
from company.permissions import HasCompanyContext
from control_boards.permissions import CanReadBoard

from .permissions import CanViewAccountsBoard
from .services import AccountsBoardService

logger = logging.getLogger(__name__)

#: SAP B1's own lower bound for a posting date is 1900; anything below it is a
#: typo rather than a period. The upper bound is deliberately generous -- a
#: forward-dated voucher is a real thing -- and only excludes nonsense.
MIN_YEAR = 1900
MAX_YEAR = 2999


def _parse_period(request):
    """What month to report on, or a 400 message.

    Returns ``(period, error)`` where period is a ``(year, month)`` pair,
    :data:`AccountsBoardService.LATEST`, or ``None`` for the whole book.

    THREE ANSWERS, NOT TWO
    ``?period=latest``   the newest month this book has -- the screen's default,
                         resolved on the server so the page opens on September
                         in one round trip rather than fetching the whole book,
                         reading the month list off it and fetching again.
    ``?year=&month=``    that month.
    nothing              the whole book, which is a real choice here.
    """
    raw_period = request.query_params.get("period")
    raw_year = request.query_params.get("year")
    raw_month = request.query_params.get("month")

    if raw_period is not None:
        if raw_period != AccountsBoardService.LATEST:
            return None, "period may only be 'latest'. Use year and month otherwise."
        if raw_year is not None or raw_month is not None:
            return None, "Send period=latest or year and month, not both."
        return AccountsBoardService.LATEST, None

    if raw_year is None and raw_month is None:
        return None, None
    if raw_year is None or raw_month is None:
        return None, "Send year and month together, or neither."

    try:
        year = int(raw_year)
        month = int(raw_month)
    except (TypeError, ValueError):
        return None, "year and month must be whole numbers."

    if not MIN_YEAR <= year <= MAX_YEAR:
        return None, f"year must be between {MIN_YEAR} and {MAX_YEAR}."
    if not 1 <= month <= 12:
        return None, "month must be between 1 and 12."

    return (year, month), None


class AccountsBoardAPI(APIView):
    """The whole accounts dashboard, in one read."""

    # Either the cash book rights that already open this register, or the board
    # feed right a dashboard-only login holds. `|` only ever widens, so nobody
    # who can read this today loses it.
    #
    # Safe to widen HERE specifically because this view IS the board: the whole
    # thing is composed server-side behind this one read, so a feed right buys
    # exactly this screen and reaches no operational endpoint. The service then
    # masks the per-person names for a reader who holds only the feed -- so this
    # class answers "may you open it", and ``meta.names_visible`` answers "may
    # you see who".
    #
    # THE CAROUSEL RIGHT IS HONOURED FOR THE SAME REASON, AND ONLY HERE.
    # ``admin_board.carousel`` states the rule: that right may only be accepted
    # by an endpoint the wall rotation actually reads, and only for reading.
    # This app has exactly one endpoint, it is this one, it is a GET, and it
    # writes nothing -- the same shape that makes Admin and Plant safe places
    # to honour it. Nothing else in the cash book accepts it, so a display
    # login gets this board and no route into the register behind it.
    #
    # And it gets no names: the wall screen holds neither ``can_view_cash_book``
    # nor anything that implies it, so ``may_name_people`` is False and every
    # per-person row comes back masked. A television in a corridor showing who
    # is holding the factory's cash is the precise outcome this arrangement
    # exists to prevent.
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewAccountsBoard
        | CanViewBoardCarousel
        | CanReadBoard("cash_book", board="Accounts"),
    ]

    def get(self, request):
        period, error = _parse_period(request)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

        service = AccountsBoardService(
            request.company.company, user=request.user, period=period
        )
        return Response(service.build())
