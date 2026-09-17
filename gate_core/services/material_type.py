"""What kind of stock a raw-material gate entry is carrying: RM, PM, or both.

One ``RAW_MATERIAL`` gate entry can hold several POs and each PO several lines,
and those lines are not necessarily the same kind of stock -- a vehicle often
arrives with raw material and packaging on the same challan. The gate list needs
to say, per entry, which of the two (or both) is on board.

The RM/PM split is read off the *visible* item-code prefix, never the SAP item
group: the prefix is what gate staff see on the PO line, the item group is not,
and the two already-shipped rules that split the same way -- the weighment rule
(:mod:`gate_core.services.weighment_rules`) and the scan exemption
(:func:`gate_core.services.box_packing.is_pm_item_code`) -- key off the prefix
too. Reusing them here keeps one answer to "is this line RM or PM".

Anything that is neither (assets, consumables) is its own bucket: such an entry
reports ``OTHER`` rather than being forced into RM or PM. An entry whose lines
mix RM or PM *with* one of those still reports on the RM/PM it carries, because
that is the question the column asks.
"""

from gate_core.services.box_packing import is_pm_item_code
from gate_core.services.weighment_rules import is_rm_item_code

MATERIAL_TYPE_RM = "RM"
MATERIAL_TYPE_PM = "PM"
MATERIAL_TYPE_BOTH = "BOTH"
MATERIAL_TYPE_OTHER = "OTHER"

MATERIAL_TYPE_LABELS = {
    MATERIAL_TYPE_RM: "RM",
    MATERIAL_TYPE_PM: "PM",
    MATERIAL_TYPE_BOTH: "RM + PM",
    MATERIAL_TYPE_OTHER: "Other",
}


def classify_item_codes(item_codes):
    """Return the material-type code for a collection of PO item codes.

    ``None`` when there are no lines at all -- a gate entry that has not been
    given its PO yet carries nothing to classify.
    """
    has_rm = False
    has_pm = False
    has_any = False

    for code in item_codes:
        has_any = True
        if is_rm_item_code(code):
            has_rm = True
        elif is_pm_item_code(code):
            has_pm = True

    if has_rm and has_pm:
        return MATERIAL_TYPE_BOTH
    if has_rm:
        return MATERIAL_TYPE_RM
    if has_pm:
        return MATERIAL_TYPE_PM
    return MATERIAL_TYPE_OTHER if has_any else None


def entry_material_type(vehicle_entry, po_receipts=None):
    """The material-type code for one gate entry, across every PO on it.

    ``po_receipts`` lets a caller that has already loaded the receipts (the list
    serializer does, prefetched) hand them in instead of hitting the database a
    second time.
    """
    receipts = vehicle_entry.po_receipts.all() if po_receipts is None else po_receipts
    return classify_item_codes(
        item.po_item_code for receipt in receipts for item in receipt.items.all()
    )
