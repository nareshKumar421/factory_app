"""Shared cross-company ownership moves for boxes and pallets.

Extracted from ``IntercompanyTransferService`` so both the intercompany-transfer
flow and the warehouse Branch-Stock-Transfer (BST) flow reassign ownership the
same way: update the ``company`` FK on the entity and keep its ``BarcodeMaster``
row in sync, optionally remapping the item code for company pairs that use
different catalogues (e.g. JIVO_OIL → JIVO_MART).

These helpers ONLY move ownership (and optional item-code) — plus an
OWNERSHIP_TRANSFER movement row per entity so the handoff shows up in the
box/pallet trail (it previously only reached ``BarcodeAuditLog``, invisible on
the detail pages). Callers remain responsible for any warehouse change and
flow-level audit logging.
"""

from django.utils import timezone

from ..models import (
    BarcodeMaster,
    Box,
    BoxMovement,
    BoxMovementType,
    Pallet,
    PalletMovement,
    PalletMovementType,
)
from .oitm_item_service import OitmItemReadError, OitmItemService

# Company pairs whose SAP item catalogues differ need a code remap when stock
# changes hands — the JIVO OIL / JIVO MART pair, in BOTH directions. The link is
# the JIVO MART OITM `U_Oil_ItemCode` column (Oil's code stored against each Mart
# item); Oil's OITM has no equivalent column, so every remap reads that one column:
#   OIL → MART: find the Mart item whose U_Oil_ItemCode == the Oil code.
#   MART → OIL: read the Mart item's own U_Oil_ItemCode to get the Oil code.
JIVO_OIL_COMPANY_CODE = "JIVO_OIL"
JIVO_MART_COMPANY_CODE = "JIVO_MART"


class ItemCodeMappingError(ValueError):
    """A required source→destination item-code mapping is missing or ambiguous."""


def _is_oil_to_mart(source, destination) -> bool:
    return (
        str(source.code or "").upper() == JIVO_OIL_COMPANY_CODE
        and str(destination.code or "").upper() == JIVO_MART_COMPANY_CODE
    )


def _is_mart_to_oil(source, destination) -> bool:
    return (
        str(source.code or "").upper() == JIVO_MART_COMPANY_CODE
        and str(destination.code or "").upper() == JIVO_OIL_COMPANY_CODE
    )


def requires_item_code_remap(source, destination) -> bool:
    """True for company pairs whose item catalogues differ (JIVO OIL ⇄ JIVO MART)."""
    return _is_oil_to_mart(source, destination) or _is_mart_to_oil(source, destination)


def resolve_destination_item_code_map(source, destination, item_codes) -> dict[str, str]:
    """Map each source item_code → the destination company's item_code.

    Returns ``{}`` when the pair shares a catalogue (no remap needed). For the
    JIVO OIL ⇄ JIVO MART pair, resolves each code via the JIVO MART OITM
    ``U_Oil_ItemCode`` column and raises :class:`ItemCodeMappingError` on a
    missing (or, forward only, duplicate) mapping so callers can fail early with a
    clear message.
    """
    codes = sorted({str(code or "").strip() for code in item_codes if str(code or "").strip()})
    if not codes:
        return {}

    if _is_oil_to_mart(source, destination):
        # Forward: search the JIVO MART OITM for the item carrying this Oil code.
        mapper = OitmItemService(company_code=destination.code)
        mapping: dict[str, str] = {}
        for oil_item_code in codes:
            try:
                matches = mapper.find_item_codes_by_oil_item_code(oil_item_code)
            except OitmItemReadError as exc:
                raise ItemCodeMappingError(str(exc)) from exc
            if not matches:
                raise ItemCodeMappingError(
                    "Item mapping not found in Jivo Mart for Oil ItemCode: "
                    f"{oil_item_code}. Please maintain U_Oil_ItemCode in Jivo Mart OITM table."
                )
            if len(matches) > 1:
                raise ItemCodeMappingError(
                    "Duplicate item mapping found in Jivo Mart for Oil ItemCode: "
                    f"{oil_item_code}. Please correct duplicate U_Oil_ItemCode values in Jivo Mart OITM table."
                )
            mapping[oil_item_code] = matches[0]
        return mapping

    if _is_mart_to_oil(source, destination):
        # Reverse: read each JIVO MART item's own U_Oil_ItemCode to recover the Oil
        # code. Unambiguous by construction (a single column on one row), so there is
        # no duplicate case to guard — only the missing/blank one.
        mapper = OitmItemService(company_code=source.code)
        mapping = {}
        for mart_item_code in codes:
            try:
                oil_item_code = mapper.find_oil_item_code_by_mart_item_code(mart_item_code)
            except OitmItemReadError as exc:
                raise ItemCodeMappingError(str(exc)) from exc
            if not oil_item_code:
                raise ItemCodeMappingError(
                    "Item mapping not found in Jivo Mart for Jivo Mart ItemCode: "
                    f"{mart_item_code}. Please maintain U_Oil_ItemCode in Jivo Mart OITM table."
                )
            mapping[mart_item_code] = oil_item_code
        return mapping

    return {}


def _ownership_note(source, destination, reference):
    note = f"Ownership: {getattr(source, 'code', source) or ''} → {destination.code}"
    return f"{note} ({reference})" if reference else note


def reassign_boxes_to_company(boxes, destination, *, item_code_map=None, user=None, reference="") -> None:
    """Move ``boxes`` to the ``destination`` company.

    ``item_code_map`` maps a box's current item_code → the destination item_code,
    for company pairs whose catalogues differ. Pass ``None``/empty to keep codes.
    ``user``/``reference`` label the OWNERSHIP_TRANSFER trail row (e.g. the
    transfer/BST number).
    """
    box_list = list(boxes)
    if not box_list:
        return

    sources = {box.id: box.company for box in box_list}

    if not item_code_map:
        ids = [box.id for box in box_list]
        Box.objects.filter(id__in=ids).update(company=destination)
        BarcodeMaster.objects.filter(box_id__in=ids).update(company=destination)
    else:
        now = timezone.now()
        for box in box_list:
            current_code = str(box.item_code or "").strip()
            box.company = destination
            if current_code in item_code_map:
                box.item_code = item_code_map[current_code]
            box.updated_at = now
        Box.objects.bulk_update(box_list, ["company", "item_code", "updated_at"])

        for destination_item_code in set(item_code_map.values()):
            box_ids = [box.id for box in box_list if box.item_code == destination_item_code]
            BarcodeMaster.objects.filter(box_id__in=box_ids).update(
                company=destination,
                material_code=destination_item_code,
            )

    BoxMovement.objects.bulk_create([
        BoxMovement(
            company=destination,
            box=box,
            movement_type=BoxMovementType.OWNERSHIP_TRANSFER,
            from_warehouse=box.current_warehouse,
            to_warehouse=box.current_warehouse,
            performed_by=user,
            notes=_ownership_note(sources.get(box.id), destination, reference),
        )
        for box in box_list
    ])


def reassign_pallets_to_company(pallets, destination, *, item_code_map=None, user=None, reference="") -> None:
    """Move ``pallets`` to the ``destination`` company (same rules as boxes)."""
    pallet_list = list(pallets)
    if not pallet_list:
        return

    sources = {pallet.id: pallet.company for pallet in pallet_list}

    if not item_code_map:
        ids = [pallet.id for pallet in pallet_list]
        Pallet.objects.filter(id__in=ids).update(company=destination)
        BarcodeMaster.objects.filter(pallet_id__in=ids).update(company=destination)
    else:
        now = timezone.now()
        for pallet in pallet_list:
            current_code = str(pallet.item_code or "").strip()
            pallet.company = destination
            if current_code in item_code_map:
                pallet.item_code = item_code_map[current_code]
            pallet.updated_at = now
        Pallet.objects.bulk_update(pallet_list, ["company", "item_code", "updated_at"])

        for destination_item_code in set(item_code_map.values()):
            pallet_ids = [p.id for p in pallet_list if p.item_code == destination_item_code]
            BarcodeMaster.objects.filter(pallet_id__in=pallet_ids).update(
                company=destination,
                material_code=destination_item_code,
            )

    PalletMovement.objects.bulk_create([
        PalletMovement(
            company=destination,
            pallet=pallet,
            movement_type=PalletMovementType.OWNERSHIP_TRANSFER,
            from_warehouse=pallet.current_warehouse,
            to_warehouse=pallet.current_warehouse,
            performed_by=user,
            notes=_ownership_note(sources.get(pallet.id), destination, reference),
        )
        for pallet in pallet_list
    ])
