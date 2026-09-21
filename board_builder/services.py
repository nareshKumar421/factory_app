"""
board_builder/services.py

Reading one composed board: every card on it, in one request, with a named
reason for each one that is not there.

THIS IS THE ONLY PLACE A CARD RUNS
-----------------------------------
There is one endpoint for every board anybody ever builds, and it is this
service behind it. That is not an optimisation, it is the security boundary:
``control_boards/feeds.py`` states the rule that a feed right may only ever be
honoured INSIDE a composed board service, never on an operational view, because
a board that fans out from the browser to a dozen endpoints cannot be granted
narrowly. A per-card endpoint would be exactly that fan-out, invented one card
at a time.

FOUR WAYS A CARD CAN BE ABSENT, AND THEY ARE FOUR LISTS
--------------------------------------------------------
``control_boards.sections`` already insists that "we tried and could not read
it" (``degraded``) and "you may not see it" (``withheld``) never share a list,
because one sends an operator to the server room and the other to an
administrator. A board somebody composed themselves adds two more, and they
are the author's problem rather than the reader's:

    meta.degraded   -- the source did not answer
    meta.withheld   -- this reader may not see it
    meta.retired    -- the card was removed from the catalogue
    meta.misplaced  -- the card no longer fits where it was put

The last two only ever happen across a deploy: a card is retired, or its
footprint grows and the 2x1 an author placed in the last column is now a 3x1
hanging off the edge. Both are reported and the card is dropped, because the
alternative -- rendering it anyway -- is two cards on one square with nothing
anywhere saying so. Neither is an error: the board still returns 200 and the
rest of it draws.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from control_boards.sections import SectionBuilder

from . import catalogue, viz
from .catalogue import CardContext, CardSpec
from .models import CustomBoard

logger = logging.getLogger(__name__)


@dataclass
class _Placed:
    """A placement that survived the geometry check, with its spec attached."""

    placement: object
    spec: CardSpec


class CustomBoardService(SectionBuilder):
    """Build one board's payload for one reader.

    ``user=None`` withholds nothing, matching every other board service in the
    product -- a service built without a request (a management command, a
    test) behaves as it did before feed rights existed.
    """

    def __init__(self, board: CustomBoard, *, company_code: str, user=None):
        self.board = board
        self.company_code = company_code
        self.user = user
        self._init_sections()
        self._retired: list[str] = []
        self._misplaced: list[str] = []

    # -- geometry -----------------------------------------------------------

    def _fits(self, placement, spec: CardSpec, taken: set[tuple[int, int]]) -> bool:
        """Whether this card's footprint is inside the grid and unoccupied.

        Checked on READ and not only on write, because a card's footprint is
        catalogue data: it can change under a saved board without anything
        touching the board's own rows. The check is cheap -- a board holds a
        couple of dozen cells -- and it is the difference between an author
        being told to move a tile and a reader seeing two stacked on one.
        """
        if placement.column + spec.columns > self.board.columns:
            return False
        if placement.row + spec.rows > self.board.rows:
            return False
        cells = {
            (placement.column + dx, placement.row + dy)
            for dx in range(spec.columns)
            for dy in range(spec.rows)
        }
        if cells & taken:
            return False
        taken |= cells
        return True

    def _resolve(self) -> list[_Placed]:
        """The placements that can actually be drawn, in reading order.

        Row then column, so the first card placed wins a collision. Arbitrary,
        but deterministic, which is what matters: the alternative is a board
        that drops a different card depending on row ordering in the database.
        """
        placed: list[_Placed] = []
        taken: set[tuple[int, int]] = set()

        for placement in self.board.placements.all():
            spec = catalogue.get(placement.card_key)
            if spec is None:
                self._retired.append(placement.card_key)
                continue
            if not self._fits(placement, spec, taken):
                self._misplaced.append(placement.card_key)
                continue
            placed.append(_Placed(placement=placement, spec=spec))

        return placed

    # -- the read -----------------------------------------------------------

    def _card(self, entry: _Placed) -> dict:
        """One card's payload, or the reason it has none.

        The build is wrapped by ``SectionBuilder.section``, so a card that
        raises costs its own tile and never the board -- a wall with eleven
        good tiles and one that says why it is dark is worth far more than a
        503 nobody is standing there to read.
        """
        placement, spec = entry.placement, entry.spec
        context = CardContext(
            company_code=self.company_code,
            user=self.user,
            options=spec.coerce_options(placement.options),
        )

        payload = self.section(
            spec.key,
            lambda: viz.validate(spec.build(context)),
            needs_sap=spec.needs_sap,
            feed=spec.feed,
        )

        return {
            "id": placement.id,
            "card_key": spec.key,
            "title": placement.title or spec.title,
            "accent": placement.accent or self.board.accent,
            "column": placement.column,
            "row": placement.row,
            "columns": spec.columns,
            "rows": spec.rows,
            "category": spec.category,
            "note": spec.note,
            # None means the section builder did not run it. Which of the two
            # reasons applies is in meta, named by `spec.key`.
            "payload": payload,
        }

    def build(self) -> dict:
        board = self.board
        cards = [self._card(entry) for entry in self._resolve()]

        if self._retired:
            self.warn(
                "This board holds "
                f"{len(self._retired)} card(s) that no longer exist. Open it in "
                "the builder to clear them."
            )
        if self._misplaced:
            self.warn(
                f"{len(self._misplaced)} card(s) no longer fit where they were "
                "placed and are not shown. Open it in the builder to move them."
            )

        meta = self.section_meta()
        meta["retired"] = list(self._retired)
        meta["misplaced"] = list(self._misplaced)
        meta["generated_at"] = timezone.now().isoformat()

        return {
            "board": {
                "slug": board.slug,
                "name": board.name,
                "description": board.description,
                "mode": board.mode,
                "columns": board.columns,
                "rows": board.rows,
                "surface": board.surface,
                "density": board.density,
                "accent": board.accent,
                "show_heading": board.show_heading,
                "visibility": board.visibility,
                "in_carousel": board.in_carousel,
            },
            "cards": cards,
            "meta": meta,
        }
