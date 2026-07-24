"""When gate weighment is mandatory.

Weighment is not a blanket gate requirement: most vehicles (daily need,
maintenance, construction, fixed asset, empty vehicle, packaging-material and
BST loads) leave without ever touching the weighbridge. It is mandatory only for

* **raw-material** vehicles carrying RM stock, and
* **job-work** vehicles.

RM vs PM is read off the *visible* item-code prefix (``RM…``), not the SAP item
group -- the item group is invisible to gate staff and not trusted, so keying the
rule on it would make packaging vehicles ask for a weighment the operator can't
explain. A raw-material entry counts as "needs weighment" only when *every* PO
line is an RM item; any packaging (or other) line makes it a mixed load and the
weighment stays optional, so a PM-only or mixed vehicle is never blocked.

Physically the weighment is captured in two passes on one ``Weighment`` record:
the loaded **gross** weight at raw-material gate-in, and the empty **tare** weight
at gate-out (empty vehicle out). So gate-in only needs gross to be present, while
gate-out needs the full gross+tare pair.
"""

RM_ITEM_CODE_PREFIX = "RM"


def is_rm_item_code(item_code):
    """True when an item code identifies raw material (``RM`` prefix)."""
    return bool(item_code) and item_code.strip().upper().startswith(RM_ITEM_CODE_PREFIX)


def raw_material_entry_is_all_rm(vehicle_entry):
    """True when the entry has PO lines and every one of them is an RM item.

    A mixed load (any non-RM/PM line) or a PM-only load returns False, so it is
    never forced through the weighbridge.
    """
    item_codes = [
        item.po_item_code
        for po_receipt in vehicle_entry.po_receipts.all()
        for item in po_receipt.items.all()
    ]
    return bool(item_codes) and all(is_rm_item_code(code) for code in item_codes)


def gate_out_requires_weighment(vehicle_entry):
    """Whether marking this vehicle out must capture a full (gross+tare) weighment."""
    entry_type = vehicle_entry.entry_type
    if entry_type == "RAW_MATERIAL":
        return raw_material_entry_is_all_rm(vehicle_entry)
    if entry_type == "JOB_WORK":
        return True
    return False


def gate_in_requires_weighment(vehicle_entry):
    """Whether completing this gate-in must capture at least the loaded gross weight.

    Only all-RM raw-material entries; every other type completes without it.
    """
    return (
        vehicle_entry.entry_type == "RAW_MATERIAL"
        and raw_material_entry_is_all_rm(vehicle_entry)
    )
