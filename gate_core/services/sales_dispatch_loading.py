"""Attribute a scanned box (or a whole pallet's boxes) to the bills on a Docking,
stage it INSIDE_VEHICLE, and free its Warehouse Ops bin.

This is the shared core behind both the gate box-scan endpoint and the warehouse
"Dispatch Loading" pallet-scan endpoint. Loading a pallet here is purely
additive and never a required step: the docking flow still works exactly as
before whether or not a pallet was scanned through this path — this simply
reuses the same per-box attribution + staging that a box scan already runs, so
scanning a whole pallet frees its map location the same way box scans do.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from barcode.models import Box, BoxStatus, PalletStatus
from barcode.services.vehicle_load import load_boxes_into_vehicle

from gate_core.models import (
    SalesDispatchBoxScan,
    SalesDispatchGateOutDocument,
)
from gate_core.services.sales_dispatch_box_match import (
    document_invoices_item,
    expected_containers_for_bill_item,
    invoice_unit_label,
    invoice_units_per_box,
    loose_box_scan_error,
    remaining_expected_boxes,
    remaining_invoiced_qty,
    resolve_scan_document,
)

# Outcome statuses for a single box scan.
SCANNED = "scanned"      # newly staged into the vehicle
DUPLICATE = "duplicate"  # already actively scanned on this docking
REJECTED = "rejected"    # a business rule refused it (see ``detail``)


@dataclass
class BoxScanOutcome:
    status: str
    detail: str = ""
    scan: Optional[SalesDispatchBoxScan] = None
    document: Optional[SalesDispatchGateOutDocument] = None
    created: bool = False


def docking_reference(entry) -> str:
    """Human trail label for movements caused by this docking."""
    vehicle_no = getattr(getattr(entry, "vehicle", None), "vehicle_number", "")
    return f"Docking {entry.entry_no}" + (f" ({vehicle_no})" if vehicle_no else "")


def box_unavailable_detail(box) -> str:
    """Why a box can't be scanned onto a docking, naming the truck holding it."""
    if box.status == BoxStatus.INSIDE_VEHICLE:
        other = (
            SalesDispatchBoxScan.objects.filter(box_barcode=box.box_barcode, is_active=True)
            .select_related("sales_dispatch")
            .order_by("-scanned_at")
            .first()
        )
        where = f" (Docking {other.sales_dispatch.entry_no})" if other else ""
        return f"Box {box.box_barcode} is already loaded in a vehicle{where}."
    return f"Box {box.box_barcode} is {box.status} and cannot be dispatched."


def scan_box_onto_docking(
    entry,
    box,
    *,
    user,
    document_id=None,
    barcode_raw="",
    scan_log_id=None,
    reference=None,
):
    """Scan one already-resolved ``box`` onto ``entry``'s bills and stage it.

    Runs the same attribution + over-scan guards as the gate box-scan endpoint,
    then creates (or re-activates) the ``SalesDispatchBoxScan`` and flips the box
    to INSIDE_VEHICLE via ``load_boxes_into_vehicle`` (which frees its WMS bin
    once the whole pallet is loaded). Returns a :class:`BoxScanOutcome`; a
    business rejection is returned, never raised, so callers scanning many boxes
    (a pallet) can skip a bad box and continue.

    ``document_id`` pins the bill the operator is scanning into (rejecting a box
    whose item that bill doesn't invoice); when ``None`` the bill is auto-resolved.
    """
    reference = docking_reference(entry) if reference is None else reference

    # Duplicate: already actively scanned on this docking. Checked before the
    # status gate because the first scan flips the box to INSIDE_VEHICLE, which
    # would otherwise mis-report a re-scan as "already in a vehicle".
    existing = (
        SalesDispatchBoxScan.objects
        .filter(sales_dispatch=entry, box_barcode=box.box_barcode)
        .first()
    )
    if existing and existing.is_active:
        return BoxScanOutcome(status=DUPLICATE, scan=existing, document=existing.document)

    if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
        return BoxScanOutcome(status=REJECTED, detail=box_unavailable_detail(box))

    # Resolve the one bill this box is scanned against. When a specific bill is
    # given we honour it (and reject a box whose item that bill doesn't invoice);
    # otherwise auto-resolve so a box whose item is on several bills isn't counted
    # against all of them.
    if document_id is not None:
        try:
            document = SalesDispatchGateOutDocument.objects.get(
                id=document_id, sales_dispatch=entry, is_active=True,
            )
        except SalesDispatchGateOutDocument.DoesNotExist:
            return BoxScanOutcome(status=REJECTED, detail="Bill not found on this Docking.")
        if not document_invoices_item(entry, document.id, box.item_code):
            return BoxScanOutcome(
                status=REJECTED,
                detail=(
                    f"Box {box.box_barcode} (item {box.item_code}) is not on "
                    f"bill {document.sap_doc_num}."
                ),
            )
    else:
        document = resolve_scan_document(entry, item_code=box.item_code, box=box)

    # Over-scan guards — identical to the gate box-scan endpoint.
    if document is not None:
        remaining_qty = remaining_invoiced_qty(entry, document.id, box.item_code)
        # What this box is worth against the invoice. For CSD stock the bill counts
        # BOXES, so a carton is worth 1 however many bottles its label declares;
        # comparing the declared 20 against a 4-carton line is what rejected the scan.
        box_qty = invoice_units_per_box(entry, document.id, box.item_code, box.qty)
        if remaining_qty <= 0:
            return BoxScanOutcome(
                status=REJECTED,
                detail=(
                    f"Bill {document.sap_doc_num} already has the full invoiced "
                    f"quantity of {box.item_code} scanned."
                ),
            )
        if box_qty > remaining_qty:
            # Word it in the unit the bill is written in, so a CSD rejection talks about
            # the cartons the bill counts rather than pieces it never did.
            return BoxScanOutcome(
                status=REJECTED,
                detail=(
                    f"Box {box.box_barcode} "
                    f"({box_qty} {invoice_unit_label(entry, document.id, box.item_code, box_qty)}) "
                    f"would exceed the invoiced quantity of {box.item_code} on bill "
                    f"{document.sap_doc_num} — only {remaining_qty} "
                    f"{invoice_unit_label(entry, document.id, box.item_code, remaining_qty)} remain."
                ),
            )
        loose_error = loose_box_scan_error(entry, document, box)
        if loose_error:
            return BoxScanOutcome(status=REJECTED, detail=loose_error)
        # Cap the box COUNT at the boxes the bill can arrive in -- its printed box count
        # plus one for the loose remainder, since those pieces come in a short box of
        # their own. None = the item ships loose, so there is no box count to cap
        # against; the quantity guards above are what bound the scan for those lines.
        box_headroom = remaining_expected_boxes(entry, document.id, box.item_code)
        if box_headroom is not None and box_headroom <= 0:
            expected_containers = expected_containers_for_bill_item(
                entry, document.id, box.item_code
            )
            return BoxScanOutcome(
                status=REJECTED,
                detail=(
                    f"Bill {document.sap_doc_num} already has the expected number "
                    f"of boxes for {box.item_code} scanned "
                    f"({expected_containers} for this bill)."
                    + (
                        f" {remaining_qty} "
                        f"{invoice_unit_label(entry, document.id, box.item_code, remaining_qty)} "
                        f"are still short — check the boxes already scanned for a partial one."
                        if remaining_qty > 0
                        else ""
                    )
                ),
            )

    with transaction.atomic():
        scan, created = SalesDispatchBoxScan.objects.get_or_create(
            sales_dispatch=entry,
            box_barcode=box.box_barcode,
            defaults={
                "company": entry.company,
                "document": document,
                "box": box,
                "scan_log_id": scan_log_id,
                "barcode_raw": barcode_raw,
                "item_code": box.item_code,
                "item_name": box.item_name,
                "batch_number": box.batch_number,
                "quantity": box.qty,
                "uom": box.uom,
                "net_weight": box.n_weight,
                "gross_weight": box.g_weight,
                "box_status": box.status,
                "warehouse_code": box.current_warehouse,
                "pallet_code": box.pallet.pallet_id if box.pallet else "",
                "scanned_by": user,
                "created_by": user,
                "updated_by": user,
            },
        )
        if not created and not scan.is_active:
            # A previously-removed scan (is_active=False) is re-activated as a
            # fresh scan rather than left dangling.
            scan.is_active = True
            scan.document = document
            scan.box = box
            scan.scan_log_id = scan_log_id
            scan.barcode_raw = barcode_raw
            scan.item_code = box.item_code
            scan.item_name = box.item_name
            scan.batch_number = box.batch_number
            scan.quantity = box.qty
            scan.uom = box.uom
            scan.net_weight = box.n_weight
            scan.gross_weight = box.g_weight
            scan.box_status = box.status
            scan.warehouse_code = box.current_warehouse
            scan.pallet_code = box.pallet.pallet_id if box.pallet else ""
            scan.scanned_by = user
            scan.scanned_at = timezone.now()
            scan.updated_by = user
            scan.save()
            created = True
        if created:
            # The box is physically in the truck from the moment it is scanned:
            # mark it INSIDE_VEHICLE and free its warehouse location.
            load_boxes_into_vehicle(entry.company, [box], user, reference=reference)

    return BoxScanOutcome(status=SCANNED, scan=scan, document=document, created=created)


@dataclass
class PalletScanResult:
    pallet_id: str
    item_code: str
    item_name: str
    total_boxes: int          # active/partial boxes considered
    scanned: int              # newly staged
    duplicates: int           # already on this docking
    rejected: int             # refused by a rule
    rejections: list          # [{"box_barcode", "detail"}]
    documents_touched: list   # bill sap_doc_num(s) that received boxes
    bin_freed: bool           # whole pallet staged -> WMS location vacated
    pallet_status: str        # barcode pallet status after the scan


def scan_pallet_onto_docking(entry, pallet, *, user, document_id=None,
                             barcode_raw="", scan_log_id=None):
    """Scan every loadable box of ``pallet`` onto ``entry``'s bills.

    Each box runs through :func:`scan_box_onto_docking`, so partial pallets and
    boxes that don't fit a bill are skipped and reported rather than blocking the
    rest. When the last active box is staged the pallet flips to INSIDE_VEHICLE
    and its Warehouse Ops bin is freed automatically. ``scan_log_id`` (the
    pallet's own scan-log row, resolved by the caller) is stamped on each box
    scan so the trail links back to the single pallet scan.
    """
    reference = docking_reference(entry)
    boxes = list(
        Box.objects.filter(
            pallet=pallet, status__in=(BoxStatus.ACTIVE, BoxStatus.PARTIAL)
        ).select_related("pallet")
    )

    scanned = duplicates = rejected = 0
    rejections = []
    documents_touched = {}
    for box in boxes:
        outcome = scan_box_onto_docking(
            entry, box, user=user, document_id=document_id,
            barcode_raw=barcode_raw, scan_log_id=scan_log_id, reference=reference,
        )
        if outcome.status == SCANNED:
            scanned += 1
            if outcome.document is not None:
                documents_touched[outcome.document.id] = outcome.document.sap_doc_num
        elif outcome.status == DUPLICATE:
            duplicates += 1
            if outcome.document is not None:
                documents_touched[outcome.document.id] = outcome.document.sap_doc_num
        else:
            rejected += 1
            rejections.append({"box_barcode": box.box_barcode, "detail": outcome.detail})

    pallet.refresh_from_db()
    bin_freed = pallet.status == PalletStatus.INSIDE_VEHICLE or (pallet.box_count or 0) <= 0

    return PalletScanResult(
        pallet_id=pallet.pallet_id,
        item_code=pallet.item_code or "",
        item_name=pallet.item_name or "",
        total_boxes=len(boxes),
        scanned=scanned,
        duplicates=duplicates,
        rejected=rejected,
        rejections=rejections,
        documents_touched=list(documents_touched.values()),
        bin_freed=bin_freed,
        pallet_status=pallet.status,
    )
