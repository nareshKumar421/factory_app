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
