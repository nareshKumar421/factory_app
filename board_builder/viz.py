"""
board_builder/viz.py

The shapes a card is allowed to be, as data.

WHY A CARD RETURNS A SHAPE AND NOT A COMPONENT
-----------------------------------------------
The whole point of this app is that adding the twentieth card is a backend
change and nothing else -- somebody writes a query, registers it, and it is in
the palette that afternoon. That only holds if the frontend never has to learn
a new card. So a card does not describe how it looks; it returns one of a
CLOSED set of shapes, and the renderer knows all of them already.

The set is closed on purpose and it is small. Every entry below already exists
as a component on the three wall boards -- ``OpsMeter``, ``OpsBars``,
``OpsPair``, ``OpsMatrix`` -- so a card built here and a band on the Plant
board draw the same bar the same way. A card that genuinely needs a shape
nobody has drawn yet adds it HERE, once, with its renderer, and every future
card can then use it. That is the trade: no freedom per card, in exchange for
no frontend work per card.

WHAT A CARD ALWAYS RETURNS
--------------------------
:func:`figure` builds the envelope, and every card goes through it. A card that
cannot compute its number returns :func:`missing` instead -- never zero. The
boards' one hard rule is that an empty warehouse and an unreadable one must not
look the same, and a card returning ``0`` where it meant "no source" is exactly
that failure with nobody to catch it.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal

#: What a tag's colour means. Mirrors ``OpsTagTone`` in the frontend.
#: ``nil`` is "there is no source", which is not a condition and never amber.
Tone = Literal["neut", "ok", "warn", "bad", "nil"]

#: Which tint of the card's own hue a bar segment wears. Never a literal
#: colour: the card names a weight and the board supplies the hue, which is how
#: one card can sit on a teal board and an amber one without being recoloured
#: by hand.
Fill = Literal["main", "light", "mute"]


def tag(label: str, tone: Tone = "neut") -> dict:
    """The pill in the card's top-right -- a share, a comparison, a p90."""
    return {"label": label, "tone": tone}


def figure(
    value: str,
    *,
    unit: str = "",
    sub: str = "",
    tag: dict | None = None,  # noqa: A002 - matches the payload key
    viz: dict | None = None,
    note: str = "",
) -> dict:
    """A card that has a number.

    ``value`` is a STRING and already formatted. Formatting happens on the
    server because that is where the context is: whether this figure is rupees,
    tonnes or hours, whether a lakh should compact to "L", how many decimals
    still carry meaning. A renderer handed a bare float has to guess all three,
    and the plant boards learned that the hard way -- "NaN pcs ordered" on a
    wall looks like a number and survives until somebody notices.
    """
    return {
        "value": value,
        "unit": unit,
        "sub": sub,
        "tag": tag,
        "viz": viz,
        "note": note,
        "missing": None,
    }


def missing(reason: str, *, sub: str = "") -> dict:
    """A card with nothing behind it, and the reason on its face.

    Not an error and not a zero. The tile renders a rule where the figure goes
    and prints ``reason`` underneath, so a reader can tell "nobody has
    configured this" from "the answer is none".
    """
    return {
        "value": "",
        "unit": "",
        "sub": sub,
        "tag": None,
        "viz": None,
        "note": "",
        "missing": reason,
    }


# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------


def meter(segments: Iterable[tuple[str, float, Fill]]) -> dict:
    """One quantity split into its parts, as a single composition bar.

    Segments share one scale and are laid in order, so the bar reads as the
    WHOLE of something. Use it for "of the total, this much is X" and never for
    unrelated quantities side by side -- two things on one bar that do not sum
    is the chart that lies most easily.
    """
    return {
        "kind": "meter",
        "segments": [
            {"label": label, "value": float(value), "fill": fill}
            for label, value, fill in segments
        ],
    }


def bars(days: Iterable[tuple[str, float, bool]]) -> dict:
    """A short column chart -- the recent window, with today picked out.

    Heights are a share of the TALLEST column in the window, not of a target.
    That is the only question a chart this size can answer honestly: "is today
    normal for this week". A card wanting "against target" wants :func:`pair`.
    """
    return {
        "kind": "bars",
        "days": [
            {"label": label, "value": float(value), "current": bool(current)}
            for label, value, current in days
        ],
    }


def pair(rows: Iterable[tuple[str, float, Fill]], *, note: str = "") -> dict:
    """Two or three figures as stacked bars on ONE shared scale.

    Scaled against the largest of the set, so the visible gap between them is
    the difference and nothing else. Two bars on two scales would make any gap
    look like whatever the author wanted it to look like.
    """
    return {
        "kind": "pair",
        "rows": [
            {"label": label, "value": float(value), "fill": fill}
            for label, value, fill in rows
        ],
        "note": note,
    }


def matrix(
    *,
    columns: list[str],
    rows: list[tuple[str, list[int | None]]],
    caption: str = "",
) -> dict:
    """Counts across two dimensions -- age bands down, stages across.

    Bands must be EXCLUSIVE windows, so the rows sum to the total. Cumulative
    floors were tried on the freight board and rejected: three counts of the
    same overlapping set read as duplicated data.
    """
    return {
        "kind": "matrix",
        "columns": list(columns),
        "rows": [{"label": label, "cells": list(cells)} for label, cells in rows],
        "caption": caption,
    }


def table(
    *,
    columns: list[str],
    rows: list[list[str]],
    aligns: list[Literal["left", "right"]] | None = None,
) -> dict:
    """A short list -- the worst five, the oldest four.

    Every cell is a formatted string for the reason :func:`figure` gives. Keep
    it to what fits without scrolling: a card is a headline with its evidence
    under it, and a table long enough to scroll belongs behind a drill, not on
    the face of a tile nobody is standing at.
    """
    return {
        "kind": "table",
        "columns": list(columns),
        "rows": [[str(cell) for cell in row] for row in rows],
        "aligns": list(aligns) if aligns else ["left"] * len(columns),
    }


def split(parts: Iterable[tuple[str, str]]) -> dict:
    """Two to four small labelled figures, for a card answering one question
    in several parts -- the three legs of a stage clock, say.

    Not a table: these share the card's own headline rather than standing
    alone, so they render as a row of small figures under it.
    """
    return {
        "kind": "split",
        "parts": [{"label": label, "value": value} for label, value in parts],
    }


def nothing() -> dict:
    """No visualisation -- a card that is one big number and its subtitle.

    Returned explicitly rather than by passing ``viz=None``, so a card author
    saying "this is deliberately just a figure" reads differently from one who
    forgot.
    """
    return {"kind": "none"}


#: Every kind the renderer knows. Asserted against in the tests so a shape
#: added here without a renderer fails the build rather than a wall screen.
VIZ_KINDS: frozenset[str] = frozenset(
    {"meter", "bars", "pair", "matrix", "table", "split", "none"}
)


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Check a card's own output before it reaches a screen.

    Cheap, and worth it: a card is somebody's query written six weeks after
    this file, and the failure mode of a malformed payload is a blank tile on a
    wall rather than a stack trace anybody sees.
    """
    viz = payload.get("viz")
    if viz is not None and viz.get("kind") not in VIZ_KINDS:
        raise ValueError(
            f"Card returned an unknown visualisation {viz.get('kind')!r}. "
            f"Known kinds: {', '.join(sorted(VIZ_KINDS))}."
        )
    if payload.get("missing") is None and not payload.get("value"):
        raise ValueError(
            "A card must return either a value or a reason it has none. "
            "Use viz.missing(reason) rather than an empty figure."
        )
    return payload
