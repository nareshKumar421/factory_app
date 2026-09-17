"""Recording and reading what a godown keeper declares he is sending out.

Every write goes through here for one reason: the per-warehouse manager check.
A keeper may only declare movements out of a warehouse he runs, and a view that
wrote to the model directly would be one refactor away from losing that.

Reads are deliberately NOT scoped. Visibility is unrestricted across the
warehouse module — lists show everything, only the actions are gated — and a
register whose totals change depending on who is looking is useless for the
dashboard this feeds. See ``services/warehouse_scope`` for the rules themselves.

Two things this module will not do, both deliberate:

* **It never posts to SAP.** The document is a statement of intent. The move
  itself, when it has to happen through the system, is
  ``services/transfer_request_service``.
* **It never touches HANA on a write.** The item and warehouse names ride along
  from the pickers, so an unreachable HANA leaves the keeper able to type but not
  to search — rather than unable to file anything at all.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from django.db import transaction
from django.db.models import (
    DecimalField,
    ExpressionWrapper,
    F,
    Prefetch,
    Q,
    Sum,
)
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from company.models import Company

from ..models_pf_movement import (
    PFMovementDestinationKind,
    PFStockMovement,
    PFStockMovementEvent,
    PFStockMovementLine,
)
from . import warehouse_scope
from .wms_hana_reader import WMSHanaReader

logger = logging.getLogger(__name__)

# SAP item group for finished goods. The same code in all three company schemas
# — verified live: 102 is 'FINISHED' in Oil, Mart and Beverages (105 is
# PACKAGING MATERIAL, 106 RAW MATERIAL). The group is what SAP classifies on;
# an item-code prefix is only a naming convention.
FG_ITEM_GROUP_CODE = 102

# The production-finished floor, which is what this page was built for. Only a
# default: the source is whichever warehouse the keeper manages, so the Gupta
# godown keeper can file his own outward moves from the same screen.
DEFAULT_PF_WAREHOUSE = "BH-PF"


def default_source_warehouse() -> str:
    """The warehouse the form opens on."""
    from django.conf import settings

    return str(
        getattr(settings, "PF_MOVEMENT_WAREHOUSE", DEFAULT_PF_WAREHOUSE)
    ).strip().upper()


# ---------------------------------------------------------------------------
# Pickers (the only paths here that touch HANA)
# ---------------------------------------------------------------------------

def search_fg_items(
    *,
    company_code: str,
    search: str = "",
    warehouse_code: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    """Finished-goods items from SAP, for the item picker.

    Each row carries the warehouse's SAP on-hand — so the keeper can see what
    SAP thinks is there beside the pieces he is about to declare — plus the two
    factors a line snapshots: ``pieces_per_box`` (``OITM.SalFactor2``) and
    ``litres_per_piece`` (``OITM.SalPackUn``, gated on ``U_IsLitre``).

    The on-hand needs no conversion to be compared with what he types: SAP
    counts ``OITW.OnHand`` in the inventory UoM, which is pieces, and so does
    this register.
    """
    reader = WMSHanaReader(company_code=company_code)
    return reader.search_items_in_group(
        item_group_code=FG_ITEM_GROUP_CODE,
        search=search,
        warehouse_code=(warehouse_code or default_source_warehouse()),
        limit=limit,
    )


def list_destinations() -> List[Dict]:
    """Every active warehouse of every active company, for the destination picker.

    Spans companies on purpose. The move this page exists to record — the PF
    floor pushing finished stock into the Gupta godown — crosses from Oil into
    Mart, so a picker limited to the active company could not express it.

    One company failing to answer does not empty the list: its warehouses are
    reported missing and the rest are still offered, because a keeper who cannot
    name Mart's godown can still file the three moves inside his own company.
    """
    out: List[Dict] = []
    for company in Company.objects.filter(is_active=True).order_by("name"):
        try:
            warehouses = WMSHanaReader(company_code=company.code).get_warehouses()
        except Exception as exc:  # noqa: BLE001 — one company must not sink the rest
            logger.warning(
                "Destination warehouses unavailable for %s: %s", company.code, exc
            )
            out.append(
                {
                    "company_id": company.id,
                    "company_code": company.code,
                    "company_name": company.name,
                    "warehouses": [],
                    "error": str(exc),
                }
            )
            continue
        out.append(
            {
                "company_id": company.id,
                "company_code": company.code,
                "company_name": company.name,
                "warehouses": warehouses,
                "error": "",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Reading the register
# ---------------------------------------------------------------------------

def list_movements(
    *,
    company_code: Optional[str] = None,
    from_warehouse: Optional[str] = None,
    to_warehouse: Optional[str] = None,
    destination_kind: Optional[str] = None,
    date_from=None,
    date_to=None,
    search: str = "",
    include_cancelled: bool = False,
):
    """Declared movements, filtered the way the page filters them.

    ``company_code`` is the *source* company. Left off, the list spans
    companies — which is what the dashboard wants, and what someone looking for
    "everything that left the plant today" means.
    """
    rows = PFStockMovement.objects.select_related(
        "company", "to_company", "created_by", "updated_by", "cancelled_by"
    ).prefetch_related(
        Prefetch("lines", queryset=PFStockMovementLine.objects.order_by("item_code"))
    )

    if company_code:
        rows = rows.filter(company__code=company_code)
    if not include_cancelled:
        rows = rows.filter(is_active=True)
    if from_warehouse:
        rows = rows.filter(from_warehouse=from_warehouse.strip().upper())
    if to_warehouse:
        rows = rows.filter(to_warehouse=to_warehouse.strip().upper())
    if destination_kind:
        rows = rows.filter(destination_kind=_clean_kind(destination_kind))
    if date_from:
        rows = rows.filter(movement_date__gte=date_from)
    if date_to:
        rows = rows.filter(movement_date__lte=date_to)

    term = (search or "").strip()
    if term:
        # Matches the document and its contents both: "where did PET 1L go" and
        # "show me PFM-...-0007" are the same search box on the page.
        rows = rows.filter(
            Q(entry_no__icontains=term)
            | Q(vehicle_no__icontains=term)
            | Q(reference__icontains=term)
            | Q(remarks__icontains=term)
            | Q(to_warehouse__icontains=term)
            | Q(to_warehouse_name__icontains=term)
            | Q(lines__item_code__icontains=term)
            | Q(lines__item_name__icontains=term)
        ).distinct()

    return rows.order_by("-movement_date", "-id")


def summarise(movements) -> Dict:
    """Headline figures for the page, computed in one query over the same filter.

    ``order_by()`` strips the list's ordering before the subquery. Searching
    makes the list ``DISTINCT``, and Postgres refuses a ``SELECT DISTINCT id``
    ordered by a column that is not in the select list — a failure sqlite does
    not reproduce.
    """
    lines = PFStockMovementLine.objects.filter(
        movement__in=movements.order_by().values("id")
    )
    # Litres are summed in the database as `pieces * litres_per_piece` rather
    # than in Python off the objects, so the figure does not depend on how many
    # rows the page happened to fetch. Lines SAP holds no volume for contribute
    # nothing: `litres_per_piece` is null there and null * n is null, which Sum
    # skips.
    litre_expr = ExpressionWrapper(
        F("pieces") * F("litres_per_piece"),
        output_field=DecimalField(max_digits=20, decimal_places=6),
    )
    # Aliased `*_sum`, not `pieces`: an alias that shadows the field name makes
    # the `F("pieces")` inside `litre_expr` resolve to the aggregate instead of
    # the column, and Django refuses it as "'pieces' is an aggregate".
    totals = lines.aggregate(pieces_sum=Sum("pieces"), litres_sum=Sum(litre_expr))
    # Split by kind in the same pass: "how much went to godowns and how much
    # straight out" is the first thing anyone asks of this register, and the two
    # are reconciled against different SAP documents.
    dispatched = lines.filter(
        movement__destination_kind=PFMovementDestinationKind.DISPATCH
    ).aggregate(pieces_sum=Sum("pieces"), litres_sum=Sum(litre_expr))

    total_pieces = int(totals["pieces_sum"] or 0)
    dispatched_pieces = int(dispatched["pieces_sum"] or 0)
    total_litres = Decimal(totals["litres_sum"] or 0)
    dispatched_litres = Decimal(dispatched["litres_sum"] or 0)
    return {
        "movements": movements.count(),
        "total_pieces": total_pieces,
        "dispatched_pieces": dispatched_pieces,
        "to_godown_pieces": total_pieces - dispatched_pieces,
        "total_litres": _round_litres(total_litres),
        "dispatched_litres": _round_litres(dispatched_litres),
        "to_godown_litres": _round_litres(total_litres - dispatched_litres),
    }


def _round_litres(value: Decimal) -> str:
    """Litres to 3 places, as a string.

    A string rather than a float: litres come from a 6-place SAP factor, and
    JSON floats would round 0.8242-litre pouches differently on every client.
    """
    return str(Decimal(value).quantize(Decimal("0.001")))


def get_movement(*, pk: int, company_code: Optional[str] = None):
    """One document with its lines and its trail, or None."""
    rows = PFStockMovement.objects.select_related(
        "company", "to_company", "created_by", "updated_by", "cancelled_by"
    ).prefetch_related("lines", "events__changed_by")
    if company_code:
        rows = rows.filter(company__code=company_code)
    return rows.filter(pk=pk).first()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

@transaction.atomic
def create_movement(
    *,
    user,
    company: Company,
    from_warehouse: str,
    lines: List[Dict],
    to_warehouse: str = "",
    to_company: Optional[Company] = None,
    destination_kind: str = PFMovementDestinationKind.GODOWN,
    movement_date=None,
    from_warehouse_name: str = "",
    to_warehouse_name: str = "",
    vehicle_no: str = "",
    reference: str = "",
    remarks: str = "",
) -> PFStockMovement:
    """File one declared consignment — to a godown, or straight out."""
    destination_kind, from_warehouse, to_warehouse, to_company = _clean_route(
        from_warehouse=from_warehouse,
        to_warehouse=to_warehouse,
        company=company,
        to_company=to_company,
        destination_kind=destination_kind,
    )
    movement_date = _clean_date(movement_date)
    cleaned = _clean_lines(lines)

    warehouse_scope.assert_manages(
        user,
        company.code,
        [from_warehouse],
        action="record stock movements out of",
    )

    movement = PFStockMovement.objects.create(
        company=company,
        entry_no=PFStockMovement.generate_entry_no(),
        movement_date=movement_date,
        from_warehouse=from_warehouse,
        destination_kind=destination_kind,
        to_warehouse=to_warehouse,
        to_company=to_company,
        from_warehouse_name=from_warehouse_name or "",
        # Blank on a dispatch whatever the caller sent — there is no godown for
        # the name to describe.
        to_warehouse_name=(to_warehouse_name or "") if to_warehouse else "",
        vehicle_no=vehicle_no or "",
        reference=reference or "",
        remarks=remarks or "",
        created_by=user if _is_real(user) else None,
    )
    _write_lines(movement, cleaned)
    _log(movement, action=PFStockMovementEvent.Action.CREATED, user=user, note=remarks)
    logger.info(
        "PF movement %s filed: %s -> %s (%s), %d line(s)",
        movement.entry_no,
        from_warehouse,
        movement.destination_display,
        to_company.code if to_company else "dispatch",
        len(cleaned),
    )
    return movement


@transaction.atomic
def update_movement(
    *,
    user,
    movement: PFStockMovement,
    destination_kind: Optional[str] = None,
    to_warehouse: Optional[str] = None,
    to_company: Optional[Company] = None,
    lines: Optional[List[Dict]] = None,
    movement_date=None,
    to_warehouse_name: Optional[str] = None,
    vehicle_no: Optional[str] = None,
    reference: Optional[str] = None,
    remarks: Optional[str] = None,
    note: str = "",
) -> PFStockMovement:
    """Correct a declaration that has not been retracted.

    The source warehouse is not editable. Moving a document to another floor
    would rewrite whose declaration it is, and the manager check that let it be
    filed was made against the original — retract it and file a new one instead.

    Switching a godown move to a direct dispatch **drops** its destination
    rather than asking the caller to blank three fields; switching back demands
    a real one. Either way the trail keeps where it used to be going.

    Lines are replaced wholesale when supplied. A partial line edit would need
    the client to track line ids through a form the keeper rebuilds freely, and
    the trail keeps what the document said before either way.
    """
    if not movement.is_active:
        raise ValidationError(
            {"detail": f"{movement.entry_no} was cancelled and can no longer be edited."}
        )

    warehouse_scope.assert_manages(
        user,
        movement.company.code,
        [movement.from_warehouse],
        action="edit stock movements out of",
    )

    fields = ["updated_by", "updated_at"]

    if destination_kind is not None or to_warehouse is not None or to_company is not None:
        target_kind = _clean_kind(
            destination_kind if destination_kind is not None else movement.destination_kind
        )
        if target_kind == PFMovementDestinationKind.DISPATCH:
            # Deliberately NOT inherited from the stored document: asking for a
            # dispatch is asking for the destination to go away. Anything the
            # caller still sent for it reaches `_clean_route` and is refused, so
            # a contradictory request fails loudly instead of half-applying.
            target_to = to_warehouse if to_warehouse is not None else ""
            target_company = to_company
        else:
            target_to = (
                to_warehouse if to_warehouse is not None else movement.to_warehouse
            )
            target_company = to_company if to_company is not None else movement.to_company

        target_kind, _, cleaned_to, cleaned_company = _clean_route(
            from_warehouse=movement.from_warehouse,
            to_warehouse=target_to,
            company=movement.company,
            to_company=target_company,
            destination_kind=target_kind,
        )
        movement.destination_kind = target_kind
        movement.to_warehouse = cleaned_to
        movement.to_company = cleaned_company
        fields += ["destination_kind", "to_warehouse", "to_company"]
        if not cleaned_to:
            # The stored name described a godown this document no longer goes to.
            movement.to_warehouse_name = ""
            fields.append("to_warehouse_name")

    # Skipped on a dispatch: there is no godown for the name to describe, and
    # the branch above has already blanked it.
    if to_warehouse_name is not None and not movement.is_dispatch:
        movement.to_warehouse_name = to_warehouse_name
        fields.append("to_warehouse_name")
    if movement_date is not None:
        movement.movement_date = _clean_date(movement_date)
        fields.append("movement_date")
    if vehicle_no is not None:
        movement.vehicle_no = vehicle_no
        fields.append("vehicle_no")
    if reference is not None:
        movement.reference = reference
        fields.append("reference")
    if remarks is not None:
        movement.remarks = remarks
        fields.append("remarks")

    movement.updated_by = user if _is_real(user) else None
    # De-duplicated: `to_warehouse_name` can be appended by either branch above,
    # and `update_fields` must not repeat a column.
    movement.save(update_fields=list(dict.fromkeys(fields)))

    if lines is not None:
        cleaned = _clean_lines(lines)
        PFStockMovementLine.objects.filter(movement=movement).delete()
        _write_lines(movement, cleaned)

    _log(movement, action=PFStockMovementEvent.Action.UPDATED, user=user, note=note)
    return movement


@transaction.atomic
def cancel_movement(
    *, user, movement: PFStockMovement, reason: str = ""
) -> PFStockMovement:
    """Retract a declaration, keeping it and its trail.

    Deactivates rather than deletes: that the keeper once said this stock was
    going out is itself a fact the dashboard has to be able to see, and a row
    that vanishes takes the question with it.
    """
    warehouse_scope.assert_manages(
        user,
        movement.company.code,
        [movement.from_warehouse],
        action="cancel stock movements out of",
    )
    if not movement.is_active:
        return movement

    movement.is_active = False
    movement.cancelled_at = timezone.now()
    movement.cancelled_by = user if _is_real(user) else None
    movement.cancellation_reason = reason or ""
    movement.save(
        update_fields=[
            "is_active", "cancelled_at", "cancelled_by",
            "cancellation_reason", "updated_at",
        ]
    )
    _log(
        movement,
        action=PFStockMovementEvent.Action.CANCELLED,
        user=user,
        note=reason,
    )
    return movement


@transaction.atomic
def restore_movement(
    *, user, movement: PFStockMovement, note: str = ""
) -> PFStockMovement:
    """Put a retracted declaration back, for the cancel that was a misclick."""
    warehouse_scope.assert_manages(
        user,
        movement.company.code,
        [movement.from_warehouse],
        action="restore stock movements out of",
    )
    if movement.is_active:
        return movement

    movement.is_active = True
    movement.cancelled_at = None
    movement.cancelled_by = None
    movement.cancellation_reason = ""
    movement.updated_by = user if _is_real(user) else None
    movement.save(
        update_fields=[
            "is_active", "cancelled_at", "cancelled_by", "cancellation_reason",
            "updated_by", "updated_at",
        ]
    )
    _log(movement, action=PFStockMovementEvent.Action.RESTORED, user=user, note=note)
    return movement


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _clean_kind(destination_kind) -> str:
    kind = (destination_kind or PFMovementDestinationKind.GODOWN).strip().upper()
    if kind not in PFMovementDestinationKind.values:
        raise ValidationError(
            {"destination_kind": f"{kind} is not a destination this page knows."}
        )
    return kind


def _clean_route(
    *, from_warehouse: str, to_warehouse: str, company, to_company, destination_kind
):
    """Validate the route, and return it in the shape the kind demands.

    A dispatch is returned with **no** destination — not a placeholder code.
    Anything the caller sent for it is refused rather than quietly dropped: a
    destination that silently disappears is how a document ends up saying
    something nobody typed.
    """
    kind = _clean_kind(destination_kind)
    src = (from_warehouse or "").strip().upper() or default_source_warehouse()
    dst = (to_warehouse or "").strip().upper()

    if kind == PFMovementDestinationKind.DISPATCH:
        if dst:
            raise ValidationError(
                {
                    "to_warehouse": (
                        "A direct dispatch has no destination godown — "
                        f"remove {dst}, or send it to that godown instead."
                    )
                }
            )
        if to_company is not None:
            raise ValidationError(
                {"to_company": "A direct dispatch has no destination company."}
            )
        return kind, src, "", None

    if not dst:
        raise ValidationError({"to_warehouse": "Name the godown the stock is going to."})
    if to_company is None:
        raise ValidationError(
            {"to_company": "Name the company the destination godown belongs to."}
        )
    # Same warehouse of the same company is not a movement. The same code in a
    # *different* company is — BH-PF exists in both Oil and Mart, and stock
    # crossing between their books is a real move whatever the code says.
    if dst == src and getattr(to_company, "id", None) == getattr(company, "id", None):
        raise ValidationError(
            {"to_warehouse": f"{dst} is where the stock already is."}
        )
    return kind, src, dst, to_company


def _clean_date(movement_date):
    movement_date = movement_date or timezone.localdate()
    # A future date is allowed and is the normal case: the whole point is
    # declaring tomorrow's dispatch tonight. Only an absurd one is refused, and
    # a year out is the line — anything beyond it is a mistyped year.
    if (movement_date - timezone.localdate()).days > 365:
        raise ValidationError(
            {"movement_date": "That date is more than a year out — check the year."}
        )
    return movement_date


def _clean_lines(lines: List[Dict]) -> List[Dict]:
    """Validate the item lines, refusing the mistakes that corrupt a total."""
    if not lines:
        raise ValidationError({"lines": "Add at least one item."})

    cleaned: List[Dict] = []
    seen = set()
    for index, raw in enumerate(lines):
        item_code = (raw.get("item_code") or "").strip().upper()
        if not item_code:
            raise ValidationError({"lines": f"Line {index + 1}: choose an item."})
        if item_code in seen:
            # Refused rather than summed: two lines for one item are always a
            # double-entry, and summing them hides the keeper's mistake inside a
            # total nobody can check.
            raise ValidationError(
                {"lines": f"{item_code} is on this movement twice — merge the lines."}
            )
        seen.add(item_code)

        pieces = raw.get("pieces")
        try:
            pieces = int(pieces)
        except (TypeError, ValueError):
            raise ValidationError(
                {"lines": f"Line {index + 1}: pieces must be a whole number."}
            )
        if pieces <= 0:
            raise ValidationError(
                {"lines": f"{item_code}: enter how many pieces are going — at least one."}
            )

        cleaned.append(
            {
                "item_code": item_code,
                "item_name": (raw.get("item_name") or "")[:200],
                "uom": (raw.get("uom") or "")[:20],
                "pieces": pieces,
                "pieces_per_box": _clean_factor(raw.get("pieces_per_box")),
                "litres_per_piece": _clean_litres(raw.get("litres_per_piece")),
                "remarks": (raw.get("remarks") or "")[:200],
            }
        )
    return cleaned


def _clean_factor(value):
    """A pack size from SAP, or None.

    0 is SAP's "not set". Stored as 0 it would make the box equivalent read as a
    division by zero, and stored as 1 it would claim every piece is its own box.
    """
    if value in ("", None):
        return None
    try:
        factor = int(value)
    except (TypeError, ValueError):
        return None
    return factor if factor >= 1 else None


def _clean_litres(value):
    """Litres in one piece from SAP, or None for an item not measured in litres.

    None rather than 0 all the way down: an item SAP holds no volume for is not
    a zero-litre item, and a 0 stored here would be added into litre totals that
    then read as complete.
    """
    if value in ("", None):
        return None
    try:
        litres = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return litres if litres > 0 else None


def _write_lines(movement: PFStockMovement, cleaned: List[Dict]) -> None:
    PFStockMovementLine.objects.bulk_create(
        [PFStockMovementLine(movement=movement, **line) for line in cleaned]
    )


def _log(movement: PFStockMovement, *, action: str, user, note: str = "") -> None:
    # Counted off the database rather than the caller's list so the event says
    # what the document holds, not what the request meant to put there. Queried
    # explicitly rather than through `movement.lines` — the movement may have
    # arrived with its lines prefetched, and a stale cache would log the totals
    # the document had *before* this very edit.
    lines = list(PFStockMovementLine.objects.filter(movement=movement))
    PFStockMovementEvent.objects.create(
        movement=movement,
        action=action,
        movement_date=movement.movement_date,
        destination_kind=movement.destination_kind,
        to_warehouse=movement.to_warehouse,
        line_count=len(lines),
        total_pieces=sum(line.pieces for line in lines),
        total_litres=sum(
            (line.litres for line in lines if line.litres is not None), Decimal("0")
        ),
        note=note or "",
        changed_by=user if _is_real(user) else None,
    )


def _is_real(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False))
