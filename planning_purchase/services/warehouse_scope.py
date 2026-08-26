"""Which warehouses count as available stock, per kind of material.

Packaging and oil are not kept in the same places, and scoping them together
lets a packaging item pick up stock from a tank farm and an oil pick up stock
from a carton store. So the scope is per material type:

    PACKAGING   BH-PS, BH-PC, BH-PM      the packaging stores
    RAW         BH-LO, BH-OT             the bulk-oil stores
    OTHER       the union of both        nothing should silently read as zero

Scoping at all matters more than which list wins. Summing the whole estate counts
finished-goods godowns, non-moving stores, job-work locations and wastage as
though production could draw on them, which understates every shortage and
under-buys — the expensive direction to be wrong in.

Note what the RAW list leaves out: `BH-PC` holds 93 K litres of raw material, and
for several oils it holds *more* than `BH-LO` does — RM0000011 has 33,646 there
against 204 in the tank farm. That is deliberate. `BH-PC` is Production
Consumption, a staging area for material already pulled towards a run, so it is
not free for a new plan to spend. Move it into the RAW list if the business
decides otherwise; it is a setting, not a code change.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from django.conf import settings

PACKAGING = "PACKAGING"
RAW = "RAW"
OTHER = "OTHER"

DEFAULT_PACKAGING = ("BH-PS", "BH-PC", "BH-PM")
DEFAULT_RAW = ("BH-LO", "BH-OT")


def _clean(values) -> List[str]:
    """Tolerate a hand-edited .env: trim padding, drop blanks."""
    return [str(code).strip() for code in (values or []) if str(code).strip()]


def packaging_warehouses() -> List[str]:
    return _clean(
        getattr(settings, "PLANNING_PURCHASE_PM_WAREHOUSES", DEFAULT_PACKAGING)
    )


def raw_warehouses() -> List[str]:
    return _clean(getattr(settings, "PLANNING_PURCHASE_RM_WAREHOUSES", DEFAULT_RAW))


def scope_by_material_type() -> Dict[str, List[str]]:
    """The configured scope, keyed by material type.

    `OTHER` gets the union rather than an empty list. A component SAP groups as
    neither packaging nor raw — a consumable, say — would otherwise report zero
    stock and be ordered in full, which is a worse failure than counting it in
    slightly too many places.
    """
    packaging = packaging_warehouses()
    raw = raw_warehouses()
    combined = list(dict.fromkeys([*packaging, *raw]))
    return {PACKAGING: packaging, RAW: raw, OTHER: combined}


def all_scoped_warehouses() -> List[str]:
    """Every warehouse in any scope, for a single fetch covering all components."""
    scope = scope_by_material_type()
    return list(dict.fromkeys([*scope[PACKAGING], *scope[RAW]]))


def warehouses_for(material_type: str) -> List[str]:
    scope = scope_by_material_type()
    return scope.get(material_type, scope[OTHER])


def counts(material_type: str, warehouse: str, override: Sequence[str] | None) -> bool:
    """Whether a stock row counts towards a component of this material type.

    `override` is a caller-supplied warehouse filter — a user narrowing the view.
    It replaces the per-type scope entirely rather than intersecting with it, so
    asking for one warehouse shows that warehouse and not an empty table.
    """
    if override:
        return warehouse in set(override)
    allowed = warehouses_for(material_type)
    # An unconfigured scope falls back to counting everything. Reporting no stock
    # would raise a purchase order for the whole plan, so this is the safer way
    # for a misconfiguration to fail, and the response states which way it went.
    return True if not allowed else warehouse in set(allowed)
