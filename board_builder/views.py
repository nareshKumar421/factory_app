"""
board_builder/views.py

The builder's API: a palette, boards, and one composed read per board.

ONE READ ENDPOINT FOR EVERY BOARD EVER BUILT
---------------------------------------------
``GET .../boards/<slug>/data/`` is the whole of it. Every card on every board
runs behind this one view, which is what lets a feed right be honoured at all --
``control_boards/feeds.py`` allows it only inside a composed board service,
because a board that fans out from the browser cannot be granted narrowly. A
per-card endpoint would re-open that hole one card at a time, so there is not
one and there must never be one.

WHY THE DATA READ NEVER 500s
-----------------------------
Copied from ``admin_board/views.py`` and for the same reason: a card that
cannot be read is reported INSIDE a 200, named in ``meta.degraded`` with its
payload null, and the rest of the board draws. The only failures that get a
status code are the ones where there is no board to show at all -- no company
context, no rights, or no such board.

WHY A BOARD YOU MAY NOT SEE IS A 404
-------------------------------------
Every path that serves a board calls ``may_open`` and raises ``Http404`` when
it says no. A private draft's existence is not somebody else's business, and a
403 on a guessable slug confirms it.

A PUBLISHED board the reader holds no feed for does get a 403, with the
reason, because there is something there and the answer is "ask an
administrator" rather than "this does not exist".

There is no object-permission CLASS doing this. ``APIView`` never calls
``check_object_permissions`` by itself, so one would read as protection while
doing nothing -- see the note in ``permissions.py``.
"""

from __future__ import annotations

import logging

from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .constants import (
    BOARD_DENSITIES,
    BOARD_SURFACES,
    CARD_ACCENTS,
    GRID_MAX_COLUMNS,
    GRID_MIN_COLUMNS,
    GRID_MIN_ROWS,
    PAGE_MAX_ROWS,
    WALL_MAX_ROWS,
)
from .models import BoardMode, BoardPlacement, BoardVisibility, CustomBoard
from .permissions import CanBuildDashboards, may_build, may_open, readable_cards
from .serializers import (
    BoardListSerializer,
    BoardSerializer,
    CardSpecSerializer,
    unique_slug,
)
from .services import CustomBoardService

logger = logging.getLogger(__name__)


class CardCatalogueAPI(APIView):
    """``GET`` the palette: the cards this login may place, and the limits.

    Gated on the build right rather than on any feed. The palette is already
    filtered to what the reader may read, so it discloses nothing about the
    data -- but a login that cannot build has no use for it, and an endpoint
    with no gate is an endpoint somebody will later hang something on.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanBuildDashboards]

    def get(self, request):
        cards = readable_cards(request.user)
        return Response(
            {
                "cards": CardSpecSerializer(cards, many=True).data,
                # Ordered as the palette should group them, taken from the
                # cards actually offered -- a category whose every card is
                # withheld must not appear as an empty heading.
                "categories": list(dict.fromkeys(spec.category for spec in cards)),
                "accents": list(CARD_ACCENTS),
                "surfaces": list(BOARD_SURFACES),
                "densities": list(BOARD_DENSITIES),
                "limits": {
                    "min_columns": GRID_MIN_COLUMNS,
                    "max_columns": GRID_MAX_COLUMNS,
                    "min_rows": GRID_MIN_ROWS,
                    "max_rows": {
                        BoardMode.WALL: WALL_MAX_ROWS,
                        BoardMode.PAGE: PAGE_MAX_ROWS,
                    },
                },
            },
            status=status.HTTP_200_OK,
        )


def _visible_boards(request):
    """Every board this login may open, in this company.

    One query, and the ``Q`` is the access rule in the ORM: mine, plus what
    has been published to a group I am in (or to nobody in particular).
    ``_holds_any_feed`` is NOT applied here -- it walks the catalogue per
    board, which is fine for one board and wrong for a list -- so a published
    board whose every card the reader is refused still appears in the list and
    explains itself when opened. Listing a board's NAME is not a disclosure;
    its figures are.
    """
    user = request.user
    return (
        CustomBoard.objects.filter(
            is_active=True,
            company=request.company.company,
        )
        .filter(
            Q(owner=user)
            | Q(
                visibility=BoardVisibility.PUBLISHED,
                audience__isnull=True,
            )
            | Q(
                visibility=BoardVisibility.PUBLISHED,
                audience__in=user.groups.all(),
            )
        )
        .distinct()
    )


class BoardListCreateAPI(APIView):
    """``GET`` the boards this login may open; ``POST`` a new one."""

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        boards = (
            _visible_boards(request)
            .annotate(card_count=Count("placements", distinct=True))
            .select_related("owner")
            .order_by("-updated_at")
        )
        return Response(
            {
                "boards": BoardListSerializer(
                    boards, many=True, context={"request": request}
                ).data,
                # So the frontend can show or hide "New board" without a
                # second round trip, and without guessing from a 403.
                "can_build": may_build(request.user),
            }
        )

    def post(self, request):
        if not may_build(request.user):
            return Response(
                {"detail": CanBuildDashboards.message},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = BoardSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        board = serializer.save()
        return Response(
            BoardSerializer(board, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class BoardDetailAPI(APIView):
    """``GET``, ``PATCH`` and ``DELETE`` one board.

    Reading is open to its audience; changing it is the owner's alone. Not
    "anybody with the build right": a published board is somebody's work, and
    a colleague who wants a variant of it should duplicate it, which is one
    click and leaves the original alone.
    """

    # No object-permission class: ``APIView`` never calls
    # ``check_object_permissions`` by itself, so one listed here would read as
    # protection while doing nothing. The gate is ``may_open`` below, called
    # explicitly on every path, and it turns a refusal into a 404 rather than
    # the 403 a permission class would raise -- see the module docstring.
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_object(self, request, slug: str) -> CustomBoard:
        board = get_object_or_404(
            CustomBoard,
            slug=slug,
            company=request.company.company,
            is_active=True,
        )
        if not may_open(request.user, board):
            # 404 and not 403: see the module docstring.
            raise Http404
        return board

    def _owned(self, request, slug: str) -> CustomBoard:
        """The board, if this reader owns it.

        A 403 rather than a 404, unlike the read path: they can SEE this
        board, so pretending it is not there would be a mystery. The message
        names the duplicate route, which is the thing they actually want.
        """
        board = self.get_object(request, slug)
        if board.owner_id != request.user.id:
            raise PermissionDenied(
                "This board belongs to somebody else. Duplicate it to get your "
                "own copy to change."
            )
        return board

    def get(self, request, slug: str):
        board = self.get_object(request, slug)
        return Response(BoardSerializer(board, context={"request": request}).data)

    def patch(self, request, slug: str):
        board = self._owned(request, slug)
        serializer = BoardSerializer(
            board, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(BoardSerializer(board, context={"request": request}).data)

    def delete(self, request, slug: str):
        board = self._owned(request, slug)
        # Soft, via BaseModel.is_active. A board on a wall somewhere is a
        # thing people rely on, and "deleted by mistake" is recoverable by an
        # administrator this way and not by any other.
        board.is_active = False
        board.in_carousel = False
        board.updated_by = request.user
        board.save(update_fields=["is_active", "in_carousel", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class BoardPublishAPI(APIView):
    """``POST`` to publish, ``DELETE`` to take it back."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanBuildDashboards]

    def _owned(self, request, slug: str) -> CustomBoard | None:
        board = get_object_or_404(
            CustomBoard, slug=slug, company=request.company.company, is_active=True
        )
        return board if board.owner_id == request.user.id else None

    def post(self, request, slug: str):
        board = self._owned(request, slug)
        if board is None:
            return Response(
                {"detail": "Only a board's owner can publish it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not board.placements.exists():
            return Response(
                {
                    "detail": (
                        "There is nothing on this board yet. Add a card before "
                        "publishing it."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        audience = request.data.get("audience")
        if audience is not None:
            from django.contrib.auth.models import Group

            board.audience.set(Group.objects.filter(pk__in=audience))
        board.publish(request.user)
        return Response(BoardSerializer(board, context={"request": request}).data)

    def delete(self, request, slug: str):
        board = self._owned(request, slug)
        if board is None:
            return Response(
                {"detail": "Only a board's owner can unpublish it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        board.unpublish()
        return Response(BoardSerializer(board, context={"request": request}).data)


class BoardDuplicateAPI(APIView):
    """``POST`` to take a private copy of any board you can open.

    The copy is always private and always owned by whoever asked, whatever the
    original was. Duplicating somebody's published wall board must not publish
    yours by accident, and it must not put it on the rotation.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanBuildDashboards]

    def post(self, request, slug: str):
        original = get_object_or_404(
            CustomBoard, slug=slug, company=request.company.company, is_active=True
        )
        if not may_open(request.user, original):
            raise Http404

        placements = list(original.placements.all())
        copy = CustomBoard.objects.create(
            company=original.company,
            owner=request.user,
            created_by=request.user,
            updated_by=request.user,
            name=f"{original.name} (copy)",
            slug=unique_slug(original.company, f"{original.name} copy"),
            description=original.description,
            mode=original.mode,
            columns=original.columns,
            rows=original.rows,
            surface=original.surface,
            density=original.density,
            accent=original.accent,
            show_heading=original.show_heading,
            visibility=BoardVisibility.PRIVATE,
            in_carousel=False,
        )
        if placements:
            for placement in placements:
                placement.pk = None
                placement.board = copy
            BoardPlacement.objects.bulk_create(placements)

        return Response(
            BoardSerializer(copy, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class BoardDataAPI(APIView):
    """``GET`` one board's figures. The composed read -- see the module docstring."""

    # Same as BoardDetailAPI: the gate is ``may_open``, called below.
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, slug: str):
        board = get_object_or_404(
            CustomBoard,
            slug=slug,
            company=request.company.company,
            is_active=True,
        )
        if not may_open(request.user, board):
            # Distinguish "there is nothing here for you" from "there is
            # something here and you hold none of its rights". The first is a
            # 404 so a slug cannot be probed; the second is a 403 because the
            # board is listed for this reader and silence would be a mystery.
            if board.visibility == BoardVisibility.PUBLISHED:
                return Response(
                    {
                        "detail": (
                            "You hold none of the data rights this board's cards "
                            "need. Ask an administrator for the matching "
                            "Dashboards group."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            raise Http404

        try:
            payload = CustomBoardService(
                board,
                company_code=request.company.company.code,
                user=request.user,
            ).build()
        except Exception as exc:  # noqa: BLE001
            # Only reached if the composition itself fails -- every card
            # already catches its own. Logged with the board so one broken
            # layout is distinguishable from a code fault.
            logger.exception("board_builder: %s could not be composed", board.slug)
            return Response(
                {"detail": "This board could not be read.", "error": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(payload, status=status.HTTP_200_OK)


class CarouselBoardsAPI(APIView):
    """``GET`` the built boards that belong on the wall rotation.

    Its own endpoint rather than a filter on the list, because the caller is
    the carousel and what it needs is different: only WALL boards, only ones
    flagged onto the rotation, and only the identity -- the rotation fetches
    each board's figures itself, one at a time, as it comes round.

    UNLIKE THE LIST, THIS ONE CHECKS FEEDS PER BOARD
    The list deliberately does not: walking the catalogue for forty boards to
    decide what to show would cost more than it is worth, and a board's NAME
    is not a disclosure. Here it is worth it. The caller is an unattended
    screen with nobody in front of it, so a slide it cannot read would rotate
    into view and sit there for forty-five seconds as a grid of refusals.
    Better to hand it a shorter rotation. The set is small -- only boards
    somebody deliberately flagged -- so the cost is bounded.

    WHY THE CAROUSEL RIGHT DOES NOT OPEN A BUILT BOARD
    ``admin_board.can_view_board_carousel`` is honoured on the Admin and Plant
    boards' composed reads, and it is NOT honoured here. Those two boards are
    fixed: a developer chose every tile, so the right buys a known, audited
    set of figures. A built board is whatever its author dragged onto it this
    morning, so honouring one right across all of them would make that right a
    key into every feed in the product -- the exact thing
    ``admin_board/carousel.py`` forbids, arrived at by a different road.

    A wall screen showing a built board is therefore granted the FEED rights
    that board's cards need, which is precisely what per-feed rights were
    minted for. It is more setup than one permission, and it is the difference
    between a display login that can read four figures and one that can read
    everything anybody ever builds.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        boards = (
            _visible_boards(request)
            .filter(mode=BoardMode.WALL, in_carousel=True)
            .order_by("name")
        )
        return Response(
            {
                "boards": [
                    {"slug": board.slug, "name": board.name, "surface": board.surface}
                    for board in boards
                    if may_open(request.user, board)
                ]
            }
        )
