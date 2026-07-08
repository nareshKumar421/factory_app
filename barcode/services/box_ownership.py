"""Shared cross-company ownership moves for boxes and pallets.

Extracted from ``IntercompanyTransferService`` so both the intercompany-transfer
flow and the warehouse Branch-Stock-Transfer (BST) flow reassign ownership the
same way: update the ``company`` FK on the entity and keep its ``BarcodeMaster``
row in sync, optionally remapping the item code for company pairs that use
different catalogues (e.g. JIVO_OIL → JIVO_MART).

These helpers ONLY move ownership (and optional item-code). Callers are
responsible for any warehouse change, movement history, and audit logging that
their flow requires.
"""

from django.utils import timezone

from ..models import BarcodeMaster, Box, Pallet
from .oitm_item_service import OitmItemReadError, OitmItemService

# Company pairs whose SAP item catalogues differ need a code remap when stock
# changes hands. Today only JIVO OIL → JIVO MART does; the destination item code
# is read from the JIVO MART OITM `U_Oil_ItemCode` column.
JIVO_OIL_COMPANY_CODE = "JIVO_OIL"
JIVO_MART_COMPANY_CODE = "JIVO_MART"


class ItemCodeMappingError(ValueError):
    """A required source→destination item-code mapping is missing or ambiguous."""


def requires_item_code_remap(source, destination) -> bool:
    """True for company pairs whose item catalogues differ (JIVO OIL → JIVO MART)."""
    return (
        str(source.code or "").upper() == JIVO_OIL_COMPANY_CODE
        and str(destination.code or "").upper() == JIVO_MART_COMPANY_CODE
    )


def resolve_destination_item_code_map(source, destination, item_codes) -> dict[str, str]:
    """Map each source item_code → the destination company's item_code.

    Returns ``{}`` when the pair shares a catalogue (no remap needed). For the
    JIVO OIL → JIVO MART pair, resolves each code via the JIVO MART OITM
    ``U_Oil_ItemCode`` column and raises :class:`ItemCodeMappingError` on a
    missing or duplicate mapping so callers can fail early with a clear message.
    """
    if not requires_item_code_remap(source, destination):
        return {}

    codes = sorted({str(code or "").strip() for code in item_codes if str(code or "").strip()})
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


def reassign_boxes_to_company(boxes, destination, *, item_code_map=None) -> None:
    """Move ``boxes`` to the ``destination`` company.

    ``item_code_map`` maps a box's current item_code → the destination item_code,
    for company pairs whose catalogues differ. Pass ``None``/empty to keep codes.
    """
    box_list = list(boxes)
    if not box_list:
        return

    if not item_code_map:
        ids = [box.id for box in box_list]
        Box.objects.filter(id__in=ids).update(company=destination)
        BarcodeMaster.objects.filter(box_id__in=ids).update(company=destination)
        return

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


def reassign_pallets_to_company(pallets, destination, *, item_code_map=None) -> None:
    """Move ``pallets`` to the ``destination`` company (same rules as boxes)."""
    pallet_list = list(pallets)
    if not pallet_list:
        return

    if not item_code_map:
        ids = [pallet.id for pallet in pallet_list]
        Pallet.objects.filter(id__in=ids).update(company=destination)
        BarcodeMaster.objects.filter(pallet_id__in=ids).update(company=destination)
        return

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
