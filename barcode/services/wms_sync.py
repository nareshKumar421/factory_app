"""Keep the Warehouse Ops (``wms``) map in sync with barcode pallet lifecycle
events that happen *outside* the WMS module.

The WMS module renders its own ``Pallet``/``Inventory`` records. When a barcode
pallet is dispatched (or otherwise emptied) through the dispatch/docking flow,
nothing in that flow touches WMS — so the pallet would linger on the map as
phantom stock. ``reconcile_pallet_to_wms`` closes that gap: it removes the WMS
pallet + its inventory when the pallet has left (dispatched / no boxes left), or
decrements the box count / quantity when only some boxes were dispatched.

Matched by license plate, so it covers both WMS pallet representations — the
receive/putaway records (keyed by their own uuid) and the pallet-move mirror
inventory (``bc-pallet-<id>``).

A pallet that is INSIDE_VEHICLE (fully loaded into a truck that has not left
yet) is *staged*, not gone: its location is freed (OUTBOUND movement + inventory
removed + ``currentLocationId`` cleared) but the WMS pallet record is kept, so
if the load is reverted the pallet reappears as un-placed (operators re-place it
via putaway) instead of vanishing from the map. Deletion still only happens at
real dispatch / emptying.
"""
import uuid

from django.utils import timezone

from ..models import PalletStatus


def reconcile_pallet_to_wms(company, pallet, *, note="Dispatched"):
    """Sync one barcode pallet's remaining state into the WMS map."""
    from wms.models import (
        Inventory as WmsInventory,
        Movement as WmsMovement,
        Pallet as WmsPallet,
    )

    plate = (pallet.pallet_id or "").strip()
    if not plate:
        return

    # "Staged" = loaded into a vehicle that hasn't left: free the location but
    # keep the pallet record. "Gone" = the pallet no longer holds stock at its
    # bin (fully dispatched or emptied). box_count is the remaining active-box
    # count after recalculation.
    staged = pallet.status == PalletStatus.INSIDE_VEHICLE
    gone = not staged and (
        (pallet.box_count or 0) <= 0 or pallet.status == PalletStatus.DISPATCHED
    )
    now = timezone.now().isoformat()

    for wms_pallet in WmsPallet.objects.filter(company=company, data__licensePlate=plate):
        data = wms_pallet.data or {}
        location_id = data.get("currentLocationId")
        inventory_qs = WmsInventory.objects.filter(
            company=company, data__palletId=wms_pallet.record_id
        )
        if staged:
            # Free the cell (with an OUTBOUND audit entry) but keep the pallet
            # record un-located so a reverted load can be re-placed via putaway.
            if location_id:
                record_id = str(uuid.uuid4())
                WmsMovement.objects.create(
                    company=company,
                    record_id=record_id,
                    data={
                        "id": record_id,
                        "type": "OUTBOUND",
                        "itemCode": data.get("itemCode", ""),
                        "itemName": data.get("itemName", ""),
                        "palletId": wms_pallet.record_id,
                        "fromLocationId": location_id,
                        "toLocationId": None,
                        "quantity": float(data.get("totalUnits") or 0),
                        "boxCount": data.get("boxCount") or 0,
                        "lotNumber": data.get("lotNumber", ""),
                        "userId": "",
                        "userName": "Dispatch",
                        "note": note,
                        "createdAt": now,
                    },
                )
            inventory_qs.delete()
            data.update(
                {
                    "currentLocationId": None,
                    "stagedInVehicle": True,
                    "updatedAt": now,
                }
            )
            wms_pallet.data = data
            wms_pallet.save(update_fields=["data", "updated_at"])
        elif gone:
            # Leave an OUTBOUND entry so the cell's audit trail records the exit.
            if location_id:
                record_id = str(uuid.uuid4())
                WmsMovement.objects.create(
                    company=company,
                    record_id=record_id,
                    data={
                        "id": record_id,
                        "type": "OUTBOUND",
                        "itemCode": data.get("itemCode", ""),
                        "itemName": data.get("itemName", ""),
                        "palletId": wms_pallet.record_id,
                        "fromLocationId": location_id,
                        "toLocationId": None,
                        "quantity": float(data.get("totalUnits") or 0),
                        "boxCount": data.get("boxCount") or 0,
                        "lotNumber": data.get("lotNumber", ""),
                        "userId": "",
                        "userName": "Dispatch",
                        "note": note,
                        "createdAt": now,
                    },
                )
            inventory_qs.delete()
            wms_pallet.delete()
        else:
            # Partial dispatch — the pallet is still at its bin with fewer boxes.
            data.update(
                {
                    "boxCount": pallet.box_count or 0,
                    "totalUnits": float(pallet.total_qty or 0),
                    "stagedInVehicle": False,
                    "updatedAt": now,
                }
            )
            wms_pallet.data = data
            wms_pallet.save(update_fields=["data", "updated_at"])
            for inventory in inventory_qs:
                inv_data = inventory.data or {}
                inv_data.update(
                    {
                        "boxCount": pallet.box_count or 0,
                        "quantity": float(pallet.total_qty or 0),
                        "updatedAt": now,
                    }
                )
                inventory.data = inv_data
                inventory.save(update_fields=["data", "updated_at"])

    # Drop the pallet-move bridge mirror inventory too once the pallet is gone
    # or staged in a vehicle (it represents bin occupancy, which has ended).
    if gone or staged:
        WmsInventory.objects.filter(
            company=company, record_id=f"bc-pallet-{pallet.id}"
        ).delete()
