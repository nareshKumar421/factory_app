"""Setting and reading the raw-material quantities a warehouse states it holds.

Every write goes through here, for one reason: the per-warehouse manager check.
A store keeper may only state quantities for a warehouse they run, and a view
that wrote to the model directly would be one refactor away from losing that.

Reads are deliberately NOT scoped. Visibility is unrestricted across the
warehouse module — lists show everything, only the actions are gated — and a
register whose totals change depending on who is looking is worse than useless
for reconciliation. See ``services/warehouse_scope`` for the rules themselves.

The register covers **one** warehouse: the bulk-oil store, ``BH-LO``. Raw
material is not spread across stores the way packaging is — it is in the tank
farm — so offering a warehouse choice invited a keeper to file today's count
against the wrong one, where nothing would ever read it. The code is a setting
rather than a literal because it is company data, not a law of nature.

The item picker reads SAP (``OITM`` group 106 = RAW MATERIAL) so a keeper picks
a real item code instead of typing one; the quantity itself is the app's own
figure and nothing here posts to SAP. Note that the group, not the ``RM`` code
prefix, is what decides: ``PM0000457 LAKADONG TURMERIC`` and ``PM0000460
TURMERIC POWDER`` are both SAP group 106, and they are genuinely raw materials
whatever their codes say.
"""

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..models_rm_stock import RawMaterialStock, RawMaterialStockEntry
from . import warehouse_scope
from .wms_hana_reader import WMSHanaReader

logger = logging.getLogger(__name__)

# SAP item group for raw material. The same code in all three company schemas
# (105 = PACKAGING MATERIAL, 102 = FINISHED, 106 = RAW MATERIAL), and the group
# is what SAP actually classifies on — the `RM*` code prefix is only a naming
# convention and items exist that break it.
RM_ITEM_GROUP_CODE = 106

# The single warehouse this register covers. See the module docstring.
DEFAULT_RM_STOCK_WAREHOUSE = "BH-LO"


def register_warehouse() -> str:
    """The one warehouse raw-material quantities are stated against."""
    from django.conf import settings

    return str(
        getattr(settings, "RM_STOCK_WAREHOUSE", DEFAULT_RM_STOCK_WAREHOUSE)
    ).strip().upper()


# ---------------------------------------------------------------------------
# Item picker
# ---------------------------------------------------------------------------

def search_rm_items(
    *,
    company_code: str,
    search: str = "",
    warehouse_code: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    """Raw-material items from SAP, for the picker on the page.

    Always the register's own warehouse, so the SAP on-hand shown beside each
    item is the figure for the store the keeper is actually counting.
    """
    reader = WMSHanaReader(company_code=company_code)
    return reader.search_items_in_group(
        item_group_code=RM_ITEM_GROUP_CODE,
        search=search,
        warehouse_code=warehouse_code or register_warehouse(),
        limit=limit,
    )


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------

def list_stock(
    *,
    company_code: str,
    warehouse_code: Optional[str] = None,
    search: str = "",
    include_inactive: bool = False,
):
    """The register, filtered the way the page filters it."""
    rows = RawMaterialStock.objects.filter(
        company__code=company_code
    ).select_related("company", "set_by")

    if not include_inactive:
        rows = rows.filter(is_active=True)
    if warehouse_code:
        rows = rows.filter(warehouse_code=warehouse_code.strip().upper())

    term = (search or "").strip()
    if term:
        rows = rows.filter(Q(item_code__icontains=term) | Q(item_name__icontains=term))

    return rows.order_by("warehouse_code", "item_code")


@transaction.atomic
def set_quantity(
    *,
    user,
    company,
    warehouse_code: str,
    item_code: str,
    qty: Decimal,
    item_name: str = "",
    uom: str = "",
    as_of_date=None,
    remarks: str = "",
) -> RawMaterialStock:
    """Set the quantity for one item in one warehouse, creating the row if new.

    Upserts rather than refusing a duplicate: the page is a register of current
    quantities, and "set it to 40" must work the same whether or not somebody
    set it to 60 yesterday. The previous figure is not lost — it is written to
    :class:`RawMaterialStockEntry` before being replaced.

    A removed (deactivated) row is brought back rather than blocked, since the
    alternative is a keeper who can see nothing wrong and cannot save.
    """
    # The register covers one warehouse. An omitted code takes it; a different
    # one is refused rather than quietly rewritten — silently filing a count
    # against a store the caller did not name is worse than saying no.
    register = register_warehouse()
    warehouse_code = (warehouse_code or "").strip().upper() or register
    if warehouse_code != register:
        raise ValidationError({
            "warehouse_code": (
                f"The raw-material register covers {register} only; "
                f"{warehouse_code} cannot be set here."
            )
        })
    item_code = (item_code or "").strip().upper()
    if not item_code:
        raise ValidationError({"item_code": "Choose an item."})
    if qty is None:
        raise ValidationError({"qty": "Enter a quantity."})
    if Decimal(qty) < 0:
        raise ValidationError({"qty": "A stock quantity cannot be negative."})

    warehouse_scope.assert_manages(
        user,
        company.code,
        [warehouse_code],
        action="set raw-material stock",
    )

    as_of_date = as_of_date or timezone.localdate()
    if as_of_date > timezone.localdate():
        raise ValidationError(
            {"as_of_date": "A stock quantity cannot be true as of a future date."}
        )

    # select_for_update so two keepers saving the same item at once cannot both
    # read the old figure and write history that disagrees with the row.
    row = (
        RawMaterialStock.objects.select_for_update()
        .filter(company=company, warehouse_code=warehouse_code, item_code=item_code)
        .first()
    )

    if row is None:
        row = RawMaterialStock.objects.create(
            company=company,
            warehouse_code=warehouse_code,
            item_code=item_code,
            item_name=item_name or "",
            uom=uom or "",
            qty=qty,
            as_of_date=as_of_date,
            remarks=remarks or "",
            set_by=user,
        )
        _log(
            row,
            action=RawMaterialStockEntry.Action.CREATED,
            previous_qty=None,
            user=user,
            remarks=remarks,
        )
        return row

    previous_qty = row.qty
    was_inactive = not row.is_active

    row.qty = qty
    row.as_of_date = as_of_date
    row.remarks = remarks or ""
    row.set_by = user
    row.is_active = True
    fields = ["qty", "as_of_date", "remarks", "set_by", "is_active", "updated_at"]
    # Only overwrite the cached SAP text when the caller actually supplied it,
    # so a save from a screen that did not look the item up cannot blank it.
    if item_name:
        row.item_name = item_name
        fields.append("item_name")
    if uom:
        row.uom = uom
        fields.append("uom")
    row.save(update_fields=fields)

    _log(
        row,
        action=(
            RawMaterialStockEntry.Action.RESTORED
            if was_inactive
            else RawMaterialStockEntry.Action.UPDATED
        ),
        previous_qty=previous_qty,
        user=user,
        remarks=remarks,
    )
    return row


@transaction.atomic
def remove_row(
    *, user, company, row: RawMaterialStock, remarks: str = ""
) -> RawMaterialStock:
    """Take an item off the register, keeping the row and its history.

    Deactivates rather than deletes, matching ``UserWarehouse``: the row is the
    evidence this material was once held here, and the quantity trail hangs off
    it.
    """
    warehouse_scope.assert_manages(
        user,
        company.code,
        [row.warehouse_code],
        action="remove raw-material stock",
    )
    if not row.is_active:
        return row

    row.is_active = False
    row.set_by = user
    row.save(update_fields=["is_active", "set_by", "updated_at"])
    _log(
        row,
        action=RawMaterialStockEntry.Action.REMOVED,
        previous_qty=row.qty,
        user=user,
        remarks=remarks,
    )
    return row


def history_for(*, company_code: str, row: RawMaterialStock):
    """Every change to one register row, newest first.

    Matched on the item and warehouse rather than the row id: the point of the
    denormalised columns is that the trail survives a row being removed and a
    later one taking its place.
    """
    return (
        RawMaterialStockEntry.objects.filter(
            company__code=company_code,
            warehouse_code=row.warehouse_code,
            item_code=row.item_code,
        )
        .select_related("changed_by")
        .order_by("-changed_at", "-id")
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _log(row: RawMaterialStock, *, action: str, previous_qty, user, remarks: str) -> None:
    RawMaterialStockEntry.objects.create(
        stock=row,
        company=row.company,
        warehouse_code=row.warehouse_code,
        item_code=row.item_code,
        action=action,
        previous_qty=previous_qty,
        qty=row.qty,
        as_of_date=row.as_of_date,
        remarks=remarks or "",
        changed_by=user if getattr(user, "is_authenticated", False) else None,
    )
