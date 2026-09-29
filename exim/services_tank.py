"""The rules of the tank farm. Views and the EXIM copy come through here.

LEVELS ARE ENTERED, NOT DERIVED. The store dips a tank and types the level in,
as it did in EXIM; the rules below only keep a level honest (never above the
tank's capacity, never an oil without a level or a level without an oil).

WHICH LOTS ARE IN THE TANKS: ``settle_in_tank_lots``
The average cost of an oil in the farm is worked out by lining its IN_TANK lots
up oldest first and taking from each until the litres in its tanks are
accounted for. Lots the tanks no longer have room for are marked COMPLETED.
That is EXIM's rule exactly, including its direction (the tanks are taken to
hold the OLDEST lots). What changed is when it runs: EXIM marked lots
COMPLETED as a side effect of somebody opening the tank monitor (a GET that
wrote), here it runs when a tank's dip changes - its level or its oil - and
opening the average only reads. Not when a lot goes into the tanks: the lot
arrives before the store has dipped the tank to include it, and settling then
would mark the newest lot completed on the spot.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.db.models.deletion import ProtectedError

from .models_lot import LotStatus, OilLot
from .models_tank import Tank, TankItem, TankKind
from .services_licence import EximError

#: Litres in a kilogram of oil. EXIM's figure, used everywhere it converts.
DENSITY = Decimal("1.0989")
_CENT = Decimal("0.01")

_CODE_PREFIX = {TankKind.TANK: ("TNK", 4), TankKind.TOTE: ("TOT", 3)}


def _q2(value) -> Decimal:
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Oils
# ---------------------------------------------------------------------------

OIL_FIELDS = ("code", "name", "category", "color", "is_active")


class SapUnavailable(EximError):
    status_code = 503


def sap_oil(company, code: str) -> dict:
    """SAP's raw-material oil with this code: an oil here is one of them, its
    code and name SAP's. Raises EximError when SAP has no such oil, or cannot
    be asked - an oil is never taken on trust."""
    from sap_client.exceptions import SAPConnectionError, SAPDataError

    from .hana_reader import raw_material_oil

    try:
        found = raw_material_oil(company.code, code)
    except (SAPConnectionError, SAPDataError) as exc:
        raise SapUnavailable(
            "SAP is not answering, so the oil cannot be checked against its raw materials. Try again shortly.",
            "sap_unavailable",
            {},
        ) from exc
    if found is None:
        raise EximError(
            f"SAP has no raw-material oil {code} (an RM item whose unit is OIL).", "not_a_sap_oil", {"code": code}
        )
    return found


def _free_code(company, code, oil=None) -> None:
    clash = TankItem.objects.filter(company=company, code__iexact=code)
    if oil is not None:
        clash = clash.exclude(pk=oil.pk)
    if clash.exists():
        raise EximError(f"{code} is already an oil here.", "oil_exists", {"code": code})


def create_oil(*, company, user, **data) -> TankItem:
    """Add one of SAP's raw-material oils. Its name is SAP's, whatever was sent."""
    code = data["code"].strip()
    _free_code(company, code)
    found = sap_oil(company, code)
    oil = TankItem(company=company, created_by=user)
    for name in OIL_FIELDS:
        if name in data:
            setattr(oil, name, data[name])
    oil.code, oil.name = found["code"], found["name"]
    oil.save()
    return oil


def update_oil(oil: TankItem, **data) -> TankItem:
    """Change an oil. Moving it to another SAP raw material takes that item's
    code and name; tanks and lots point at the oil, not at its code, so nothing
    else has to follow. Its colour, category and in-use flag change freely."""
    data.pop("name", None)
    if "code" in data:
        code = data["code"].strip()
        if code.upper() != oil.code.upper():
            _free_code(oil.company, code, oil)
            found = sap_oil(oil.company, code)
            data["code"], data["name"] = found["code"], found["name"]
        else:
            del data["code"]
    for name in OIL_FIELDS:
        if name in data:
            setattr(oil, name, data[name])
    oil.save()
    return oil


def delete_oil(oil: TankItem) -> None:
    try:
        oil.delete()
    except ProtectedError as exc:
        raise EximError(
            f"{oil.name} is still in a tank or on a lot, so it cannot be removed. Mark it inactive instead.",
            "oil_in_use",
            {},
        ) from exc


# ---------------------------------------------------------------------------
# Tanks
# ---------------------------------------------------------------------------

def next_tank_code(company, kind) -> str:
    """The lowest free number for the kind: TNK0001, TNK0002 ... / TOT001 ..."""
    prefix, pad = _CODE_PREFIX[kind]
    taken = set()
    for code in Tank.objects.filter(company=company, code__startswith=prefix).values_list("code", flat=True):
        try:
            taken.add(int(code[len(prefix):]))
        except ValueError:
            continue
    number = 1
    while number in taken:
        number += 1
    return f"{prefix}{number:0{pad}d}"


def _check_level(capacity, level, item) -> None:
    """EXIM's rules for a tank's contents, each with its own message."""
    level = Decimal(level or 0)
    if level < 0:
        raise EximError("A level cannot be below nothing.", "level_negative", {})
    if capacity is not None and level > Decimal(capacity):
        raise EximError(
            f"The level cannot be more than the tank holds ({capacity} L).",
            "level_over_capacity",
            {"capacity_l": str(capacity), "level_l": str(level)},
        )
    if item is not None and level == 0:
        raise EximError("A tank with an oil in it needs a level above nothing.", "level_required", {})
    if item is None and level > 0:
        raise EximError("Say which oil is in the tank.", "oil_required", {})


@transaction.atomic
def create_tank(*, company, user, kind, capacity_l, item=None, level_l=0, is_active=True) -> Tank:
    _check_level(capacity_l, level_l, item)
    # Two creates at once can pick the same number; the unique constraint
    # refuses the second, and it simply takes the next.
    for _ in range(5):
        try:
            with transaction.atomic():
                tank = Tank.objects.create(
                    company=company,
                    code=next_tank_code(company, kind),
                    kind=kind,
                    capacity_l=capacity_l,
                    item=item,
                    level_l=level_l or 0,
                    is_active=is_active,
                    updated_by=user,
                )
            break
        except IntegrityError:
            continue
    else:
        raise EximError("Could not number the new tank; try again.", "tank_code_busy", {})
    if item is not None:
        settle_in_tank_lots(company, [item.pk])
    return tank


_UNSET = object()


@transaction.atomic
def update_tank(tank: Tank, *, user, level_l=_UNSET, item=_UNSET, capacity_l=_UNSET,
                is_active=_UNSET) -> Tank:
    """Record a dip (level and oil), or correct the tank's capacity."""
    before = tank.item_id
    new_level = tank.level_l if level_l is _UNSET else (level_l or 0)
    new_item = tank.item if item is _UNSET else item
    new_capacity = tank.capacity_l if capacity_l is _UNSET else capacity_l
    _check_level(new_capacity, new_level, new_item)
    tank.level_l, tank.item, tank.capacity_l = new_level, new_item, new_capacity
    if is_active is not _UNSET:
        tank.is_active = is_active
    tank.updated_by = user
    tank.save()
    settle_in_tank_lots(tank.company, [pk for pk in {before, tank.item_id} if pk])
    return tank


def empty_tank(tank: Tank, *, user) -> Tank:
    return update_tank(tank, user=user, level_l=Decimal("0"), item=None)


def delete_tank(tank: Tank) -> None:
    item = tank.item_id
    company = tank.company
    tank.delete()
    if item:
        settle_in_tank_lots(company, [item])


# ---------------------------------------------------------------------------
# Read-outs
# ---------------------------------------------------------------------------

def tank_summary(company) -> dict:
    """The active farm in one line. EXIM's ``tank-summary``, except that its
    headline is the tanks alone: a tote is an IBC container, not part of how
    full the farm is. The Admin board's tank tile draws the same line, so the
    two agree; the totes are reported beside it."""
    active = Tank.objects.filter(company=company, is_active=True)
    agg = active.filter(kind=TankKind.TANK).aggregate(
        capacity=Sum("capacity_l"),
        level=Sum("level_l"),
        tanks=Count("id"),
    )
    totes = active.filter(kind=TankKind.TOTE).aggregate(
        capacity=Sum("capacity_l"), level=Sum("level_l"), count=Count("id"),
    )
    capacity = agg["capacity"] or Decimal("0")
    level = agg["level"] or Decimal("0")
    return {
        "capacity_l": capacity,
        "level_l": level,
        "used_pct": _q2(level / capacity * 100) if capacity > 0 else Decimal("0"),
        "tank_count": agg["tanks"],
        # Oils in the tanks or the totes: an oil held only in a tote is still held.
        "oil_count": active.exclude(item__isnull=True).values("item").distinct().count(),
        "totes": {
            "capacity_l": totes["capacity"] or Decimal("0"),
            "level_l": totes["level"] or Decimal("0"),
            "count": totes["count"],
        },
    }


def oil_summary(company) -> dict:
    """Litres of each oil across the active farm, with the tanks holding it."""
    tanks = (
        Tank.objects.filter(company=company, is_active=True, item__isnull=False)
        .select_related("item")
        .order_by("item__code", "code")
    )
    rows = {}
    for tank in tanks:
        row = rows.setdefault(
            tank.item_id,
            {
                "item": tank.item_id,
                "code": tank.item.code,
                "name": tank.item.name,
                "color": tank.item.color,
                "level_l": Decimal("0"),
                "capacity_l": Decimal("0"),
                "tanks": [],
            },
        )
        row["level_l"] += tank.level_l or 0
        row["capacity_l"] += tank.capacity_l or 0
        row["tanks"].append(tank.code)
    items = list(rows.values())
    for row in items:
        row["tank_count"] = len(row["tanks"])
    return {"total_l": sum((r["level_l"] for r in items), Decimal("0")), "items": items}


@dataclass
class _Allocation:
    lots: list = field(default_factory=list)
    #: The lots the tanks have no room left for.
    beyond: list = field(default_factory=list)
    tank_l: Decimal = Decimal("0")
    matched_l: Decimal = Decimal("0")


def _allocate(company, item_id) -> _Allocation:
    """Line the oil's IN_TANK lots up oldest first against the litres in its tanks."""
    tank_l = Tank.objects.filter(company=company, item_id=item_id).aggregate(t=Sum("level_l"))["t"]
    tank_l = Decimal(tank_l or 0)
    lots = (
        OilLot.objects.filter(company=company, item_id=item_id, deleted=False, status=LotStatus.IN_TANK)
        .order_by("created_at", "id")
    )
    result = _Allocation(tank_l=tank_l)
    remaining = tank_l
    for lot in lots:
        if remaining <= 0:
            result.beyond.append(lot)
            continue
        consumed = min(lot.quantity_litres or Decimal("0"), remaining)
        result.lots.append((lot, consumed))
        remaining -= consumed
    result.matched_l = tank_l - max(remaining, Decimal("0"))
    return result


def average_cost(company, item: TankItem) -> dict:
    """What the litres of an oil in the farm cost, lot by lot. EXIM's
    ``item-wise-average``, without the write EXIM hid inside it.

    Per litre it is EXIM's own arithmetic. The kilogram figures differ in one
    place: EXIM turned the tanks' litres into kilograms by MULTIPLYING by the
    density (1 L = 1.0989 kg), where a kilogram is 1.0989 litres; here the
    litres are divided, as every other conversion in the module does. The
    averages EXIM's screens showed were already right; its tonnes were not.
    """
    alloc = _allocate(company, item.pk)
    tank_l = alloc.tank_l
    tank_kg = tank_l / DENSITY
    weighted = Decimal("0")
    weighted_kg = Decimal("0")
    breakdown = []
    for lot, consumed in alloc.lots:
        rate_l = lot.rate_per_litre or Decimal("0")
        rate_kg = lot.rate or Decimal("0")
        consumed_kg = consumed / DENSITY
        weighted += rate_l * consumed
        weighted_kg += rate_kg * consumed_kg
        breakdown.append(
            {
                "lot": lot.pk,
                "created_at": lot.created_at,
                "party": lot.vendor_name or lot.vendor_code,
                "vehicle": lot.vehicle_number,
                "transporter": lot.transporter,
                "rate_per_litre": rate_l,
                "rate_per_kg": rate_kg,
                "lot_litres": lot.quantity_litres,
                "lot_kg": _q2((lot.quantity_litres or 0) / DENSITY),
                "litres_in_tank": _q2(consumed),
                "kg_in_tank": _q2(consumed_kg),
                "value": _q2(consumed * rate_l),
            }
        )
    matched = alloc.matched_l
    unmatched = tank_l - matched
    result = {
        "item": item.pk,
        "code": item.code,
        "name": item.name,
        "tank_l": _q2(tank_l),
        "tank_kg": _q2(tank_kg),
        "matched_l": _q2(matched),
        "matched_kg": _q2(matched / DENSITY),
        "unmatched_l": _q2(unmatched),
        # Over every litre in the tanks, lots or no lots (EXIM's "IN_TANK").
        "average_per_litre": _q2(weighted / tank_l) if tank_l > 0 else Decimal("0"),
        "average_per_kg": _q2(weighted_kg / tank_kg) if tank_kg > 0 else Decimal("0"),
        # Over the litres a lot accounts for (EXIM's "STO"): what the screens show.
        "matched_average_per_litre": _q2(weighted / matched) if matched > 0 else Decimal("0"),
        "matched_average_per_kg": _q2(weighted_kg / (matched / DENSITY)) if matched > 0 else Decimal("0"),
        "lots": breakdown,
        "warning": None,
    }
    if tank_l > 0 and matched == 0:
        result["warning"] = "The tanks hold this oil but no lot is in the tanks for it, so there is no cost to average."
    elif unmatched > 0:
        result["warning"] = (
            f"{_q2(unmatched)} L in the tanks is not accounted for by any lot, so the average covers the rest."
        )
    return result


def settle_in_tank_lots(company, item_ids: Optional[list] = None) -> int:
    """Mark COMPLETED the IN_TANK lots the tanks no longer have room for.

    Returns how many were marked. Runs for the given oils, or every oil in the
    company, but only for an oil an active tank holds. See the module docstring
    for the rule and its direction.
    """
    if item_ids is None:
        item_ids = list(
            OilLot.objects.filter(company=company, deleted=False, status=LotStatus.IN_TANK)
            .values_list("item_id", flat=True)
            .distinct()
        )
    # EXIM only ever settled the oils its tank monitor lists - those an active
    # tank holds - so an oil whose last tank was emptied keeps its lots.
    held = set(
        Tank.objects.filter(company=company, is_active=True, item_id__in=item_ids)
        .values_list("item_id", flat=True)
    )
    marked = 0
    for item_id in item_ids:
        if item_id not in held:
            continue
        beyond = _allocate(company, item_id).beyond
        if beyond:
            marked += OilLot.objects.filter(pk__in=[lot.pk for lot in beyond]).update(
                status=LotStatus.COMPLETED
            )
    return marked


def in_tank_oils(company) -> list:
    """The oils the active farm holds now."""
    return list(
        TankItem.objects.filter(company=company, tanks__is_active=True, tanks__level_l__gt=0)
        .distinct()
        .order_by("code")
    )


def capacity_insights(company) -> dict:
    """EXIM's ``capacity-insights``: every tank, active or not."""
    agg = Tank.objects.filter(company=company).aggregate(capacity=Sum("capacity_l"), level=Sum("level_l"))
    capacity = agg["capacity"] or Decimal("0")
    level = agg["level"] or Decimal("0")
    if capacity == 0:
        return {"capacity_l": 0, "filled_l": 0, "filled_pct": 0, "empty_l": 0, "empty_pct": 0}
    empty = capacity - level
    return {
        "capacity_l": capacity,
        "filled_l": level,
        "filled_pct": _q2(level / capacity * 100),
        "empty_l": empty,
        "empty_pct": _q2(empty / capacity * 100),
    }
