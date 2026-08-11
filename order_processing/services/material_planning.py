"""BOM explosion → material requirements → procurement requirements.

    production requirement
        → explode the BOM
        → gross material requirement
        → net off stock, its own commitments, and open purchase orders
        → procurement requirement for whatever is still missing

The netting is the part that earns its keep. The specification is explicit that
this must be a *net* requirement, not "required minus physical stock":

    net = gross − max(on_hand − committed, 0) − open PO

Skipping open POs makes the system re-order the same material every cycle until
it arrives, which is the fastest way to have its output ignored.

Two refusals worth knowing:

* **A product with no BOM is reported, not treated as needing nothing.** An empty
  component list would silently say "no materials required" and the shortfall
  would evaporate.
* **When SAP cannot be read, the net figures are marked unusable** rather than
  computed from zeros — a zero on-hand reads as "buy everything".
"""
import logging
import uuid
from decimal import Decimal

from django.db import transaction

from ..integrations.sap import bom as bom_reader
from ..integrations.sap import inventory
from ..models import (
    MaterialRequirement,
    ProcurementRequirement,
    ProcurementStatus,
    ProductionRequirement,
    RequirementStatus,
)
from .order_sync import log_event

logger = logging.getLogger(__name__)
ZERO = Decimal("0")


@transaction.atomic
def plan_materials(requirement, *, correlation_id="", bom_depth=1):
    """Explode one production requirement into material requirements.

    Returns ``(materials, missing_bom, error)``. Re-running replaces the previous
    explosion for this requirement rather than adding to it, so a changed
    production quantity produces corrected materials, not doubled ones.
    """
    correlation_id = correlation_id or uuid.uuid4().hex
    company = requirement.sap_company
    if not company:
        return [], [], "No SAP company on the requirement — cannot read its BOM."

    try:
        components, bom_warehouses, missing = bom_reader.explode(
            company, requirement.item_code, requirement.quantity, depth=bom_depth,
        )
    except bom_reader.BomUnavailable as exc:
        log_event("BOM_READ_FAILED", correlation_id=correlation_id,
                  entity_type="ProductionRequirement", entity_id=requirement.pk,
                  source="SAP", result="FAILED", error=str(exc))
        return [], [], str(exc)

    if missing:
        # "Cannot be made" is a finding. Saying nothing would let the shortfall
        # disappear between production and procurement.
        log_event("BOM_MISSING", correlation_id=correlation_id,
                  entity_type="ProductionRequirement", entity_id=requirement.pk,
                  source="SAP", result="SKIPPED", detail={"items": missing})
        return [], missing, ""

    codes = list(components)
    # Components are checked in the warehouse the BOM ISSUES them from, not the
    # finished good's. The live BOMs issue from BH-PC while finished goods book
    # against GP-FG, so using the FG warehouse finds nothing and raises a purchase
    # for material already in the materials store.
    fallback = requirement.warehouse_code
    by_warehouse = {}
    for item in codes:
        by_warehouse.setdefault(bom_warehouses.get(item) or fallback, []).append(item)

    snapshots, open_po, po_known = {}, {}, True
    for warehouse, items in by_warehouse.items():
        snapshots[warehouse] = inventory.fetch_stock(company, items, warehouse)
        try:
            open_po.update(bom_reader.fetch_open_po_quantities(company, items, warehouse))
        except bom_reader.BomUnavailable:
            # Best-effort: an unreadable PO list must not stop planning, but
            # pretending it is zero would over-order. Treated as unknown, flagged.
            po_known = False

    # Re-explosion replaces: a requirement whose quantity dropped must not keep
    # yesterday's larger material lines alongside today's.
    MaterialRequirement.objects.filter(requirement=requirement).exclude(
        item_code__in=codes
    ).delete()

    written = []
    for item_code, gross in sorted(components.items()):
        warehouse = bom_warehouses.get(item_code) or fallback
        snapshot = snapshots[warehouse]
        stock = snapshot.get(item_code)
        known = snapshot.ok and stock.known and po_known
        usable = stock.available if snapshot.ok and stock.known else ZERO
        incoming = open_po.get(item_code, ZERO)
        net = max(gross - usable - incoming, ZERO) if known else ZERO

        material, _created = MaterialRequirement.objects.update_or_create(
            requirement=requirement, item_code=item_code,
            defaults={
                "warehouse_code": warehouse,
                "quantity_per_unit": (gross / requirement.quantity)
                                     if requirement.quantity else ZERO,
                "gross_required": gross,
                "on_hand": stock.on_hand, "committed": stock.committed,
                "incoming_po": incoming, "net_required": net, "stock_known": known,
            },
        )
        written.append(material)

    failed = [w for w, snap in snapshots.items() if not snap.ok]
    log_event("MATERIALS_PLANNED", correlation_id=correlation_id,
              entity_type="ProductionRequirement", entity_id=requirement.pk,
              source="SYSTEM",
              detail={"components": len(written),
                      "short": sum(1 for m in written if m.is_short),
                      "warehouses": sorted(by_warehouse), "po_known": po_known})
    return written, [], (snapshots[failed[0]].error if failed else "")


@transaction.atomic
def plan_procurement(*, correlation_id=""):
    """Roll every short material line into procurement requirements.

    Netted across production requirements: two runs needing the same cap are one
    thing to buy. Derived from the material lines each time, so a requirement that
    is no longer short disappears rather than lingering as a phantom order.
    """
    correlation_id = correlation_id or uuid.uuid4().hex

    shortages = (MaterialRequirement.objects
                 .filter(net_required__gt=0, stock_known=True,
                         requirement__status__in=[RequirementStatus.REQUIRED,
                                                  RequirementStatus.PLANNED])
                 .select_related("requirement"))

    grouped = {}
    for material in shortages:
        key = (material.item_code, material.warehouse_code)
        bucket = grouped.setdefault(key, {
            "quantity": ZERO, "incoming": ZERO, "materials": [],
            "sap_company": material.requirement.sap_company,
            "needed_by": material.requirement.needed_by,
            "item_name": material.item_name,
        })
        bucket["quantity"] += material.net_required
        # Incoming is a property of the ITEM, not of each requirement — summing it
        # per line would count the same purchase order once per production run.
        bucket["incoming"] = max(bucket["incoming"], material.incoming_po)
        bucket["materials"].append(material)
        if material.requirement.needed_by and (
            bucket["needed_by"] is None or material.requirement.needed_by < bucket["needed_by"]
        ):
            bucket["needed_by"] = material.requirement.needed_by

    created = updated = 0
    for (item_code, warehouse), data in grouped.items():
        procurement, was_created = ProcurementRequirement.objects.get_or_create(
            item_code=item_code, warehouse_code=warehouse,
            status__in=[ProcurementStatus.REQUIRED, ProcurementStatus.REQUESTED],
            defaults={"sap_company": data["sap_company"],
                      "item_name": data["item_name"],
                      "status": ProcurementStatus.REQUIRED},
        )
        procurement.quantity = data["quantity"]
        procurement.incoming_po = data["incoming"]
        procurement.needed_by = data["needed_by"]
        procurement.save(update_fields=["quantity", "incoming_po", "needed_by", "updated_at"])
        procurement.materials.set(data["materials"])
        created += int(was_created)
        updated += int(not was_created)

    # Anything no longer short is retired — a phantom purchase requirement is
    # worse than none, because someone will act on it.
    live = {(k[0], k[1]) for k in grouped}
    stale = ProcurementRequirement.objects.filter(
        status=ProcurementStatus.REQUIRED,
    ).exclude(
        item_code__in=[k[0] for k in live] or [""],
    )
    retired = 0
    for procurement in ProcurementRequirement.objects.filter(status=ProcurementStatus.REQUIRED):
        if (procurement.item_code, procurement.warehouse_code) not in live:
            procurement.status = ProcurementStatus.CANCELLED
            procurement.notes = "No production requirement still needs this."
            procurement.save(update_fields=["status", "notes", "updated_at"])
            retired += 1

    log_event("PROCUREMENT_PLANNED", correlation_id=correlation_id,
              entity_type="ProcurementRequirement", entity_id="",
              source="SYSTEM",
              detail={"created": created, "updated": updated, "retired": retired})
    return created, updated, retired


def plan_all(*, bom_depth=1):
    """Explode every open production requirement, then roll up procurement."""
    correlation_id = uuid.uuid4().hex
    exploded = skipped = failed = 0
    missing_boms = []

    for requirement in ProductionRequirement.objects.filter(
        status__in=[RequirementStatus.REQUIRED, RequirementStatus.PLANNED]
    ):
        materials, missing, error = plan_materials(
            requirement, correlation_id=correlation_id, bom_depth=bom_depth,
        )
        if error:
            failed += 1
        elif missing:
            skipped += 1
            missing_boms.extend(missing)
        else:
            exploded += 1

    created, updated, retired = plan_procurement(correlation_id=correlation_id)
    return {
        "exploded": exploded, "no_bom": skipped, "failed": failed,
        "missing_boms": missing_boms,
        "procurement_created": created, "procurement_updated": updated,
        "procurement_retired": retired,
    }
