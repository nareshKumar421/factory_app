"""The rules that move an oil lot from contract to tank.

EXIM's ``stock.services`` and the hooks it had hidden in ``StockStatus.save()``,
gathered in one place. Views, the bulk actions and the EXIM copy all come
through here, and every change writes the lot's history (``LotChange``).

THE WAYS A LOT MOVES (EXIM's names in brackets)
 - ``move_lot``     the whole lot changes status ("bulk"). A different weighed
                    quantity is either absorbed (TOLERATE) or handed back to the
                    storage lot it came from (RETAIN).
 - ``dispatch_lot`` part of a lot leaves as a new lot in the new status
                    ("batch"): a truck loaded from a contract, say. The source
                    keeps the rest (RETAIN), or is closed with it (TOLERATE).
 - ``arrive_lot``   a lot reaches a refinery and joins the one lot there that
                    collects every arrival from the same storage lot ("arrive
                    batch").
 - ``into_tank``    a lot is weighed into the tank farm (or a warehouse). Weighed
                    at less than it was loaded, the shortage is recorded; either
                    way the arrival is written to the tank log.

WHAT CHANGED FROM EXIM, AND WHY
 - DEBIT is gone. EXIM offered it on every move, and its code created the
   shortage with fields the shortage does not have, so it could only ever fail.
   A shortage is recorded where it always really was: on the way into the tank.
 - The tank log is written the FIRST time a lot enters the tanks, whatever it
   weighed. EXIM only wrote it when the weight had changed, so a lot that
   arrived exactly as loaded never appeared in the log at all.
 - A move no longer blanks the arrival date and location it was not given.
 - A dispatch keeps the payment status it was given (EXIM's screen asked for
   one and the dispatch dropped it), and an arrival keeps its job work vendor
   (the screen sent ``job_work_vendor``, the model's field is ``job_work``).
 - Who did it is the signed-in person, not a ``created_by`` the browser sent.
 - The crude-to-canola conversion at the refinery is NOT carried. EXIM compared
   the oil's name with "Crude Oil", and the crude oil is named "CRUDE CANOLA",
   so the rule never ran on any lot EXIM holds.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max, Sum

from .models_lot import (
    LotChange,
    LotChangeAction,
    LotFieldChange,
    LotShortage,
    LotStatus,
    OilLot,
    StockDashboardRow,
    TemporaryVendor,
)
from .models_tank import Tank, TankItem, TankLog
from .services_licence import EximError
from .services_tank import DENSITY

#: The share of the loaded quantity a supplier is allowed to lose in transit.
ALLOWED_SHORTAGE = Decimal("0.0025")

#: The fields a lot's history records, as EXIM's ``TRACKED_FIELDS``.
TRACKED_FIELDS = ("status", "rate", "quantity", "vehicle_number", "location", "eta")

_CENT = Decimal("0.01")
_MILL = Decimal("0.001")


class Action:
    RETAIN = "RETAIN"
    TOLERATE = "TOLERATE"


# ---------------------------------------------------------------------------
# Saving a lot, with EXIM's hooks
# ---------------------------------------------------------------------------

def _derive(lot: OilLot) -> None:
    """Litres, rate per litre and total, from the kilograms and rate per kg."""
    if lot.quantity is not None and lot.rate is not None:
        lot.quantity_litres = (Decimal(lot.quantity) * DENSITY).quantize(_CENT, rounding=ROUND_HALF_UP)
        lot.rate_per_litre = (Decimal(lot.rate) / DENSITY).quantize(_MILL, rounding=ROUND_HALF_UP)
        lot.total = (Decimal(lot.quantity) * Decimal(lot.rate)).quantize(_CENT, rounding=ROUND_HALF_UP)
    if lot.quantity is not None and Decimal(lot.quantity) <= 0:
        # A lot with nothing left is done with; EXIM removed it the same way.
        lot.deleted = True


def _record_shortage(lot: OilLot, loaded_kg, user) -> LotShortage:
    load_mt = Decimal(loaded_kg) / 1000
    unload_mt = Decimal(lot.quantity) / 1000
    rate_mt = Decimal(lot.rate) * 1000
    shortage = load_mt - unload_mt
    allowed = load_mt * ALLOWED_SHORTAGE
    deducted = shortage - allowed if shortage > allowed else Decimal("0")
    return LotShortage.objects.create(
        company=lot.company,
        lot=lot,
        item_code=lot.item.code,
        item_name=lot.item.name,
        supplier_code=lot.vendor_code,
        supplier=lot.vendor_name,
        vehicle_number=lot.vehicle_number,
        transporter=lot.transporter,
        bilty_number=lot.bilty_number,
        grpo_number=lot.grpo_number,
        rate=rate_mt.quantize(_MILL),
        load_qty_mt=load_mt.quantize(_MILL),
        unload_qty_mt=unload_mt.quantize(_MILL),
        shortage_mt=shortage.quantize(_MILL),
        allowed_mt=allowed.quantize(_MILL),
        deducted_mt=deducted.quantize(_MILL),
        deduction_amount=(deducted * rate_mt).quantize(_MILL),
        created_by=user,
        created_by_label=_label(user),
    )


def _record_tank_arrival(lot: OilLot, user) -> TankLog:
    return TankLog.objects.create(
        company=lot.company,
        lot=lot,
        quantity_kg=lot.quantity,
        rate=lot.rate,
        vehicle_number=lot.vehicle_number,
        party=lot.vendor_name or lot.vendor_code,
        item_code=lot.item.code,
        item_name=lot.item.name,
        arrival=lot.eta,
        created_by=user,
        created_by_label=_label(user),
    )


def _save(lot: OilLot, *, user, before: dict | None = None) -> OilLot:
    """Save a lot and run what EXIM's ``save()`` ran. ``before`` is the lot's
    status, quantity and eta before this change (None for a new lot)."""
    old_status = before["status"] if before else None
    if (
        before
        and old_status != LotStatus.OUT_SIDE_FACTORY
        and lot.status == LotStatus.OUT_SIDE_FACTORY
        and not lot.arrival_date
    ):
        # Reaching the factory gate: the date it was expected is the date it came.
        lot.arrival_date = before["eta"]
    _derive(lot)
    lot.save()

    if before and old_status != LotStatus.IN_TANK and lot.status == LotStatus.IN_TANK:
        if before["quantity"] is not None and Decimal(before["quantity"]) != Decimal(lot.quantity):
            _record_shortage(lot, before["quantity"], user)
        if not lot.tank_logs.exists():
            _record_tank_arrival(lot, user)
    # No settling here: which lots the tanks still hold is decided by the
    # tanks' own levels (``services_tank``), and a lot arriving before the
    # store has dipped the tank would otherwise be marked completed at once.
    return lot


def _before(lot: OilLot) -> dict:
    return {"status": lot.status, "quantity": lot.quantity, "eta": lot.eta}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _label(user) -> str:
    return (getattr(user, "email", "") or "") if user else ""


def _value(lot: OilLot, name: str):
    """A field as the database holds it: a decimal at its own places, so a lot
    given 10000 and read back as 10000.00 is not a change."""
    value = getattr(lot, name)
    if value is None:
        return None
    if isinstance(value, Decimal):
        places = OilLot._meta.get_field(name).decimal_places
        return str(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
    return str(value)


def snapshot(lot: OilLot) -> dict:
    return {name: _value(lot, name) for name in TRACKED_FIELDS}


def _audit(lot: OilLot, *, user, action, before_snapshot=None, note="") -> LotChange:
    change = LotChange.objects.create(
        lot=lot, action=action, changed_by=user, changed_by_label=_label(user), note=note[:255],
    )
    if action == LotChangeAction.CREATE:
        LotFieldChange.objects.create(change=change, field_name="__create__", new_value=snapshot(lot))
    elif before_snapshot is not None:
        now = snapshot(lot)
        LotFieldChange.objects.bulk_create(
            [
                LotFieldChange(change=change, field_name=name, old_value=before_snapshot[name], new_value=now[name])
                for name in TRACKED_FIELDS
                if before_snapshot[name] != now[name]
            ]
        )
    return change


# ---------------------------------------------------------------------------
# Entering and correcting a lot
# ---------------------------------------------------------------------------

#: What a person may enter or correct on a lot directly. Status is not here:
#: it changes only through the moves below.
LOT_FIELDS = (
    "rate",
    "quantity",
    "vehicle_number",
    "transporter",
    "location",
    "eta",
    "arrival_date",
    "bilty_number",
    "grpo_number",
    "contract_start",
    "contract_end",
    "payment_status",
    "job_work",
)


def vendor_name_for(company, code: str, given: str = "") -> str:
    """A temporary vendor's name is ours; any other is taken as the picker gave it."""
    temp = TemporaryVendor.objects.filter(company=company, code=code).first()
    return temp.name if temp else (given or "").strip()


@transaction.atomic
def create_lot(*, company, user, item: TankItem, status, vendor_code, vendor_name="", **data) -> OilLot:
    if item.company_id != company.id:
        raise EximError("That oil belongs to another company.", "oil_invalid", {})
    lot = OilLot(
        company=company,
        item=item,
        status=status,
        vendor_code=vendor_code.strip(),
        vendor_name=vendor_name_for(company, vendor_code.strip(), vendor_name),
        created_by=user,
        created_by_label=_label(user),
    )
    for name in LOT_FIELDS:
        if name in data and data[name] is not None:
            setattr(lot, name, data[name])
    _check_total(lot)
    _save(lot, user=user)
    _audit(lot, user=user, action=LotChangeAction.CREATE)
    return lot


def _check_total(lot: OilLot) -> None:
    # EXIM's screen refused a lot worth ten billion rupees or more; the total
    # column holds no more.
    if Decimal(lot.quantity) * Decimal(lot.rate) >= Decimal("1e10"):
        raise EximError("Rate x quantity is too large for one lot.", "total_too_large", {})


@transaction.atomic
def update_lot(lot: OilLot, *, user, **data) -> OilLot:
    """Correct what was entered, the status aside."""
    before, before_snapshot = _before(lot), snapshot(lot)
    for name in LOT_FIELDS:
        if name in data:
            value = data[name]
            field = lot._meta.get_field(name)
            if value is None and not field.null:
                value = field.get_default()
            setattr(lot, name, value)
    _check_total(lot)
    _save(lot, user=user, before=before)
    _audit(lot, user=user, action=LotChangeAction.UPDATE, before_snapshot=before_snapshot)
    return lot


@transaction.atomic
def delete_lot(lot: OilLot, *, user) -> OilLot:
    before_snapshot = snapshot(lot)
    lot.deleted = True
    lot.save(update_fields=["deleted", "updated_at"])
    _audit(lot, user=user, action=LotChangeAction.UPDATE, before_snapshot=before_snapshot, note="removed")
    return lot


# ---------------------------------------------------------------------------
# Moving a lot
# ---------------------------------------------------------------------------

def storage_parent(lot: OilLot):
    """Where a lot rests: itself if it is a storage lot, else the lot it came from."""
    return lot if lot.can_be_parent else lot.parent


def _return_to_parent(lot: OilLot, difference: Decimal, user) -> None:
    parent = storage_parent(lot)
    if parent is None or parent.deleted or parent.pk == lot.pk:
        raise EximError(
            "There is no storage lot to hand the difference back to. Choose Tolerate.",
            "retain_without_parent",
            {"lot": lot.pk},
        )
    before, before_snapshot = _before(parent), snapshot(parent)
    parent.quantity = Decimal(parent.quantity) + difference
    _save(parent, user=user, before=before)
    _audit(parent, user=user, action=LotChangeAction.UPDATE, before_snapshot=before_snapshot,
           note=f"difference from #{lot.pk} returned")


def _check_action(action, difference) -> str:
    if difference == 0:
        return Action.TOLERATE
    if action not in (Action.RETAIN, Action.TOLERATE):
        raise EximError(
            "The quantity changed: say whether to hand the difference back (Retain) or absorb it (Tolerate).",
            "action_required",
            {"difference": str(difference)},
        )
    return action


@transaction.atomic
def move_lot(lot: OilLot, *, user, status, quantity, action=None, arrival_date=None,
             location=None, payment_status=None) -> OilLot:
    """The whole lot changes status (EXIM's "bulk")."""
    if lot.deleted:
        raise EximError("That lot has been removed.", "lot_removed", {"lot": lot.pk})
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise EximError("The quantity must be more than nothing.", "quantity_required", {})
    difference = Decimal(lot.quantity) - quantity
    action = _check_action(action, difference)
    if difference != 0 and action == Action.RETAIN:
        _return_to_parent(lot, difference, user)

    before, before_snapshot = _before(lot), snapshot(lot)
    lot.status = status
    lot.quantity = quantity
    if arrival_date is not None:
        lot.arrival_date = arrival_date
    if location is not None:
        lot.location = location
    if payment_status:
        lot.payment_status = payment_status
    _save(lot, user=user, before=before)
    _audit(lot, user=user, action=LotChangeAction.UPDATE, before_snapshot=before_snapshot,
           note=f"moved to {LotStatus(status).label}")
    return lot


@transaction.atomic
def dispatch_lot(source: OilLot, *, user, status, quantity, action=None, vehicle_number="",
                 transporter="", location="", eta=None, payment_status=None) -> OilLot:
    """Part of a lot leaves as a new lot (EXIM's "batch"). Returns the new lot."""
    if source.deleted:
        raise EximError("That lot has been removed.", "lot_removed", {"lot": source.pk})
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise EximError("The quantity must be more than nothing.", "quantity_required", {})
    if quantity > Decimal(source.quantity):
        raise EximError(
            f"Only {source.quantity} kg is left on the lot.",
            "dispatch_too_large",
            {"available": str(source.quantity), "requested": str(quantity)},
        )
    remainder = Decimal(source.quantity) - quantity
    action = _check_action(action, remainder)
    parent = storage_parent(source)

    before, before_snapshot = _before(source), snapshot(source)
    source.quantity = remainder
    if action == Action.TOLERATE:
        # Nothing more is expected from the source: it closes with the dispatch.
        source.quantity = Decimal("0")
    _save(source, user=user, before=before)

    new = OilLot(
        company=source.company,
        item=source.item,
        status=status,
        vendor_code=source.vendor_code,
        vendor_name=source.vendor_name,
        rate=source.rate,
        quantity=quantity,
        parent=parent,
        vehicle_number=vehicle_number or "",
        transporter=transporter or "",
        location=location or "",
        eta=eta,
        contract_start=source.contract_start,
        contract_end=source.contract_end,
        payment_status=payment_status or source.payment_status,
        created_by=user,
        created_by_label=_label(user),
    )
    _save(new, user=user)
    _audit(new, user=user, action=LotChangeAction.CREATE, note=f"dispatched from #{source.pk}")
    _audit(source, user=user, action=LotChangeAction.UPDATE, before_snapshot=before_snapshot,
           note="dispatch source reduced")
    return new


@transaction.atomic
def arrive_lot(lot: OilLot, *, user, weighed_qty, status=LotStatus.AT_REFINERY, action=None,
               job_work="") -> OilLot:
    """A lot reaches a refinery (EXIM's "arrive batch"). Returns the lot there
    that collects it and every other arrival from the same storage lot."""
    if lot.deleted:
        raise EximError("That lot has been removed.", "lot_removed", {"lot": lot.pk})
    weighed = Decimal(weighed_qty)
    if weighed <= 0:
        raise EximError("The weighed quantity must be more than nothing.", "quantity_required", {})
    difference = Decimal(lot.quantity) - weighed
    action = _check_action(action, difference)
    parent = storage_parent(lot)

    accumulator = None
    if parent is not None:
        accumulator = OilLot.objects.filter(
            parent=parent, status=status, is_accumulator=True, deleted=False,
        ).first()
    if accumulator is not None:
        before, before_snapshot = _before(accumulator), snapshot(accumulator)
        accumulator.quantity = Decimal(accumulator.quantity) + weighed
        if job_work:
            accumulator.job_work = job_work
        _save(accumulator, user=user, before=before)
        _audit(accumulator, user=user, action=LotChangeAction.UPDATE, before_snapshot=before_snapshot,
               note=f"arrival of #{lot.pk}")
    else:
        accumulator = OilLot(
            company=lot.company,
            item=lot.item,
            status=status,
            vendor_code=lot.vendor_code,
            vendor_name=lot.vendor_name,
            rate=lot.rate,
            quantity=weighed,
            parent=parent,
            is_accumulator=True,
            job_work=job_work or "",
            payment_status=lot.payment_status,
            created_by=user,
            created_by_label=_label(user),
        )
        _save(accumulator, user=user)
        _audit(accumulator, user=user, action=LotChangeAction.CREATE, note=f"arrival of #{lot.pk}")

    if difference != 0 and action == Action.RETAIN:
        _return_to_parent(lot, difference, user)

    before_snapshot = snapshot(lot)
    lot.deleted = True
    lot.save(update_fields=["deleted", "updated_at"])
    _audit(lot, user=user, action=LotChangeAction.UPDATE, before_snapshot=before_snapshot,
           note=f"arrived at {LotStatus(status).label}")
    return accumulator


INTO_STORE = (LotStatus.IN_TANK, LotStatus.IN_WAREHOUSE)


@transaction.atomic
def into_tank(lot: OilLot, *, user, weighed_qty, status=LotStatus.IN_TANK, bilty_number=None,
              grpo_number=None) -> OilLot:
    """A lot weighed into the tank farm (or a warehouse). A weight below what was
    loaded records the shortage; the first arrival into the tanks is logged."""
    if lot.deleted:
        raise EximError("That lot has been removed.", "lot_removed", {"lot": lot.pk})
    if status not in INTO_STORE:
        raise EximError("A lot goes into a tank or a warehouse.", "status_invalid", {})
    weighed = Decimal(weighed_qty)
    if weighed <= 0:
        raise EximError("The weighed quantity must be more than nothing.", "quantity_required", {})
    before, before_snapshot = _before(lot), snapshot(lot)
    lot.status = status
    lot.quantity = weighed
    if bilty_number is not None:
        lot.bilty_number = bilty_number.strip()
    if grpo_number is not None:
        lot.grpo_number = grpo_number.strip()
    _save(lot, user=user, before=before)
    _audit(lot, user=user, action=LotChangeAction.UPDATE, before_snapshot=before_snapshot,
           note=f"into {LotStatus(status).label.lower()}")
    return lot


# ---------------------------------------------------------------------------
# Several lots at once (EXIM's bulk bar)
# ---------------------------------------------------------------------------

BULK_ACTIONS = ("arrive_refinery", "mark_in_tank", "delete")


@transaction.atomic
def bulk(lots, *, user, action) -> int:
    """All or nothing: one lot that cannot move stops the lot of them."""
    for lot in lots:
        if action == "arrive_refinery":
            arrive_lot(lot, user=user, weighed_qty=lot.quantity, action=Action.TOLERATE)
        elif action == "mark_in_tank":
            if lot.status != LotStatus.COMPLETED:
                raise EximError(
                    f"Lot #{lot.pk} is not completed; only completed lots go back into the tanks this way.",
                    "not_completed",
                    {"lot": lot.pk},
                )
            move_lot(lot, user=user, status=LotStatus.IN_TANK, quantity=lot.quantity, action=Action.TOLERATE)
        elif action == "delete":
            delete_lot(lot, user=user)
        else:
            raise EximError("Unknown bulk action.", "action_invalid", {"action": action})
    return len(lots)


# ---------------------------------------------------------------------------
# Opening stock and temporary vendors
# ---------------------------------------------------------------------------

@transaction.atomic
def opening_stock(*, company, user, item: TankItem, rate_per_litre, quantity_litres,
                  vendor_code, vendor_name, location="Sonipat Factory") -> OilLot:
    """What an oil's tanks held before lots were tracked, entered as a lot already
    IN_TANK so the average cost has something to start from. EXIM's
    ``opening-stock``, which took litres and a rate per litre."""
    quantity_kg = Decimal(quantity_litres) / DENSITY
    rate_kg = Decimal(rate_per_litre) * DENSITY
    lot = create_lot(
        company=company,
        user=user,
        item=item,
        status=LotStatus.IN_TANK,
        vendor_code=vendor_code,
        vendor_name=vendor_name,
        rate=rate_kg.quantize(_MILL, rounding=ROUND_HALF_UP),
        quantity=quantity_kg.quantize(_CENT, rounding=ROUND_HALF_UP),
        location=location,
    )
    return lot


def create_temporary_vendor(*, company, user, name: str) -> TemporaryVendor:
    name = (name or "").strip()
    if not name:
        raise EximError("Give the vendor's name.", "name_required", {})
    for _ in range(5):
        taken = set()
        for code in TemporaryVendor.objects.filter(company=company, code__startswith="TEMP").values_list(
            "code", flat=True
        ):
            try:
                taken.add(int(code[4:]))
            except ValueError:
                continue
        number = max(taken, default=0) + 1
        try:
            with transaction.atomic():
                return TemporaryVendor.objects.create(
                    company=company, code=f"TEMP{number:04d}", name=name[:255], created_by=user,
                )
        except IntegrityError:
            continue
    raise EximError("Could not number the vendor; try again.", "vendor_code_busy", {})


# ---------------------------------------------------------------------------
# Read-outs
# ---------------------------------------------------------------------------

def active_lots(company):
    return OilLot.objects.filter(company=company, deleted=False)


def insights(lots) -> dict:
    """Totals and average prices over a set of lots. EXIM's ``stock-insights``."""
    agg = lots.aggregate(
        total_value=Sum(F("quantity") * F("rate")),
        total_qty=Sum("quantity"),
        total_qty_litres=Sum("quantity_litres"),
        count=Count("id"),
        weighted_litres=Sum(F("quantity_litres") * F("rate_per_litre")),
    )
    qty = agg["total_qty"] or Decimal("0")
    litres = agg["total_qty_litres"] or Decimal("0")
    value = agg["total_value"] or Decimal("0")
    return {
        "count": agg["count"],
        "total_value": Decimal(value).quantize(_CENT),
        "total_qty": qty,
        "total_qty_litres": litres,
        "avg_price_per_kg": (Decimal(value) / qty).quantize(_CENT) if qty > 0 else Decimal("0"),
        "avg_price_per_litre": (
            (Decimal(agg["weighted_litres"] or 0) / litres).quantize(_CENT) if litres > 0 else Decimal("0")
        ),
    }


#: The stock dashboard's status columns, in EXIM's order. Out-side-factory has a
#: column of its own and IN_TANK comes from the tanks, so neither is here.
DASHBOARD_STATUSES = (
    LotStatus.ON_THE_WAY,
    LotStatus.UNDER_LOADING,
    LotStatus.AT_REFINERY,
    LotStatus.OTW_TO_REFINERY,
    LotStatus.KANDLA_STORAGE,
    LotStatus.MUNDRA_PORT,
    LotStatus.ON_THE_SEA,
    LotStatus.IN_CONTRACT,
    LotStatus.IN_TRANSIT,
    LotStatus.PENDING,
    LotStatus.PROCESSING,
    LotStatus.COMPLETED,
    LotStatus.DELIVERED,
)


def stock_dashboard(company, *, item_id=None, vendor_code=None, status=None) -> dict:
    """Kilograms of every oil in every status, split by vendor. EXIM's
    ``stock-dashboard``, row order and all."""
    lots = active_lots(company)
    if item_id:
        lots = lots.filter(item_id=item_id)
    if vendor_code:
        lots = lots.filter(vendor_code=vendor_code)
    if status:
        lots = lots.filter(status=status)

    outside = {
        row["item_id"]: row["qty"] or Decimal("0")
        for row in lots.filter(status=LotStatus.OUT_SIDE_FACTORY).values("item_id").annotate(qty=Sum("quantity"))
    }
    cells = {}
    vendors_by_status = {}
    names = {}
    for row in (
        lots.exclude(status__in=[LotStatus.OUT_SIDE_FACTORY, LotStatus.IN_TANK])
        .values("item_id", "status", "vendor_name", "vendor_code")
        .annotate(qty=Sum("quantity"))
    ):
        vendor = row["vendor_name"] or row["vendor_code"]
        cells[(row["item_id"], row["status"], vendor)] = cells.get(
            (row["item_id"], row["status"], vendor), Decimal("0")
        ) + (row["qty"] or 0)
        vendors_by_status.setdefault(row["status"], set()).add(vendor)

    item_ids = {key[0] for key in cells} | set(outside)
    if not vendor_code and not status:
        # An oil held in the tanks with no lot in play is still on the
        # dashboard (its in-tank column is read from the tanks), so it keeps
        # its place in the shared order rather than falling to the end.
        held = Tank.objects.filter(company=company, is_active=True, item__isnull=False)
        if item_id:
            held = held.filter(item_id=item_id)
        item_ids |= set(held.values_list("item_id", flat=True))
    for item in TankItem.objects.filter(pk__in=item_ids):
        names[item.pk] = (item.code, item.name)
    order = {row.item_id: row.position for row in StockDashboardRow.objects.filter(company=company)}
    ordered = sorted(item_ids, key=lambda pk: (0, order[pk]) if pk in order else (1, names[pk][0]))

    columns = [
        {"status": s, "label": LotStatus(s).label, "vendors": sorted(vendors_by_status[s])}
        for s in DASHBOARD_STATUSES
        if vendors_by_status.get(s)
    ]
    column_totals = {}
    status_totals = {}
    rows = []
    for pk in ordered:
        out = outside.get(pk, Decimal("0"))
        row_total = out
        values = {}
        for column in columns:
            for vendor in column["vendors"]:
                key = f"{column['status']}__{vendor}"
                value = cells.get((pk, column["status"], vendor), Decimal("0"))
                values[key] = value
                row_total += value
                column_totals[key] = column_totals.get(key, Decimal("0")) + value
                status_totals[column["status"]] = status_totals.get(column["status"], Decimal("0")) + value
        rows.append(
            {
                "item": pk,
                "code": names[pk][0],
                "name": names[pk][1],
                "position": order.get(pk),
                "outside_factory": out,
                "values": values,
                "total": row_total,
            }
        )
    outside_total = sum(outside.values(), Decimal("0"))
    return {
        "columns": columns,
        "rows": rows,
        "totals": {
            "outside_factory": outside_total,
            "columns": column_totals,
            "statuses": status_totals,
            "grand_total": sum((r["total"] for r in rows), Decimal("0")),
        },
        "active_items": sum(1 for r in rows if r["total"] > 0),
        # The columns name vendors as EXIM did; the filter takes a code. Each
        # vendor here as the lots carry it, so a name picked is a code sent.
        "vendors": [
            {"code": code, "name": name or code}
            for code, name in lots.exclude(status=LotStatus.IN_TANK)
            .values_list("vendor_code", "vendor_name")
            .distinct()
            .order_by("vendor_name", "vendor_code")
        ],
    }


@transaction.atomic
def reorder_dashboard(company, item_ids: list) -> None:
    """Put the dashboard's oils in this order, the rest after them."""
    known = set(TankItem.objects.filter(company=company, pk__in=item_ids).values_list("pk", flat=True))
    StockDashboardRow.objects.filter(company=company).delete()
    StockDashboardRow.objects.bulk_create(
        [
            StockDashboardRow(company=company, item_id=pk, position=position)
            for position, pk in enumerate(item_ids, start=1)
            if pk in known
        ]
    )


def vehicle_report(company, status) -> list:
    """Lots in a status, truck by truck. EXIM's ``vehicle-report``.

    On one truck, the lots of one oil from one vendor are one line, as EXIM
    summed them. Lots with no vehicle (contracts, mostly) are never summed:
    EXIM's GROUP BY merged two contracts of one oil and vendor into one line
    and showed only the later end date, hiding the one ending first. Each line
    names its lots, so it can link to them."""
    lots_in = (
        active_lots(company)
        .filter(status=status)
        .select_related("item")
        .order_by("vehicle_number", "transporter", "item__code", "vendor_code", "id")
    )
    trucks, lines = {}, {}
    for lot in lots_in:
        vehicle = (lot.vehicle_number or "").strip()
        truck = trucks.setdefault(
            (vehicle, lot.transporter),
            {"vehicle_number": lot.vehicle_number, "transporter": lot.transporter, "items": []},
        )
        key = (vehicle, lot.transporter, lot.item_id, lot.vendor_code) if vehicle else ("lot", lot.pk)
        line = lines.get(key)
        if line is None:
            line = lines[key] = {
                "lots": [],
                "item_code": lot.item.code,
                "item_name": lot.item.name,
                "vendor_code": lot.vendor_code,
                "vendor_name": lot.vendor_name,
                "litres": Decimal("0"),
                "kg": Decimal("0"),
                "eta": None,
                "arrival_date": None,
                "job_work": "",
                "rate": None,
                "payment_status": "",
                "contract_end": None,
            }
            truck["items"].append(line)
        line["lots"].append(lot.pk)
        line["litres"] += lot.quantity_litres or 0
        line["kg"] += lot.quantity or 0
        # EXIM's MAX() per field, kept for the lines it summed.
        for name in ("eta", "arrival_date", "job_work", "rate", "payment_status", "contract_end"):
            value = getattr(lot, name)
            if value not in (None, "") and (line[name] in (None, "") or value > line[name]):
                line[name] = value
    for truck in trucks.values():
        for line in truck["items"]:
            # EXIM's tonnes: litres back to kilograms, then to tonnes.
            line["mt"] = (line["litres"] / DENSITY / 1000).quantize(_MILL)
    return list(trucks.values())


def shortage_insights(company) -> dict:
    agg = LotShortage.objects.filter(company=company).aggregate(
        deducted_mt=Sum("deducted_mt"), amount=Sum("deduction_amount"), count=Count("id"),
    )
    return {
        "count": agg["count"],
        "deducted_mt": agg["deducted_mt"] or Decimal("0"),
        "deduction_amount": agg["amount"] or Decimal("0"),
    }


def lot_filters(lots, *, statuses=None, vendors=None, items=None):
    if statuses:
        lots = lots.filter(status__in=statuses)
    else:
        # EXIM's list never showed a completed lot; asked for by name, it does.
        lots = lots.exclude(status=LotStatus.COMPLETED)
    if vendors:
        lots = lots.filter(vendor_code__in=vendors)
    if items:
        lots = lots.filter(item_id__in=items)
    return lots


#: The statuses the director inventory reports, in EXIM's order.
DIRECTOR_STATUSES = (
    LotStatus.ON_THE_WAY,
    LotStatus.UNDER_LOADING,
    LotStatus.AT_REFINERY,
    LotStatus.MUNDRA_PORT,
    LotStatus.ON_THE_SEA,
    LotStatus.IN_CONTRACT,
    LotStatus.OUT_SIDE_FACTORY,
)


def director_inventory(company) -> dict:
    """Oil at every stage, in litres and tonnes, against the oil already packed.
    EXIM's ``director-inventorty``. SAP answers for the packed oil; if it cannot,
    that part says so and the rest still stands."""
    from sap_client.exceptions import SAPConnectionError, SAPDataError

    from .hana_reader import finished_litres

    tank_l = Tank.objects.filter(company=company, is_active=True).aggregate(t=Sum("level_l"))["t"] or Decimal("0")
    lots = active_lots(company)
    stages = {}
    for status in DIRECTOR_STATUSES:
        agg = lots.filter(status=status).aggregate(litres=Sum("quantity_litres"), kg=Sum("quantity"))
        stages[status] = {
            "label": LotStatus(status).label,
            "litres": agg["litres"] or Decimal("0"),
            "mt": ((agg["kg"] or Decimal("0")) / 1000).quantize(_MILL),
        }
    in_tank = {"litres": tank_l, "mt": (tank_l / DENSITY / 1000).quantize(_MILL)}
    outside = stages[LotStatus.OUT_SIDE_FACTORY]

    finished = None
    finished_reason = None
    try:
        by_warehouse = finished_litres(company.code)
        # EXIM's tonnes of packed oil: litres over 1,098.9 (1,000 kg at the density).
        per_mt = DENSITY * 1000
        finished = {
            "total": {
                "litres": sum(by_warehouse.values(), Decimal("0")),
                "mt": (sum(by_warehouse.values(), Decimal("0")) / per_mt).quantize(_MILL),
            },
            "warehouses": [
                {"warehouse": wh, "litres": litres, "mt": (litres / per_mt).quantize(_MILL)}
                for wh, litres in by_warehouse.items()
            ],
        }
    except (SAPConnectionError, SAPDataError) as exc:
        finished_reason = str(exc)

    return {
        "at_factory": {
            "litres": tank_l + outside["litres"],
            "mt": in_tank["mt"] + outside["mt"],
            "in_tank": in_tank,
            "outside_factory": {"litres": outside["litres"], "mt": outside["mt"]},
        },
        "stages": [
            {"status": status, **values}
            for status, values in stages.items()
            if status != LotStatus.OUT_SIDE_FACTORY
        ],
        "finished": finished,
        "finished_reason": finished_reason,
    }
