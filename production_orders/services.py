"""Production order entries, step by step: preview, save as draft, post.

Each SAP step has its own page. Each page reads from SAP only what its step
needs (``*_preview``), saves only that step's fields (``save_*``), and posts
only that step (:func:`post_step`):

* **Plan** — product, boxes and loose pieces, date. The order's lines are the
  BOM's, built here and sent with the order, but not asked for.
* **Release** — nothing.
* **Issue** — date, variety, and batches for the batch-tracked lines; the lines
  themselves and their quantities are the order's.
* **Receipt** — date and the batch (line, oil code, production date, expiry);
  the quantity is the order's, as SAP requires.
* **Close** — date.

The handlers (``sap_posting.py``) re-check against SAP at send time, because
SAP may have moved since a step was saved.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.batch_stock_reader import HanaBatchStockReader
from sap_client.hana.bom_reader import HanaBOMReader
from sap_client.hana.series_reader import HanaSeriesReader
from sap_postings import services as sap_postings

from . import identity
from .constants import (
    BATCH_NUMBER_MAX_LENGTH,
    FG_ITEM_GROUP,
    LINE_CODES,
    OBJECT_ISSUE_FOR_PRODUCTION,
    OBJECT_PRODUCTION_ORDER,
    OBJECT_RECEIPT_FROM_PRODUCTION,
    OIL_CODE_LENGTH,
    QUANTITY_PLACES,
    SAP_ORDER_STATUSES,
    SAP_ORDER_TYPES,
    SHELF_LIFE_YEARS,
    STOCK_CAPS,
    SUPPORTED_COMPANIES,
)
from .models import (
    STATUS_ORDER,
    EntryStatus,
    ProductionOrderEntry,
    ProductionOrderEntryBatch,
    ProductionOrderEntryLine,
    Step,
)
from .permissions import can_take
from .sap_reader import ProductionOrderReader

logger = logging.getLogger(__name__)

#: Step -> sap_postings kind.
KINDS = {
    Step.PLAN: "production_order.create",
    Step.RELEASE: "production_order.release",
    Step.ISSUE: "production_order.issue",
    Step.RECEIPT: "production_order.receipt",
    Step.CLOSE: "production_order.close",
}
STEP_FOR_KIND = {kind: step for step, kind in KINDS.items()}

#: Changes to an order already in SAP, which are not steps: a new product,
#: quantity or date while it is planned, and taking a released order back to
#: planned so it can be changed. Each is a sap_postings kind too.
CHANGE_KINDS = {
    "REPLAN": "production_order.replan",
    "UNRELEASE": "production_order.unrelease",
}
ALL_KINDS = (*KINDS.values(), *CHANGE_KINDS.values())


class EntryError(Exception):
    """Refused before SAP is asked. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


# ---------------------------------------------------------------------------
# small rules
# ---------------------------------------------------------------------------


def q6(value) -> Decimal:
    return Decimal(str(value)).quantize(QUANTITY_PLACES)


def add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February into a year without one
        return day.replace(year=day.year + years, day=28)


def default_expiry(mfg_date: date) -> date:
    """Production date plus the shelf life, less a day (as the floor enters it)."""
    return add_years(mfg_date, SHELF_LIFE_YEARS) - timedelta(days=1)


def batch_stem(line_code: str, oil_code: str, mfg_date: date) -> str:
    """``L3851010 102608``: line, oil code, then the date as MMYYDD."""
    return f"{line_code}{oil_code} {mfg_date:%m%y%d}"


def batch_number(stem: str, sequence: int) -> str:
    return f"{stem} {int(sequence):02d}"


def require_supported(company) -> None:
    if company.code not in SUPPORTED_COMPANIES:
        raise EntryError("Production order entries are set up for Jivo Oil only so far.")


def reader_for(company) -> ProductionOrderReader:
    return ProductionOrderReader(CompanyContext(company.code))


def _status_index(entry) -> int:
    return STATUS_ORDER.index(EntryStatus(entry.status))


def _date(value, what: str, *, not_before: date | None = None) -> date:
    day = value or timezone.localdate()
    if day > timezone.localdate():
        raise EntryError(f"The {what} cannot be in the future: SAP refuses it.")
    if not_before and day < not_before:
        raise EntryError(f"The {what} cannot be before the order's date ({not_before:%d-%m-%Y}).")
    return day


def stock_caps(company, reader, *, entry_litres: Decimal = Decimal("0")) -> list[dict]:
    """SAP's stock caps as they stand, and whether each would refuse now."""
    caps = STOCK_CAPS.get(company.code) or []
    if not caps:
        return []
    warehouses = {w for cap in caps for w in cap["warehouses"]}
    held = reader.litres_by_warehouse(warehouses)
    result = []
    for cap in caps:
        litres = sum(
            (
                row["litres"]
                for row in held
                if row["warehouse"] in cap["warehouses"]
                and ("series" not in cap or row["series"] == cap["series"])
                and ("group" not in cap or row["group"] == cap["group"])
            ),
            Decimal("0"),
        )
        counted = litres + (entry_litres if cap["with_entry"] else Decimal("0"))
        result.append(
            {
                "key": cap["key"],
                "label": cap["label"],
                "litres": q6(litres),
                "limit": cap["limit"],
                "on_order": cap["on_order"],
                "on_receipt": cap["on_receipt"],
                "over": counted > cap["limit"],
            }
        )
    return result


def cap_refusal(caps: list[dict], *, on: str) -> str:
    """SAP's refusal for the first cap over its limit at ``on`` (order/receipt), or ''."""
    for cap in caps:
        if cap[f"on_{on}"] and cap["over"]:
            return (
                f"SAP will refuse this: {cap['label']} hold {cap['litres']:,.0f} L, over its "
                f"{cap['limit']:,.0f} L limit."
            )
    return ""


def resolve_series(company, object_code: str, posting_date: date) -> dict:
    """The month's numbering series, or an EntryError saying which is missing."""
    try:
        return HanaSeriesReader(CompanyContext(company.code)).resolve(object_code, posting_date)
    except SAPConnectionError:
        raise
    except SAPDataError as exc:
        raise EntryError(str(exc))


def _series_name(company, object_code, day, warnings) -> str:
    try:
        return resolve_series(company, object_code, day)["series_name"]
    except EntryError as exc:
        warnings.append(str(exc))
        return ""


def _int(value, name: str) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        raise EntryError(f"{name} must be a whole number.")
    if number < 0:
        raise EntryError(f"{name} cannot be negative.")
    return number


def _allocate(batches: list[dict], quantity: Decimal) -> tuple[list[dict], Decimal]:
    """Oldest released batches first; returns the picks and what is still short."""
    picks, remaining = [], quantity
    for batch in batches:
        if remaining <= 0:
            break
        if batch["status"] != "0" or batch["quantity"] <= 0:
            continue
        take = min(remaining, batch["quantity"])
        picks.append({"batch_number": batch["batch_number"], "quantity": q6(take)})
        remaining -= take
    return picks, max(remaining, Decimal("0"))


def _active_posting(entry, *, sending_only=False):
    from sap_postings.models import ACTIVE_STATUSES, SapPosting, SapPostingStatus

    statuses = [SapPostingStatus.SENDING] if sending_only else ACTIVE_STATUSES
    return SapPosting.objects.filter(
        kind__in=ALL_KINDS, source_id=entry.pk, status__in=statuses
    ).first()


def _lock(entry) -> ProductionOrderEntry:
    """The entry, locked for a change; refused while one of its steps is being sent."""
    entry = ProductionOrderEntry.objects.select_for_update().get(pk=entry.pk)
    if _active_posting(entry, sending_only=True):
        raise EntryError("This entry is being sent to SAP right now. Wait for it to finish.", status=409)
    return entry


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def plan_preview(company, data: dict) -> dict:
    """The planned order an entry would create, from SAP as it stands now.

    Raises EntryError for what cannot be saved at all; returns ``warnings`` for
    what SAP would refuse at posting (a cap, a missing series).
    """
    require_supported(company)
    item_code = (data.get("item_code") or "").strip()
    if not item_code:
        raise EntryError("Choose what was made.")
    posting_date = _date(data.get("posting_date"), "posting date")

    reader = reader_for(company)
    product = reader.product(item_code)
    if product is None:
        raise EntryError(f"SAP has no item {item_code}.")
    if product["item_group"] != FG_ITEM_GROUP.get(company.code):
        raise EntryError(f"{item_code} is not a finished good.")
    if product["frozen"]:
        raise EntryError(f"{item_code} is inactive in SAP.")
    if product["bom_type"] != "P" or not product["bom_quantity"]:
        raise EntryError(f"{item_code} has no production BOM in SAP.")
    pieces_per_box = product["pieces_per_box"] or Decimal("1")

    boxes = _int(data.get("boxes"), "Boxes")
    loose = _int(data.get("loose_pieces"), "Loose pieces")
    if pieces_per_box > 1 and loose >= pieces_per_box:
        raise EntryError(
            f"{loose} loose pieces make at least one full box of {pieces_per_box:g}; enter them as boxes."
        )
    quantity = q6(boxes * pieces_per_box + loose)
    if quantity <= 0:
        raise EntryError("Enter how many boxes (or loose pieces) were made.")

    context = CompanyContext(company.code)
    tree = HanaBOMReader(context).get_tree(item_code)
    if not tree or not tree["lines"]:
        raise EntryError(f"{item_code}'s BOM has no lines in SAP.")
    bom_quantity = q6(tree["quantity"])
    warehouse = tree["warehouse"] or product["bom_warehouse"]
    if not warehouse:
        raise EntryError(f"{item_code}'s BOM names no warehouse to receive into.")
    bom_lines = [line for line in tree["lines"] if line["item_type"] in ("item", "resource")]
    flags = reader.batch_managed_flags(line["item_code"] for line in bom_lines if line["item_type"] == "item")
    lines = []
    for position, line in enumerate(bom_lines):
        # The same arithmetic as the send-time BOM check (sap_posting).
        base = q6(q6(line["quantity"]) / bom_quantity)
        lines.append(
            {
                "position": position,
                "item_code": line["item_code"],
                "item_name": line["item_name"],
                "item_type": line["item_type"],
                "issue_method": "B" if line["issue_method"] == "Backflush" else "M",
                "base_quantity": base,
                "planned_quantity": q6(base * quantity),
                "warehouse": line["warehouse"] or warehouse,
                "uom": line["uom"],
                "batch_managed": bool(line["item_type"] == "item" and flags.get(line["item_code"])),
            }
        )

    warnings = []
    litres = q6(quantity * product["litres_per_piece"]) if product["litres_per_piece"] else None
    caps = stock_caps(company, reader, entry_litres=litres or Decimal("0"))
    refusal = cap_refusal(caps, on="order")
    if refusal:
        warnings.append(refusal)
    series = _series_name(company, OBJECT_PRODUCTION_ORDER, posting_date, warnings)

    return {
        "item_code": product["item_code"],
        "item_name": product["item_name"],
        "uom": product["uom"],
        "pieces_per_box": pieces_per_box,
        "litres_per_piece": product["litres_per_piece"],
        "boxes": boxes,
        "loose_pieces": loose,
        "quantity": quantity,
        "litres": litres,
        "warehouse": warehouse,
        "bom_quantity": bom_quantity,
        "variety": product["variety"],
        "posting_date": posting_date,
        "remarks": (data.get("remarks") or "").strip()[:200],
        "line_count": len(lines),
        "lines": lines,
        "caps": [cap for cap in caps if cap["on_order"]],
        "series": series,
        "warnings": warnings,
    }


_PLAN_FIELDS = (
    "item_code", "item_name", "uom", "pieces_per_box", "litres_per_piece", "boxes", "loose_pieces",
    "quantity", "warehouse", "bom_quantity", "posting_date", "remarks",
)


def _write_lines(entry, lines: list[dict]) -> None:
    entry.lines.all().delete()
    ProductionOrderEntryLine.objects.bulk_create(
        ProductionOrderEntryLine(
            entry=entry,
            position=row["position"],
            item_code=row["item_code"],
            item_name=row["item_name"][:200],
            item_type=row["item_type"],
            issue_method=row["issue_method"],
            base_quantity=row["base_quantity"],
            planned_quantity=row["planned_quantity"],
            warehouse=row["warehouse"],
            uom=row["uom"][:20],
            batch_managed=row["batch_managed"],
        )
        for row in lines
    )


def _next_entry_no(company, day: date) -> str:
    prefix = f"PRD-{day:%Y%m%d}-"
    last = (
        ProductionOrderEntry.objects.filter(company=company, entry_no__startswith=prefix)
        .order_by("-entry_no")
        .values_list("entry_no", flat=True)
        .first()
    )
    number = int(last[len(prefix):]) + 1 if last else 1
    return f"{prefix}{number:04d}"


def create_entry(company, user, data: dict) -> ProductionOrderEntry:
    """Save the Plan step as a new draft."""
    built = plan_preview(company, data)
    for _ in range(5):
        try:
            with transaction.atomic():
                entry = ProductionOrderEntry(
                    company=company,
                    entry_no=_next_entry_no(company, timezone.localdate()),
                    variety=built["variety"],
                    created_by=user,
                    updated_by=user,
                    **{field: built[field] for field in _PLAN_FIELDS},
                )
                entry.save()
                _write_lines(entry, built["lines"])
                return entry
        except IntegrityError:
            continue  # another entry took the number in between: take the next
    raise EntryError("Could not number the entry; try again.", status=409)


def save_plan(entry, user, data: dict) -> ProductionOrderEntry:
    """Change the Plan step of a draft whose order is not in SAP yet."""
    with transaction.atomic():
        entry = _lock(entry)
        if entry.status != EntryStatus.DRAFT or entry.sap_order_entry:
            raise EntryError("The order is already in SAP; its product and quantity are fixed.", status=409)
        built = plan_preview(entry.company, data)
        product_changed = built["item_code"] != entry.item_code
        for field in _PLAN_FIELDS:
            setattr(entry, field, built[field])
        if product_changed or not entry.variety:
            entry.variety = built["variety"]
        entry.updated_by = user
        entry.save()
        _write_lines(entry, built["lines"])
        return entry


def delete_entry(entry) -> None:
    if entry.status != EntryStatus.DRAFT or entry.sap_order_entry:
        raise EntryError("Only a draft that has not reached SAP can be deleted.", status=409)
    if _active_posting(entry):
        raise EntryError("This entry is being sent to SAP. Wait for it to finish.", status=409)
    entry.delete()


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


def issue_preview(entry) -> dict:
    """The order's lines with their stock and batches, for the Issue page."""
    context = CompanyContext(entry.company.code)
    reader = reader_for(entry.company)
    batch_reader = HanaBatchStockReader(context)
    lines = list(entry.lines.prefetch_related("batches"))
    stock = reader.on_hand((line.item_code, line.warehouse) for line in lines if line.item_type == "item")
    issue_date = entry.issue_date or timezone.localdate()
    warnings, rows = [], []
    for line in lines:
        planned = q6(line.planned_quantity)
        on_hand = stock.get((line.item_code, line.warehouse)) if line.item_type == "item" else None
        short = q6(planned - on_hand) if on_hand is not None and on_hand < planned else None
        if short:
            warnings.append(
                f"{line.item_code} ({line.item_name}): {line.warehouse} holds {q6(on_hand):f}, "
                f"{planned:f} is needed."
            )
        row = {
            "id": line.pk,
            "position": line.position,
            "item_code": line.item_code,
            "item_name": line.item_name,
            "item_type": line.item_type,
            "base_quantity": q6(line.base_quantity),
            "planned_quantity": planned,
            "warehouse": line.warehouse,
            "uom": line.uom,
            "batch_managed": line.batch_managed,
            "on_hand": q6(on_hand) if on_hand is not None else None,
            "short": short,
            "batches": [],
            "batches_chosen": False,
            "available": [],
        }
        if line.batch_managed:
            available = batch_reader.available_batches(line.item_code, line.warehouse)
            row["available"] = [
                {
                    "batch_number": b["batch_number"],
                    "quantity": q6(b["quantity"]),
                    "released": b["status"] == "0",
                    "in_date": b["in_date"],
                }
                for b in available
            ]
            saved = [{"batch_number": b.batch_number, "quantity": q6(b.quantity)} for b in line.batches.all()]
            if saved:
                row["batches"], row["batches_chosen"] = saved, True
                held = {b["batch_number"]: b for b in available if b["status"] == "0"}
                for pick in saved:
                    batch = held.get(pick["batch_number"])
                    if batch is None or batch["quantity"] < pick["quantity"]:
                        holds = f"holds {q6(batch['quantity']):f}" if batch else "is not available"
                        warnings.append(
                            f"{line.item_code}: batch {pick['batch_number']} {holds} in {line.warehouse} "
                            f"now, but {pick['quantity']:f} was chosen. Choose its batches again."
                        )
            else:
                row["batches"], missing = _allocate(available, planned)
                if missing > 0 and short is None:
                    warnings.append(
                        f"{line.item_code}: its released batches in {line.warehouse} are {q6(missing):f} short."
                    )
        rows.append(row)
    return {
        "issue_date": issue_date,
        "variety": entry.variety,
        "series": _series_name(entry.company, OBJECT_ISSUE_FOR_PRODUCTION, issue_date, warnings),
        "lines": rows,
        "warnings": warnings,
    }


def save_issue(entry, user, data: dict) -> ProductionOrderEntry:
    """Save the Issue step: its date, the variety, and the chosen batches."""
    with transaction.atomic():
        entry = _lock(entry)
        if entry.step_done(Step.ISSUE):
            raise EntryError("The materials are already issued.", status=409)
        if "issue_date" in data:
            entry.issue_date = _date(data["issue_date"], "issue date", not_before=entry.posting_date)
        if "variety" in data:
            entry.variety = (data["variety"] or "").strip()
        lines = {line.pk: line for line in entry.lines.all()}
        reader = HanaBatchStockReader(CompanyContext(entry.company.code))
        for row in data.get("lines") or []:
            line = lines.get(int(row["line_id"]))
            if line is None:
                raise EntryError(f"Line {row['line_id']} is not on this entry.")
            if not line.batch_managed:
                raise EntryError(f"{line.item_code} is not batch-tracked.")
            picks = [b for b in row.get("batches") or [] if q6(b.get("quantity") or 0) > 0]
            line.batches.all().delete()
            if not picks:
                continue  # back to oldest first, when posted
            total = sum((q6(b["quantity"]) for b in picks), Decimal("0"))
            if total != q6(line.planned_quantity):
                raise EntryError(
                    f"{line.item_code}'s batches add up to {total:f}; the order needs "
                    f"{q6(line.planned_quantity):f}."
                )
            reader.check_allocation(line.item_code, line.warehouse, picks)
            ProductionOrderEntryBatch.objects.bulk_create(
                ProductionOrderEntryBatch(line=line, batch_number=b["batch_number"], quantity=q6(b["quantity"]))
                for b in picks
            )
        entry.updated_by = user
        entry.save()
        return entry


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def _taken_sequences(company, reader, item_code: str, stem: str, exclude_id=None) -> set[int]:
    """Sequence numbers already used under ``stem``: in SAP, or claimed by
    another entry that has not been received yet."""
    taken = set()
    for number in reader.batch_numbers_starting(item_code, stem):
        tail = number[len(stem):].strip()
        if tail.isdigit():
            taken.add(int(tail))
    claimed = ProductionOrderEntry.objects.filter(
        company=company, item_code=item_code, batch_number__startswith=stem, is_active=True
    ).exclude(status__in=[EntryStatus.RECEIVED, EntryStatus.CLOSED])
    if exclude_id:
        claimed = claimed.exclude(pk=exclude_id)
    for number in claimed.values_list("batch_number", flat=True):
        tail = number[len(stem):].strip()
        if tail.isdigit():
            taken.add(int(tail))
    return taken


def _batch_for(company, reader, item_code, line_code, oil_code, mfg_date, sequence, *, exclude_id=None):
    if line_code not in LINE_CODES:
        raise EntryError("Choose the line the oil was filled on.")
    oil_code = (oil_code or "").strip()
    if not (oil_code.isdigit() and len(oil_code) == OIL_CODE_LENGTH):
        raise EntryError(
            f"The oil code is the {OIL_CODE_LENGTH}-digit number given for the oil in the "
            "WhatsApp group."
        )
    stem = batch_stem(line_code, oil_code, mfg_date)
    taken = _taken_sequences(company, reader, item_code, stem, exclude_id=exclude_id)
    if sequence:
        sequence = _int(sequence, "The batch's last two digits")
        if not 1 <= sequence <= 99:
            raise EntryError("The batch's last two digits run from 01 to 99.")
        number = batch_number(stem, sequence)
        if sequence in taken:
            raise EntryError(
                f"Batch {number} is already used for {item_code}. SAP refuses the same batch "
                "twice, so pick the next number."
            )
    else:
        sequence = next(n for n in range(1, 100) if n not in taken)
        number = batch_number(stem, sequence)
    if len(number) > BATCH_NUMBER_MAX_LENGTH:
        raise EntryError(f"Batch {number} is longer than SAP's {BATCH_NUMBER_MAX_LENGTH} characters.")
    return oil_code, sequence, number


def receipt_preview(entry, data: dict) -> dict:
    """The batch the goods would go in under, and what SAP would refuse."""
    reader = reader_for(entry.company)
    warnings = []
    receipt_date = _date(data.get("receipt_date") or entry.receipt_date, "receipt date",
                         not_before=entry.posting_date)
    line_code = data.get("line_code", entry.line_code) or ""
    oil_code = data.get("oil_code", entry.oil_code) or ""
    mfg_date = data.get("mfg_date", entry.mfg_date)
    result = {
        "receipt_date": receipt_date,
        "line_code": line_code,
        "oil_code": oil_code,
        "mfg_date": mfg_date,
        "batch_sequence": None,
        "batch_number": "",
        "expiry_date": data.get("expiry_date") or entry.expiry_date,
        "quantity": q6(entry.quantity),
        "warehouse": entry.warehouse,
        "variety": entry.variety,
        "series": _series_name(entry.company, OBJECT_RECEIPT_FROM_PRODUCTION, receipt_date, warnings),
        "caps": [cap for cap in stock_caps(entry.company, reader) if cap["on_receipt"]],
        "warnings": warnings,
        "complete": False,
    }
    refusal = cap_refusal(result["caps"], on="receipt")
    if refusal:
        warnings.append(refusal)
    if not (line_code and oil_code and mfg_date):
        return result
    mfg_date = _date(mfg_date, "production date")
    oil_code, sequence, number = _batch_for(
        entry.company, reader, entry.item_code, line_code, oil_code, mfg_date,
        data.get("batch_sequence", entry.batch_sequence if entry.batch_number.startswith(
            batch_stem(line_code, oil_code, mfg_date)) else None),
        exclude_id=entry.pk,
    )
    expiry_date = data.get("expiry_date") or (
        entry.expiry_date if entry.expiry_date and entry.mfg_date == mfg_date else default_expiry(mfg_date)
    )
    if expiry_date <= mfg_date:
        raise EntryError("The expiry date must come after the production date.")
    result.update(
        oil_code=oil_code, mfg_date=mfg_date, batch_sequence=sequence, batch_number=number,
        expiry_date=expiry_date, complete=True,
    )
    return result


def save_receipt(entry, user, data: dict) -> ProductionOrderEntry:
    """Save the Receipt step: its date and the batch."""
    with transaction.atomic():
        entry = _lock(entry)
        if entry.step_done(Step.RECEIPT):
            raise EntryError("The goods are already received.", status=409)
        built = receipt_preview(entry, data)
        entry.receipt_date = built["receipt_date"]
        entry.line_code = built["line_code"]
        entry.oil_code = built["oil_code"]
        entry.mfg_date = built["mfg_date"]
        entry.batch_sequence = built["batch_sequence"]
        entry.batch_number = built["batch_number"]
        entry.expiry_date = built["expiry_date"] if built["complete"] else data.get("expiry_date")
        entry.updated_by = user
        entry.save()
        return entry


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


def save_close(entry, user, data: dict) -> ProductionOrderEntry:
    with transaction.atomic():
        entry = _lock(entry)
        if entry.step_done(Step.CLOSE):
            raise EntryError("The order is already closed.", status=409)
        if "close_date" in data:
            entry.close_date = _date(data["close_date"], "closing date", not_before=entry.posting_date)
        entry.updated_by = user
        entry.save()
        return entry


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def posting_title(entry, step) -> str:
    return f"{Step(step).label} {entry.entry_no}: {entry.item_code} × {entry.quantity:g}"


def posting_link(entry) -> str:
    return f"/production-orders/entries/{entry.pk}"


def _missing_for(entry, step) -> str:
    """What the step still needs before it can be posted, in words, or ''."""
    if step == Step.ISSUE and not entry.variety:
        return "Choose the variety on the Issue page and save it first."
    if step == Step.RECEIPT and not (entry.batch_number and entry.mfg_date and entry.expiry_date):
        return "Fill in the batch on the Receipt page (line, oil code, production date) and save it first."
    return ""


def post_step(entry, user, step) -> dict:
    """Send one step to SAP as ``user``. Returns ``{step, outcome, message}``."""
    step = Step(step)
    if entry.step_done(step):
        raise EntryError(f"The {step.label.lower()} step is already done.", status=409)
    if entry.next_step != step:
        raise EntryError(
            f"{Step(entry.next_step).label} comes first: SAP takes the steps in order.", status=409
        )
    if not can_take(user, step):
        raise EntryError(f"You do not have the right to post the {step.label.lower()} step.", status=403)
    status = identity.login_status(user, entry.company)
    if not status["ready"]:
        raise EntryError(status["message"], code="NO_SAP_LOGIN")
    missing = _missing_for(entry, step)
    if missing:
        raise EntryError(missing)
    try:
        posting, outcome = sap_postings.post_now(
            kind=KINDS[step],
            company=entry.company,
            source_id=entry.pk,
            title=posting_title(entry, step),
            link=posting_link(entry),
            user=user,
        )
    except sap_postings.PostingInProgress as exc:
        return {"step": step, "outcome": "IN_PROGRESS", "message": str(exc)}
    return {"step": step, "outcome": outcome.kind, "message": outcome.message, "posting_id": posting.pk}


def _ready(entry, user, step) -> None:
    """The person may take ``step`` and SAP will take their login."""
    if not can_take(user, step):
        raise EntryError(f"You do not have the right to {step.label.lower()} this order.", status=403)
    status = identity.login_status(user, entry.company)
    if not status["ready"]:
        raise EntryError(status["message"], code="NO_SAP_LOGIN")


def _post_change(entry, user, change: str, title: str, params: dict | None = None) -> dict:
    try:
        posting, outcome = sap_postings.post_now(
            kind=CHANGE_KINDS[change],
            company=entry.company,
            source_id=entry.pk,
            title=f"{title} {entry.entry_no}: {entry.item_code}",
            link=posting_link(entry),
            params=params or {},
            user=user,
        )
    except sap_postings.PostingInProgress as exc:
        return {"step": change, "outcome": "IN_PROGRESS", "message": str(exc)}
    return {"step": change, "outcome": outcome.kind, "message": outcome.message, "posting_id": posting.pk}


def request_replan(entry, user, data: dict) -> dict:
    """Change the product, quantity, date or remarks of an order still planned in
    SAP. The new values travel with the posting and reach the entry only once
    SAP has taken them, so a change SAP refuses leaves the entry as SAP has it."""
    if entry.status != EntryStatus.PLANNED or not entry.sap_order_entry:
        raise EntryError(
            "Only an order that is planned in SAP can be changed. A released one goes back to "
            "planned first (on the Release page), if nothing has been issued to it.",
            status=409,
        )
    _ready(entry, user, Step.PLAN)
    built = plan_preview(entry.company, data)
    unchanged = (
        built["item_code"] == entry.item_code
        and q6(built["quantity"]) == q6(entry.quantity)
        and built["posting_date"] == entry.posting_date
        and built["remarks"] == entry.remarks
    )
    if unchanged:
        raise EntryError("Nothing has changed.")
    refusal = cap_refusal(built["caps"], on="order")
    if refusal:
        raise EntryError(refusal)
    params = {
        "item_code": built["item_code"],
        "boxes": built["boxes"],
        "loose_pieces": built["loose_pieces"],
        "posting_date": built["posting_date"].isoformat(),
        "remarks": built["remarks"],
    }
    return _post_change(entry, user, "REPLAN", "Change planned order", params)


def request_unrelease(entry, user) -> dict:
    """Take a released order back to planned, while nothing is issued to it."""
    if entry.status != EntryStatus.RELEASED:
        raise EntryError("Only a released order with nothing issued can go back to planned.", status=409)
    _ready(entry, user, Step.RELEASE)
    return _post_change(entry, user, "UNRELEASE", "Back to planned")


def postings_for_changes(entry) -> dict:
    """The latest posting of each change (REPLAN, UNRELEASE), for the pages."""
    from sap_postings.models import SapPosting

    latest = {}
    by_kind = {kind: change for change, kind in CHANGE_KINDS.items()}
    rows = SapPosting.objects.filter(kind__in=CHANGE_KINDS.values(), source_id=entry.pk).order_by("-id")
    for posting in rows:
        latest.setdefault(by_kind[posting.kind], posting)
    return latest


def postings_by_step(entry) -> dict:
    """The latest posting of each step (what the pages show as its state)."""
    from sap_postings.models import SapPosting

    latest = {}
    rows = SapPosting.objects.filter(kind__in=KINDS.values(), source_id=entry.pk).order_by("-id")
    for posting in rows:
        latest.setdefault(STEP_FOR_KIND[posting.kind], posting)
    return latest


def sap_orders(company, *, status="", order_type="", date_from=None, date_to=None,
               search="", limit=50, offset=0) -> dict:
    """SAP's own production orders, made here or in SAP itself, newest first.

    Read-only. Each row carries the entry here that made it (``entry``), or
    None for an order made in SAP.
    """
    require_supported(company)
    if status and status not in SAP_ORDER_STATUSES:
        raise EntryError(f"Unknown SAP order status {status!r}.")
    if order_type and order_type not in SAP_ORDER_TYPES:
        raise EntryError(f"Unknown SAP order type {order_type!r}.")
    if date_from and date_to and date_from > date_to:
        raise EntryError("The from date is after the to date.")
    found = reader_for(company).sap_orders(
        status=status, order_type=order_type, date_from=date_from, date_to=date_to,
        search=search, limit=limit, offset=offset,
    )
    made_here = {
        doc_entry: {"id": pk, "entry_no": entry_no}
        for doc_entry, pk, entry_no in entries_for(company)
        .filter(sap_order_entry__in=[row["doc_entry"] for row in found["results"]])
        .values_list("sap_order_entry", "id", "entry_no")
    }
    for row in found["results"]:
        row["type_label"] = SAP_ORDER_TYPES.get(row["type"], row["type"])
        row["status_label"] = SAP_ORDER_STATUSES.get(row["status"], row["status"])
        row["entry"] = made_here.get(row["doc_entry"])
    return found


def entries_for(company):
    return ProductionOrderEntry.objects.filter(company=company, is_active=True).select_related(
        "company", "created_by", "planned_by", "released_by", "issued_by", "received_by", "closed_by"
    )


def search_filter(text: str) -> Q:
    text = (text or "").strip()
    if not text:
        return Q()
    filters = (
        Q(entry_no__icontains=text) | Q(item_code__icontains=text)
        | Q(item_name__icontains=text) | Q(batch_number__icontains=text)
    )
    if text.isdigit():
        filters |= Q(sap_order_num=int(text)) | Q(sap_issue_num=int(text)) | Q(sap_receipt_num=int(text))
    return filters
