"""How much can actually be produced from the stock on hand.

The question is "what can the floor run tomorrow", and it has two honest answers
that must not be mixed up.

**Per SKU, standalone.** How many of ONE product could be built if the whole
warehouse were given to it: the scarcest component decides, so

    buildable(sku) = min over components of ( available(c) / qty_per_unit(c, sku) )

These numbers are **mutually exclusive**. The same 100,000 caps appear in the
figure for every SKU that uses them, so summing a column of standalone maxima
gives a total the warehouse cannot possibly support. That is why nothing here
totals them, and why every such row says what it is limited by.

**The planned mix, together.** Whether tomorrow's actual planned quantities can
all be made at once. That one IS additive and needs no allocation guesswork:
add up what the day's plan consumes per component and compare it to stock. A
component short here blocks every SKU that draws on it, and those SKUs are named.

**A typed-in run, allocated.** The same question asked the other way round: an
operator names the finished goods and the quantities they want to make, and the
answer is yes/no plus, when no, how much of it can be made. That answer is
additive like the mix -- the quantities come out of one shared pool of stock, so
they describe a run that could really happen all at once -- and it is the only
per-SKU column on any of these screens that may legitimately be totalled.

Both plan answers are reported, labelled, and never added together.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from django.utils import timezone

from ..hana_reader import BOM_LINE_TYPE_ITEM, classify_material
from . import calendar as cal
from . import warehouse_scope as scope
from .errors import PlanNotFound

logger = logging.getLogger(__name__)

ZERO = Decimal(0)

# Which stock the buildable figure is based on.
#
# ON_HAND is the default and answers the question the floor actually asks: the
# material is physically in the building, so it can physically be run. FREE nets
# off what SAP has reserved against other documents.
#
# The distinction is not academic here. On this company most components are
# *over-committed* — 41,945 HDPE bottles on hand against 54,230 committed, and
# 20,618 litres of canola oil against 177,563 — because open production and sales
# orders reserve against them. On a FREE basis nearly every SKU reads as blocked
# and the answer becomes "you can make nothing", which is both useless and untrue
# of a factory that shipped a million pieces this month. Both numbers are
# reported on every row either way, so an over-commitment stays visible rather
# than being quietly chosen for the operator.
BASIS_ON_HAND = "ON_HAND"
BASIS_FREE = "FREE"
STOCK_BASES = (BASIS_ON_HAND, BASIS_FREE)

# Wastage is not usable stock. `BH-WST` holds scrap and rejected material, and
# counting its 28,540 bottles towards what can be filled tomorrow would promise
# production material nobody intends to run.
EXCLUDED_WAREHOUSES = frozenset({"BH-WST"})

# How a typed-in run that stock cannot cover in full is cut back to something
# that can actually be made.
#
# PRIORITY fills the lines in the order they were entered: the first line gets
# everything it needs before the second gets a look. That is how a shift is
# really run -- whole batches, in a sequence somebody chose -- so it is the
# default, and it makes the order of the rows meaningful rather than cosmetic.
#
# FAIR_SHARE scales every line by one common factor instead, so no product is
# starved to fill another. It then makes a second pass in the entered order to
# spend what the scaling left behind: a line whose components nothing else
# competes for would otherwise be held back by a shortage it does not share, and
# material left idle in the answer reads as a defect rather than as fairness.
ALLOC_PRIORITY = "PRIORITY"
ALLOC_FAIR_SHARE = "FAIR_SHARE"
ALLOCATIONS = (ALLOC_PRIORITY, ALLOC_FAIR_SHARE)

# A request is one shift's worth of products, not a catalogue. Each line costs a
# BOM explosion and a stock read, so the ceiling is here to stop one pasted
# spreadsheet holding the SAP connection for a minute.
MAX_REQUEST_LINES = 50


def _dec(value) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def next_working_day(from_day: Optional[date] = None) -> date:
    """The next day the factory actually runs.

    "Tomorrow" is not tomorrow's date when tomorrow is a Sunday — a Saturday
    answer about Sunday production is useless. Honours the same non-working-day
    configuration as the plan's day buckets, and gives up after a fortnight
    rather than looping if every weekday is somehow configured off.
    """
    cursor = (from_day or timezone.localdate()) + timedelta(days=1)
    for _ in range(14):
        if cal.is_working_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


class ProducibleMixin:
    """Buildable-from-stock analysis. Mixed into `PlanService`."""

    def get_producible(
        self,
        abs_id: int,
        target_date: Optional[date] = None,
        warehouses: Optional[Sequence[str]] = None,
        spread_policy: str = cal.POLICY_EVEN_WORKING_DAYS,
        stock_basis: str = BASIS_ON_HAND,
    ) -> Dict[str, Any]:
        header_row = self.reader.get_plan_header(abs_id)
        if not header_row:
            raise PlanNotFound(f"Plan {abs_id} was not found in SAP.")

        header = self._map_plan_header(header_row)
        target = target_date or next_working_day()

        line_rows = self.reader.get_plan_lines(abs_id)
        lines = [self._map_plan_line(row) for row in line_rows]
        target_plan = self._planned_on(lines, header, target, spread_policy)

        bom_rows = self.reader.get_bom_components([l["item_code"] for l in lines])
        recipes, unusable = self._recipes(bom_rows)

        component_codes = sorted({c for r in recipes.values() for c in r})
        material_types = {
            code: detail.get("material_type", scope.OTHER)
            for code, detail in self._component_details.items()
        }
        stock = self._component_stock(
            component_codes, warehouses, stock_basis, material_types
        )

        skus = self._standalone_buildable(lines, recipes, stock, target_plan)
        components = self._mix_feasibility(lines, recipes, stock, target_plan)
        self._name_blockers(skus, components, recipes)

        return {
            "plan": header,
            "target_date": target,
            "skus": skus,
            "components": components,
            "meta": self._meta(
                skus, components, unusable, warehouses, target, stock_basis
            ),
        }

    # ------------------------------------------------------------------
    # A run somebody types in, rather than one the plan implies
    # ------------------------------------------------------------------

    def simulate_producible(
        self,
        requests: Sequence[Dict[str, Any]],
        warehouses: Optional[Sequence[str]] = None,
        stock_basis: str = BASIS_ON_HAND,
        allocation: str = ALLOC_PRIORITY,
    ) -> Dict[str, Any]:
        """Can this typed-in run be made from stock, and if not, how much of it?

        `requests` is `[{"item_code": ..., "quantity": ...}]` **in priority
        order**, quantities in the item's own inventory unit -- PCS means single
        bottles, never cases, which is the trap that turns a 2,000-case ask into
        a 2,000-bottle answer.

        Everything the plan-driven screen reports is reported here too, against
        the typed quantities instead of the plan's: the standalone maximum per
        product, and the additive component table naming what blocks the run.
        On top of that comes the part that only makes sense for a request --
        `achievable_qty` per line, the largest whole quantity of each product
        the one shared pool of stock supports alongside the other lines.

        There is no target date. The plan screen has one because a plan is
        phased across a month; a request is a question about the stock in the
        building right now, and dating it would imply arrivals this does not
        look at.
        """
        merged, merged_codes = self._merge_requests(requests)
        codes = [row["item_code"] for row in merged]

        master = {row["ItemCode"]: row for row in self.reader.get_items(codes)}
        # A code SAP has never heard of is named back, not analysed. Silently
        # dropping it would report a run as fully achievable while one of its
        # products was never checked at all.
        unknown = [code for code in codes if code not in master]

        lines = [
            self._map_plan_line({
                **master[row["item_code"]],
                "LineID": index,
                "BucketDate": None,
                "WhsCode": "",
                "PlannedQty": row["quantity"],
            })
            for index, row in enumerate(
                entry for entry in merged if entry["item_code"] in master
            )
        ]
        requested = {line["item_code"]: line["planned_qty"] for line in lines}

        bom_rows = self.reader.get_bom_components(list(requested))
        recipes, unusable = self._recipes(bom_rows)

        component_codes = sorted({c for r in recipes.values() for c in r})
        material_types = {
            code: detail.get("material_type", scope.OTHER)
            for code, detail in self._component_details.items()
        }
        stock = self._component_stock(
            component_codes, warehouses, stock_basis, material_types
        )

        items = self._standalone_buildable(lines, recipes, stock, requested)
        components = self._mix_feasibility(lines, recipes, stock, requested)
        self._name_blockers(items, components, recipes)

        # `_standalone_buildable` sorts for the plan screen, where the operator
        # chose no order. Here they did: the order they typed IS the priority
        # the allocation honours, so putting the rows back into it keeps the
        # table agreeing with the split beside it.
        position = {code: index for index, code in enumerate(requested)}
        items.sort(key=lambda row: position[row["item_code"]])

        self._allocate_request(items, recipes, stock, components, allocation)

        return {
            "items": items,
            "components": components,
            "meta": self._request_meta(
                items, components, unusable, warehouses, stock_basis,
                allocation, unknown, merged_codes,
            ),
        }

    @staticmethod
    def _merge_requests(requests: Sequence[Dict[str, Any]]) -> tuple:
        """One line per item code, keeping the position it was first asked in.

        The same product typed twice is one product to make, and leaving it as
        two lines would have the allocation compete a SKU against itself: the
        first copy would eat the stock and the second would report a shortage of
        material it had just consumed. Codes that were merged come back so the
        response can say so rather than appearing to have lost a row.
        """
        order: List[str] = []
        totals: Dict[str, Decimal] = {}
        merged: List[str] = []

        for entry in requests:
            code = (entry.get("item_code") or "").strip()
            if not code:
                continue
            quantity = _dec(entry.get("quantity"))
            if code in totals:
                if code not in merged:
                    merged.append(code)
                totals[code] += quantity
            else:
                order.append(code)
                totals[code] = quantity

        return (
            [{"item_code": code, "quantity": totals[code]} for code in order],
            merged,
        )

    def _allocate_request(
        self, items, recipes, stock, components, policy: str
    ) -> None:
        """How much of the request can actually be run, line by line.

        Unlike the standalone maxima, this one column IS additive: every
        quantity is drawn from the same declining pool of stock, so the set of
        them describes a run that could all happen at once. That is exactly why
        it needs a policy -- when two products want the same oil, something has
        to decide which gets it -- and why the policy is named on the response
        rather than left an implementation detail.
        """
        remaining = {code: entry.get("available", ZERO) for code, entry in stock.items()}
        needed = {row["component_code"]: row["needed_qty"] for row in components}
        runnable = [row for row in items if row["has_bom"]]
        granted: Dict[str, Decimal] = {row["item_code"]: ZERO for row in runnable}

        # The common factor every line is scaled to on the first pass. One for
        # PRIORITY, which is what makes its first pass fill each line outright.
        factor = Decimal(1)
        if policy == ALLOC_FAIR_SHARE:
            for component, need in needed.items():
                if need > ZERO:
                    factor = min(factor, remaining.get(component, ZERO) / need)
            factor = max(ZERO, min(Decimal(1), factor))

        def fill(row, ceiling: Decimal) -> None:
            """Raise one line as far as `ceiling` and the stock left allow."""
            recipe = recipes[row["item_code"]]
            headroom = ceiling - granted[row["item_code"]]
            if headroom <= ZERO:
                return

            capacity = None
            for component, per_unit in recipe.items():
                supports = remaining.get(component, ZERO) / per_unit
                if capacity is None or supports < capacity:
                    capacity = supports

            # Whole units only. Half a bottle is not a thing the floor can make,
            # and rounding up would hand out material that is not there.
            take = min(headroom, capacity if capacity is not None else ZERO).quantize(
                Decimal("1"), rounding="ROUND_DOWN"
            )
            if take <= ZERO:
                return

            granted[row["item_code"]] += take
            for component, per_unit in recipe.items():
                remaining[component] = remaining.get(component, ZERO) - take * per_unit

        for row in runnable:
            fill(row, row["planned_qty"] * factor)
        # Second pass: spend what a fair share left idle. A no-op when the factor
        # was already one, which is every PRIORITY request.
        if factor < Decimal(1):
            for row in runnable:
                fill(row, row["planned_qty"])

        for row in items:
            if not row["has_bom"]:
                # No recipe means no answer, and emphatically not "runs in
                # full". Claiming a quantity for an item SAP holds no BOM for
                # would be the one failure here nobody could catch downstream.
                row.update({
                    "achievable_qty": None,
                    "achievable_litres": None,
                    "achievable_cases": None,
                    "achievable_pct": None,
                    "unmet_qty": None,
                    "runs_in_full": None,
                    "allocation_limited_by": None,
                    "allocation_limited_by_detail": None,
                })
                continue

            asked = row["planned_qty"]
            achieved = granted[row["item_code"]]
            unmet = max(ZERO, asked - achieved)

            limiter = None
            if unmet > ZERO:
                recipe = recipes[row["item_code"]]
                # What is stopping it *now*, after the split -- which is not
                # always what caps it standalone. A line can lose its oil to an
                # earlier line and end up blocked by a component it had plenty
                # of to itself.
                limiter = min(
                    recipe, key=lambda code: remaining.get(code, ZERO) / recipe[code]
                )

            row.update({
                "achievable_qty": achieved,
                "achievable_litres": self._litres(achieved, row["litres_per_unit"]),
                "achievable_cases": self._cases(achieved, row["pieces_per_case"]),
                "achievable_pct": (
                    (achieved / asked * 100).quantize(Decimal("0.1"))
                    if asked > ZERO else ZERO
                ),
                "unmet_qty": unmet,
                "runs_in_full": unmet <= ZERO,
                "allocation_limited_by": limiter,
                "allocation_limited_by_detail": self._limiter_detail(
                    limiter, row["item_code"], recipes, components, remaining
                ),
            })

    @staticmethod
    def _limiter_detail(
        limiter, item_code, recipes, components, remaining
    ) -> Optional[Dict[str, Any]]:
        """The component that stopped a line, with what is left of it.

        `remaining_qty` is the leftover *after* the whole split, which is the
        number that makes the answer actionable: 300 litres left against 5
        litres a piece explains a shortfall of 60 pieces without anybody having
        to do the division.
        """
        if not limiter:
            return None
        detail = next(
            (row for row in components if row["component_code"] == limiter), {}
        )
        per_unit = (recipes.get(item_code) or {}).get(limiter, ZERO)
        return {
            "component_code": limiter,
            "component_name": detail.get("component_name", ""),
            "material_type": detail.get("material_type", "OTHER"),
            "uom": detail.get("uom", ""),
            "available_qty": detail.get("available_qty", ZERO),
            "remaining_qty": max(ZERO, remaining.get(limiter, ZERO)),
            "qty_per_unit": per_unit,
        }

    def _request_meta(
        self, items, components, unusable, warehouses, stock_basis,
        allocation, unknown, merged_codes,
    ) -> Dict[str, Any]:
        answerable = [row for row in items if row["has_bom"]]
        short = [row for row in answerable if row["runs_in_full"] is False]
        blocking = [row for row in components if row["is_blocking"]]

        requested_litres = sum((row["planned_litres"] for row in items), ZERO)
        achievable_litres = sum(
            ((row["achievable_litres"] or ZERO) for row in answerable), ZERO
        )

        return {
            "company_code": self.company_code,
            "allocation": allocation,
            "stock_basis": stock_basis,
            "item_count": len(items),
            "answerable_item_count": len(answerable),
            "item_without_bom_count": len(items) - len(answerable),
            "runnable_item_count": len(answerable) - len(short),
            "short_item_count": len(short),
            "component_count": len(components),
            "blocking_component_count": len(blocking),
            # The verdict, and the caveat on it in the same breath. A request
            # holding an item SAP has no BOM for was not fully checked, and a
            # bare "yes" there would be the answer people trust most and should
            # trust least.
            "request_runs_in_full": not blocking and not short,
            "fully_checked": not unknown and len(answerable) == len(items),
            "requested_litres": requested_litres,
            "achievable_litres": achievable_litres,
            # Litres, not a count of lines: one blocked 5 L line matters more
            # than three blocked 200 ML ones, and the litre is the unit this
            # business reads a shortfall in.
            "achievable_pct": (
                (achievable_litres / requested_litres * 100).quantize(Decimal("0.1"))
                if requested_litres > ZERO else ZERO
            ),
            "unknown_item_codes": unknown,
            "merged_item_codes": merged_codes,
            "unusable_boms": unusable,
            "over_committed_component_count": sum(
                1 for row in components if row["over_committed"]
            ),
            "warehouse_scope": (
                {"ALL": list(warehouses)}
                if warehouses
                else scope.scope_by_material_type()
            ),
            "warehouse_filtered": bool(warehouses),
            "excluded_warehouses": sorted(EXCLUDED_WAREHOUSES),
            "fetched_at": timezone.now().isoformat(),
            "notes": [
                "Quantities are in each item's own SAP inventory unit. For "
                "almost every SKU here that is PCS -- single bottles or tins, "
                "not cases.",
                "This is stock as it stands now. Nothing arriving later is "
                "counted, and work in progress is not deducted.",
                (
                    "Lines are filled in the order they were entered: the first "
                    "gets what it needs before the next gets a look."
                    if allocation == ALLOC_PRIORITY else
                    "Every line is scaled by one common factor, then a second "
                    "pass in the entered order spends what that left idle."
                ),
                "Can-make per product assumes that product gets the whole "
                "warehouse, so those figures are alternatives to one another "
                "and are never totalled. The achievable column is the additive "
                "answer, and that one does add up.",
                "Wastage warehouses are never counted as usable stock.",
            ],
        }

    # ------------------------------------------------------------------

    def _planned_on(
        self, lines, header, target: date, spread_policy: str
    ) -> Dict[str, Decimal]:
        """Each SKU's planned pieces on the target day, from its DAY buckets."""
        period_start = header["start_date"]
        period_end = header["end_date"]
        planned: Dict[str, Decimal] = {}

        for line in lines:
            buckets = cal.build_buckets(
                line["planned_qty"],
                line["bucket_date"] or period_start,
                period_start,
                period_end,
                policy=spread_policy,
            )
            for bucket in buckets.get(cal.DAY, []):
                if bucket["bucket_start"] == target:
                    planned[line["item_code"]] = (
                        planned.get(line["item_code"], ZERO) + bucket["planned_qty"]
                    )
        return planned

    def _recipes(self, bom_rows) -> tuple:
        """`{parent: {component: qty_per_unit}}`, plus the BOM lines we cannot use.

        Resource lines are dropped: a filling cost does not limit how many bottles
        can be filled from stock, and treating it as a component would make every
        SKU look blocked by a cost centre.
        """
        recipes: Dict[str, Dict[str, Decimal]] = {}
        details: Dict[str, Dict[str, Any]] = {}
        unusable: List[Dict[str, Any]] = []

        for row in bom_rows:
            per_unit = row.get("QtyPerUnit")
            if per_unit is None:
                unusable.append({
                    "parent_code": row["ParentCode"],
                    "component_code": row["ComponentCode"],
                    "reason": "BOM base quantity is zero in SAP (OITT.Qauntity)",
                })
                continue
            if int(row.get("LineType") or BOM_LINE_TYPE_ITEM) != BOM_LINE_TYPE_ITEM:
                continue
            per_unit = _dec(per_unit)
            if per_unit <= ZERO:
                continue

            recipes.setdefault(row["ParentCode"], {})[row["ComponentCode"]] = per_unit
            details.setdefault(row["ComponentCode"], {
                "component_name": row.get("ComponentName") or "",
                "item_group": row.get("ItemGroup") or "",
                "material_type": classify_material(row.get("ItemGroup")),
                "uom": row.get("Uom") or "",
            })

        self._component_details = details
        return recipes, unusable

    def _component_stock(
        self,
        codes: Sequence[str],
        warehouses: Optional[Sequence[str]],
        basis: str = BASIS_ON_HAND,
        material_types: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Stock per component, on both bases, with wastage excluded.

        `available` is the figure the buildable maths uses and follows `basis`;
        `on_hand` and `committed` come back regardless so the row can show what
        was set aside even when it is not being deducted.
        """
        material_types = material_types or {}
        rows = self.reader.get_item_stock(
            codes, warehouses or scope.all_scoped_warehouses()
        )
        by_code: Dict[str, Dict[str, Any]] = {}

        for row in rows:
            warehouse = row["WhsCode"]
            # Explicit filter rather than trusting the caller: a request with no
            # warehouse filter must still not count scrap.
            if warehouse in EXCLUDED_WAREHOUSES:
                continue
            # Same per-material-type scope the requirement screen uses. Packaging
            # from the packaging stores, oil from the oil stores -- two screens
            # answering "how much of this do we have" with different numbers would
            # be a defect, not a feature.
            if not scope.counts(
                material_types.get(row["ItemCode"], scope.OTHER), warehouse, warehouses
            ):
                continue
            entry = by_code.setdefault(row["ItemCode"], {
                "on_hand": ZERO, "committed": ZERO, "warehouses": [],
            })
            on_hand = _dec(row["OnHand"])
            entry["on_hand"] += on_hand
            entry["committed"] += _dec(row["Committed"])
            if on_hand != ZERO:
                entry["warehouses"].append({
                    "warehouse": warehouse,
                    "on_hand": on_hand,
                    "committed": _dec(row["Committed"]),
                })

        for code in codes:
            entry = by_code.setdefault(code, {
                "on_hand": ZERO, "committed": ZERO, "warehouses": [],
            })
            entry["free"] = entry["on_hand"] - entry["committed"]
            entry["over_committed"] = entry["free"] < ZERO
            entry["available"] = max(
                ZERO,
                entry["free"] if basis == BASIS_FREE else entry["on_hand"],
            )
            entry["warehouses"].sort(key=lambda w: -w["on_hand"])
        return by_code

    def _standalone_buildable(self, lines, recipes, stock, target_plan) -> List[Dict]:
        """Max buildable per SKU if that SKU had the warehouse to itself."""
        out: List[Dict[str, Any]] = []

        for line in lines:
            code = line["item_code"]
            recipe = recipes.get(code)
            planned = target_plan.get(code, ZERO)

            row = {
                "item_code": code,
                "item_name": line["item_name"],
                "uom": line["uom"],
                "pieces_per_case": line["pieces_per_case"],
                "litres_per_unit": line["litres_per_unit"],
                "is_litre_item": line["is_litre_item"],
                "planned_qty": planned,
                "planned_litres": self._litres(planned, line["litres_per_unit"]),
                "planned_cases": self._cases(planned, line["pieces_per_case"]),
                "component_count": len(recipe or {}),
            }

            if not recipe:
                # No BOM means no answer, not an answer of zero. Saying "0" here
                # would read as "out of material" when the truth is "SAP has no
                # recipe for this", which is a different problem with a different
                # owner.
                row.update({
                    "has_bom": False,
                    "buildable_qty": None,
                    "buildable_litres": None,
                    "buildable_cases": None,
                    "limited_by": None,
                    "covers_plan": None,
                    "shortfall_qty": None,
                })
                out.append(row)
                continue

            buildable = None
            limiter = None
            for component, per_unit in recipe.items():
                available = stock.get(component, {}).get("available", ZERO)
                supports = (available / per_unit) if per_unit > ZERO else ZERO
                if buildable is None or supports < buildable:
                    buildable = supports
                    limiter = component

            buildable = (buildable or ZERO).quantize(Decimal("1"), rounding="ROUND_DOWN")
            row.update({
                "has_bom": True,
                "buildable_qty": buildable,
                "buildable_litres": self._litres(buildable, line["litres_per_unit"]),
                "buildable_cases": self._cases(buildable, line["pieces_per_case"]),
                "limited_by": limiter,
                "covers_plan": buildable >= planned if planned > ZERO else None,
                "shortfall_qty": max(ZERO, planned - buildable),
            })
            out.append(row)

        # Biggest opportunity first, but anything that cannot meet its own planned
        # quantity floats to the top: that is the row somebody has to act on.
        out.sort(key=lambda r: (
            r["covers_plan"] is not False,
            -(r["buildable_litres"] or ZERO),
        ))
        return out

    def _mix_feasibility(self, lines, recipes, stock, target_plan) -> List[Dict]:
        """Can the whole planned mix for the day be made? Additive, so exact."""
        litres_by_item = {l["item_code"]: l["litres_per_unit"] for l in lines}
        names = {l["item_code"]: l["item_name"] for l in lines}

        needed: Dict[str, Decimal] = {}
        drawn_by: Dict[str, List[Dict[str, Any]]] = {}

        for item_code, planned in target_plan.items():
            if planned <= ZERO:
                continue
            for component, per_unit in (recipes.get(item_code) or {}).items():
                need = planned * per_unit
                needed[component] = needed.get(component, ZERO) + need
                drawn_by.setdefault(component, []).append({
                    "item_code": item_code,
                    "item_name": names.get(item_code, ""),
                    "planned_qty": planned,
                    "planned_litres": self._litres(
                        planned, litres_by_item.get(item_code, ZERO)
                    ),
                    "qty_per_unit": per_unit,
                    "needed_qty": need,
                })

        rows: List[Dict[str, Any]] = []
        for component, need in needed.items():
            entry = stock.get(component, {})
            available = entry.get("available", ZERO)
            detail = self._component_details.get(component, {})
            shortage = max(ZERO, need - available)
            rows.append({
                "component_code": component,
                "component_name": detail.get("component_name", ""),
                "material_type": detail.get("material_type", "OTHER"),
                "uom": detail.get("uom", ""),
                "needed_qty": need,
                "on_hand_qty": entry.get("on_hand", ZERO),
                "committed_qty": entry.get("committed", ZERO),
                "free_qty": entry.get("free", ZERO),
                "over_committed": bool(entry.get("over_committed")),
                "available_qty": available,
                "shortage_qty": shortage,
                "is_blocking": shortage > ZERO,
                # How much of the day's plan this component alone would allow,
                # so "we can run 60% of tomorrow" has a number behind it.
                "coverage_pct": (
                    (available / need * 100).quantize(Decimal("0.1"))
                    if need > ZERO else ZERO
                ),
                "drawn_by": sorted(drawn_by.get(component, []),
                                   key=lambda d: -d["needed_qty"]),
                "warehouses": entry.get("warehouses", []),
            })

        rows.sort(key=lambda r: (not r["is_blocking"], r["coverage_pct"]))
        return rows

    @staticmethod
    def _name_blockers(skus, components, recipes) -> None:
        """Give each SKU row the name and numbers of the component limiting it."""
        by_code = {c["component_code"]: c for c in components}
        for row in skus:
            code = row.get("limited_by")
            if not code:
                continue
            component = by_code.get(code, {})
            per_unit = (recipes.get(row["item_code"]) or {}).get(code, ZERO)
            row["limited_by_detail"] = {
                "component_code": code,
                "component_name": component.get("component_name", ""),
                "material_type": component.get("material_type", "OTHER"),
                "uom": component.get("uom", ""),
                "available_qty": component.get("available_qty", ZERO),
                "qty_per_unit": per_unit,
            }

    def _meta(
        self, skus, components, unusable, warehouses, target, stock_basis
    ) -> Dict[str, Any]:
        with_bom = [s for s in skus if s["has_bom"]]
        planned_today = [s for s in with_bom if s["planned_qty"] > ZERO]
        blocked = [s for s in planned_today if s["covers_plan"] is False]
        blocking = [c for c in components if c["is_blocking"]]

        # How much of tomorrow is actually at risk, in litres.
        #
        # Deliberately NOT the worst component's coverage percentage. That reads
        # as "you can only make 8.5% of tomorrow" when in reality the 8.5%
        # component gates one small SKU and the other 93 run fine. Counting the
        # litres sitting on blocked SKUs is the honest measure of the damage, and
        # it needs no allocation guesswork.
        planned_litres = sum((s["planned_litres"] for s in skus), ZERO)
        at_risk_litres = sum((s["planned_litres"] for s in blocked), ZERO)
        worst_coverage = min(
            (c["coverage_pct"] for c in blocking), default=Decimal("100.0")
        )

        return {
            "company_code": self.company_code,
            "target_date": target,
            "sku_count": len(skus),
            "sku_without_bom_count": len(skus) - len(with_bom),
            "planned_sku_count": len(planned_today),
            "blocked_sku_count": len(blocked),
            "blocked_skus": [
                {
                    "item_code": s["item_code"],
                    "shortfall_qty": s["shortfall_qty"],
                    "limited_by": s["limited_by"],
                }
                for s in blocked
            ],
            "runnable_sku_count": len(planned_today) - len(blocked),
            "component_count": len(components),
            "blocking_component_count": len(blocking),
            "worst_component_coverage_pct": worst_coverage,
            "plan_runs_in_full": not blocking,
            "planned_litres": planned_litres,
            "at_risk_litres": at_risk_litres,
            "at_risk_pct": (
                (at_risk_litres / planned_litres * 100).quantize(Decimal("0.1"))
                if planned_litres > ZERO else ZERO
            ),
            "unusable_boms": unusable,
            "stock_basis": stock_basis,
            "over_committed_component_count": sum(
                1 for c in components if c["over_committed"]
            ),
            "warehouse_scope": (
                {"ALL": list(warehouses)}
                if warehouses
                else scope.scope_by_material_type()
            ),
            "warehouse_filtered": bool(warehouses),
            "excluded_warehouses": sorted(EXCLUDED_WAREHOUSES),
            "fetched_at": timezone.now().isoformat(),
            "notes": [
                "Stock is counted per material type: packaging from "
                f"{', '.join(scope.packaging_warehouses()) or 'all'}, raw material "
                f"from {', '.join(scope.raw_warehouses()) or 'all'}.",
                "Stock on hand is the default basis: the material is in the "
                "building, so it can be run. Switch to free stock to net off what "
                "SAP has reserved against other documents.",
                "Wastage warehouses are never counted as usable stock.",
                "Per-SKU buildable assumes that SKU gets the whole warehouse, so "
                "those figures are alternatives to each other and are never "
                "totalled — the same caps appear in several of them.",
                "The component table is the additive answer: it compares what the "
                "whole day's planned mix consumes against stock.",
                "Work in progress is not deducted, and nothing arriving tomorrow "
                "is added.",
            ],
        }
