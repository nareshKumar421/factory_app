"""The Live Trail — one chain from an open order to the purchase order it needs.

The brief asks for a single brain. The planning module answers *when* to order
and *whether the plan runs*; this answers the question underneath both, for the
whole order book at once and with nothing typed in by hand:

    order pending -> can it ship from stock -> is it already being made ->
    what does the rest consume -> what do we not have.

**One production unit, two order books.** JIVO makes oil in one place. Mart sells
a book of its own that the same factory has to fill, so demand is read from Oil
*and* Mart while stock, work orders, bills of materials, purchase orders and
lead times are read from Oil alone. Mart's stock counts as cover, because a Mart
order Mart can fill is not a call on the factory.

**Intercompany is netted out by default.** An Oil sales order to Mart and Mart's
own sales order for the same goods are the same litres counted twice. ``scope``
selects which book you are reading: EXTERNAL drops intercompany lines, ALL keeps
them. Both are computed from the same rows and every line carries its own
``interco`` flag, so the two readings can never disagree.

Every number here is derived from SAP. The only thing this module takes from
elsewhere is the reference template — machine capacities and the SKU-to-machine
map — which SAP does not hold and which turn the production gap into a real
feasibility check rather than a note about conversion cost.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from .live_trail_reader import LiveTrailReader

logger = logging.getLogger(__name__)

# The factory. Demand may come from several books; production only ever from
# this one, which is why component stock, work orders and POs are read here.
PRODUCTION_COMPANY = "JIVO_OIL"

# Whose orders the factory is ultimately filling.
DEMAND_COMPANIES = ("JIVO_OIL", "JIVO_MART")

# JIVO's own group companies, per book. Selling to ourselves is a real SAP order
# and a real movement of goods, but it is not new demand on the factory: the same
# litres appear again in the buying company's own order book. Card codes differ
# per company database, so this is keyed by book and not by a single global list.
INTERCOMPANY_CARD_CODES = {
    "JIVO_OIL": {
        "CUSTA000001", "CUSTA000002", "CUSTA000003", "CUSTA000004",
        "CUSTA000606", "CUSTA000827", "CUSTA000906", "CUSTA001099", "CUSTA001113",
    },
    "JIVO_MART": {
        "CUSTA000001", "CUSTA000827", "CUSTA000874", "CUSTA000875",
        "CUSTA000876", "CUSTA000877", "CUSTA000878", "CUSTA000926",
    },
    "JIVO_BEVERAGES": {
        "CUSTA000001", "CUSTA000002", "CUSTA000003", "CUSTA000004",
        "CUSTA000606", "CUSTA000827",
    },
}

COMPANY_LABELS = {
    "JIVO_OIL": "JIVO Oil",
    "JIVO_MART": "JIVO Mart",
    "JIVO_BEVERAGES": "JIVO Beverages",
}

SCOPE_EXTERNAL = "EXTERNAL"
SCOPE_ALL = "ALL"

# A purchase order only counts as supply if its date is still believable. The
# open PO book is heavily stale — hundreds of lines are over 180 days past their
# date — so counting every open PO as incoming supply would quietly cancel real
# shortages. A date in the future, or slipped by no more than this, is credible;
# anything older is shown separately as a dead PO and funds nothing.
CREDIBLE_PO_SLIP_DAYS = 30

# How many SKUs the cover chart draws before it stops being a chart.
COVER_CHART_ROWS = 18

NOTES = (
    "SAP OITT/ITT1 sub-BOMs on purchased items are read as an in-house make "
    "route. Make cost is SAP standard/last-purchase economics — preform price "
    "plus conversion rate — not a measured blowing-line cost."
)


def _round(value, places=2):
    return round(float(value or 0), places)


def _days_between(start, end):
    if not start or not end:
        return 0
    return (end - start).days


def _parse(value):
    """``'2026-08-10'`` -> ``date``. The reader hands dates back as strings."""
    if not value:
        return None
    return timezone.datetime.strptime(value, "%Y-%m-%d").date()


class _Sku:
    """One finished good, with its whole trail attached."""

    __slots__ = (
        "item", "name", "type", "variety", "uom", "lines", "demand", "value",
        "earliest_due", "latest_due", "onhand", "onhand_by_company", "wip",
        "wo_count", "has_bom", "to_produce", "components",
    )

    def __init__(self, item):
        self.item = item
        self.name = ""
        self.type = "-"
        self.variety = "-"
        self.uom = "PCS"
        self.lines = 0
        self.demand = 0.0
        self.value = 0.0
        self.earliest_due = None
        self.latest_due = None
        self.onhand = 0.0
        self.onhand_by_company = {}
        self.wip = 0.0
        self.wo_count = 0
        self.has_bom = False
        self.to_produce = 0.0
        self.components = []


def build_live_trail(company_code=PRODUCTION_COMPANY, *, scope=SCOPE_EXTERNAL, reader=None):
    """Read the whole trail and return it as one payload.

    ``reader`` is injectable so the assembly below — which is where every
    judgement lives — can be tested without a HANA box.
    """
    scope = (scope or SCOPE_EXTERNAL).upper()
    if scope not in (SCOPE_EXTERNAL, SCOPE_ALL):
        scope = SCOPE_EXTERNAL

    if reader is not None:
        return _assemble(reader, scope)
    with LiveTrailReader(PRODUCTION_COMPANY, DEMAND_COMPANIES) as live:
        return _assemble(live, scope)


def _assemble(reader, scope):
    today = timezone.localdate()

    # ── stage 1 · the order book ──────────────────────────────────────────────
    raw_lines = reader.open_order_lines()
    for line in raw_lines:
        line["interco"] = line["card"] in INTERCOMPANY_CARD_CODES.get(line["company"], set())
        line["value"] = _round(line["open"] * line["price"])
        ordered, due = _parse(line["ordered"]), _parse(line["due"])
        line["age"] = max(_days_between(ordered, today), 0)
        line["late"] = max(_days_between(due, today), 0)

    lines = [l for l in raw_lines if scope == SCOPE_ALL or not l["interco"]]
    unresolved = _resolve_to_production_items(reader, lines)
    plannable = [l for l in lines if l["item"]]

    # ── stage 2 · what can ship from the shelf ────────────────────────────────
    skus = {}
    for line in plannable:
        sku = skus.get(line["item"])
        if sku is None:
            sku = skus[line["item"]] = _Sku(line["item"])
            sku.name = line["name"]
        sku.lines += 1
        sku.demand += line["open"]
        sku.value += line["value"]
        due = _parse(line["due"])
        if due and (sku.earliest_due is None or due < sku.earliest_due):
            sku.earliest_due = due
        if due and (sku.latest_due is None or due > sku.latest_due):
            sku.latest_due = due

    sku_codes = sorted(skus)

    # Stock is read per book and then pooled. The factory does not care which
    # company's warehouse a bottle sits in — a Mart order Mart can fill never
    # reaches the line — but the split is kept so the pooling can be audited.
    for company in reader.demand_companies:
        for code, row in reader.stock_on_hand(company, sku_codes).items():
            sku = skus.get(code)
            if sku is None:
                continue
            sku.onhand += row["onhand"]
            sku.onhand_by_company[company] = _round(row["onhand"])

    # ── stage 3 · what the floor already has open ─────────────────────────────
    for code, row in reader.open_work_orders(sku_codes).items():
        sku = skus.get(code)
        if sku is None:
            continue
        sku.wip = row["wip"]
        sku.wo_count = row["wo_count"]

    for sku in skus.values():
        sku.to_produce = max(sku.demand - sku.onhand - sku.wip, 0.0)

    # ── stage 4 · explode the gap through the live BOM ────────────────────────
    gap_codes = [code for code in sku_codes if skus[code].to_produce > 0]
    boms = reader.bills_of_material(gap_codes)

    component_codes = set()
    for code, bom in boms.items():
        sku = skus[code]
        sku.has_bom = True
        for child in bom["lines"]:
            component_codes.add(child["child"])
            sku.components.append({
                "child": child["child"],
                "per_unit": _round(child["per_unit"], 6),
                "reqd": _round(child["per_unit"] * sku.to_produce),
                "bom_qty": child["bom_qty"],
                "bom_base": child["bom_base"],
                "is_resource": child["is_resource"],
            })

    # A SKU with demand and no BOM is bought or transferred finished, not made.
    for code in sku_codes:
        skus[code].has_bom = code in boms

    component_codes = sorted(component_codes)
    resources = reader.resources()
    items = reader.item_master(sorted(set(sku_codes) | set(component_codes)))

    for code, sku in skus.items():
        master = items.get(code)
        if master:
            sku.name = master["name"] or sku.name
            sku.type = master["type"]
            sku.variety = master["variety"]
            sku.uom = master["uom"] or "PCS"

    # ── stage 5 · net each component against stock and credible POs ───────────
    purchased_codes = [c for c in component_codes if c not in resources]
    purchase_lines = reader.open_purchase_lines(purchased_codes)
    lead_times = reader.measured_lead_times(purchased_codes)
    vendors = reader.last_vendors(purchased_codes)
    sub_boms = reader.sub_bills_of_material(
        [c for c in purchased_codes if items.get(c, {}).get("purchased")]
    )

    # A sub-BOM's own inputs — the preform behind the bottle — are not components
    # of the gap and so were never fetched. Without their master data the make
    # route prices at zero and every buy silently reads as the cheaper option.
    sub_inputs = sorted(
        {line["child"] for bom in sub_boms.values() for line in bom["lines"]}
        - set(items) - set(resources)
    )
    if sub_inputs:
        items.update(reader.item_master(sub_inputs))
    stock = reader.stock_on_hand(reader.production_company, purchased_codes + sub_inputs)

    components = {}
    for code in component_codes:
        master = items.get(code, {})
        resource = resources.get(code)
        is_resource = resource is not None
        po_live = po_stale = 0.0
        stale_pos = 0
        po_eta = None
        for po in purchase_lines.get(code, []):
            eta = _parse(po["eta"])
            credible = eta is None or _days_between(eta, today) <= CREDIBLE_PO_SLIP_DAYS
            if credible:
                po_live += po["qty"]
                if eta and (po_eta is None or eta < po_eta):
                    po_eta = eta
            else:
                po_stale += po["qty"]
                stale_pos += 1
        components[code] = {
            "item": code,
            "name": (resource["name"] if is_resource else master.get("name", "")) or code,
            "is_resource": is_resource,
            "group": "CONVERSION / JOB WORK" if is_resource else master.get("group", ""),
            "family": "-" if is_resource else (master.get("variety") or "-"),
            "uom": (resource["uom"] if is_resource else master.get("uom", "")) or "PCS",
            "rate": _round(resource["rate"]) if is_resource else None,
            "reqd": 0.0,
            "used_in": 0,
            "onhand": _round(stock.get(code, {}).get("onhand", 0)),
            "po_live": _round(po_live),
            "po_stale": _round(po_stale),
            "stale_pos": stale_pos,
            "po_count": len(purchase_lines.get(code, [])),
            "po_eta": po_eta.isoformat() if po_eta else None,
            "min_level": _round(master.get("min_level", 0)),
            "price": _round(master.get("price", 0), 4),
            "vendor": vendors.get(code) or None,
            "lead_avg": lead_times.get(code, {}).get("lead_avg"),
            "lead_max": lead_times.get(code, {}).get("lead_max"),
            "lead_lines": lead_times.get(code, {}).get("lead_lines"),
            "has_subbom": code in sub_boms,
            "parents": [],
        }

    for code in gap_codes:
        sku = skus[code]
        for line in sku.components:
            component = components.get(line["child"])
            if component is None:
                continue
            component["reqd"] += line["reqd"]
            component["used_in"] += 1
            component["parents"].append({
                "parent": code,
                "name": sku.name,
                "per_unit": line["per_unit"],
                "reqd": line["reqd"],
                "earliest_due": sku.earliest_due.isoformat() if sku.earliest_due else None,
            })

    for component in components.values():
        component["reqd"] = _round(component["reqd"])
        component["parents"].sort(key=lambda p: -p["reqd"])
        if component["is_resource"]:
            # Nobody raises a purchase order for filling capacity; it is a cost
            # line and a constraint, not a shortage.
            component["short"] = component["short_ex_po"] = component["short_strict"] = 0.0
            continue
        component["short_ex_po"] = _round(max(component["reqd"] - component["onhand"], 0))
        component["short"] = _round(max(
            component["reqd"] - component["onhand"] - component["po_live"] - component["po_stale"], 0
        ))
        component["short_strict"] = _round(max(
            component["reqd"] - component["onhand"] - component["po_live"], 0
        ))

    make_vs_buy = _make_vs_buy(items, components, sub_boms, resources, stock)
    actions = _procurement_actions(components, skus, make_vs_buy, today)
    resource_rows = _resource_rows(components)
    capacity = _capacity_check(reader.production_company, skus, gap_codes)

    summary = _summary(reader, lines, raw_lines, skus, components, actions,
                       resource_rows, unresolved, scope)

    return {
        "generated_at": timezone.now().isoformat(),
        "production_company": reader.production_company,
        "demand_companies": [
            {"code": c, "label": COMPANY_LABELS.get(c, c)} for c in reader.demand_companies
        ],
        "unavailable_books": [
            {"code": c, "label": COMPANY_LABELS.get(c, c), "reason": reason}
            for c, reason in getattr(reader, "unavailable_books", {}).items()
        ],
        "scope": scope,
        "summary": summary,
        "orders": _order_rows(lines),
        "skus": _sku_rows(skus),
        "components": sorted(components.values(), key=lambda c: -c["reqd"]),
        "actions": actions,
        "makevsbuy": make_vs_buy,
        "resources": resource_rows,
        "capacity": capacity,
        "unresolved_demand": unresolved,
        "notes": NOTES,
        "caveats": _caveats(summary, capacity, getattr(reader, "unavailable_books", {})),
        "sources": [
            "SAP Business One — ORDR/RDR1 sales orders, OITW stock, OWOR work "
            "orders, OITT/ITT1 bills of materials, ORSC resources, OPOR/POR1 "
            "purchase orders, OPDN/PDN1 goods receipts, OITM/OITB item master",
            "JIVO Supply Chain Reference Template — machine capacities and the "
            "SKU-to-machine map, which SAP does not hold",
        ],
    }


def _normalise_name(name):
    return " ".join((name or "").upper().split())


def _resolve_to_production_items(reader, lines):
    """Point every demand line at the item the FACTORY knows, not the one its own
    book knows.

    The two company databases share a code space only up to the point they
    diverged. ``FG0000402`` is a 1 LTR sunflower bottle in Mart's master and a
    200 LTR groundnut drum in Oil's — planning Mart's 49,650 bottles against
    Oil's drum turns them into 9.9 million litres of the wrong oil. So a foreign
    code is accepted only when both masters agree on the name, and otherwise the
    name is used to find the real production item.

    Lines that cannot be resolved keep their money and their place in the order
    book — they are real orders — but their ``item`` is cleared so nothing is
    exploded for them, and they are returned to be declared.
    """
    unresolved = {}
    for company in reader.demand_companies:
        if company == reader.production_company:
            continue
        book = [l for l in lines if l["company"] == company]
        codes = sorted({l["item"] for l in book})
        if not codes:
            continue
        own_names = reader.demand_item_names(company, codes)
        by_name = reader.production_codes_for_names(
            sorted({_normalise_name(n) for n in own_names.values() if n})
        )
        production_names = reader.item_master(codes)

        resolution = {}
        for code in codes:
            own = _normalise_name(own_names.get(code, ""))
            here = _normalise_name(production_names.get(code, {}).get("name", ""))
            if here and own and here == own:
                resolution[code] = (code, "code")          # same code, same product
                continue
            candidates = by_name.get(own, [])
            if len(candidates) == 1:
                resolution[code] = (candidates[0], "name")  # renumbered, same product
            elif len(candidates) > 1:
                resolution[code] = (None, "ambiguous")
            else:
                resolution[code] = (None, "unknown")

        for line in book:
            target, basis = resolution.get(line["item"], (None, "unknown"))
            line["source_item"] = line["item"]
            line["match"] = basis
            line["item"] = target or ""
            if target:
                continue
            key = (company, line["source_item"])
            row = unresolved.setdefault(key, {
                "company": company,
                "label": COMPANY_LABELS.get(company, company),
                "item": line["source_item"],
                "name": own_names.get(line["source_item"], line["name"]),
                "reason": (
                    "Its name matches more than one item in the factory's master, "
                    "so which product it is cannot be settled from the data."
                    if basis == "ambiguous" else
                    "The factory's item master holds nothing by this name — it is "
                    "bought in or traded, not made here."
                ),
                "lines": 0, "units": 0.0, "value": 0.0,
            })
            row["lines"] += 1
            row["units"] += line["open"]
            row["value"] += line["value"]

    for line in lines:
        line.setdefault("source_item", line["item"])
        line.setdefault("match", "own")

    return sorted(
        ({**row, "units": _round(row["units"]), "value": _round(row["value"])}
         for row in unresolved.values()),
        key=lambda r: -r["value"],
    )


def _order_rows(lines):
    return [
        {
            "company": line["company"],
            "doc": line["doc"],
            "entry": line["entry"],
            "line": line["line"],
            "source_item": line["source_item"],
            "match": line["match"],
            "party": line["party"],
            "card": line["card"],
            "interco": line["interco"],
            "ordered": line["ordered"],
            "due": line["due"],
            "age": line["age"],
            "late": line["late"],
            "item": line["item"],
            "name": line["name"],
            "qty": _round(line["qty"]),
            "delivered": _round(line["delivered"]),
            "open": _round(line["open"]),
            "price": _round(line["price"], 4),
            "value": line["value"],
        }
        for line in sorted(lines, key=lambda l: -l["value"])
    ]


def _sku_rows(skus):
    rows = []
    for sku in skus.values():
        from_stock = min(sku.onhand, sku.demand)
        from_wip = min(sku.wip, max(sku.demand - from_stock, 0))
        rows.append({
            "item": sku.item,
            "name": sku.name,
            "type": sku.type,
            "variety": sku.variety,
            "uom": sku.uom,
            "orders": sku.lines,
            "demand": _round(sku.demand),
            "value": _round(sku.value),
            "earliest_due": sku.earliest_due.isoformat() if sku.earliest_due else None,
            "latest_due": sku.latest_due.isoformat() if sku.latest_due else None,
            "onhand": _round(sku.onhand),
            "onhand_by_company": sku.onhand_by_company,
            "wip": _round(sku.wip),
            "wo_count": sku.wo_count,
            "has_bom": sku.has_bom,
            "to_produce": _round(sku.to_produce),
            "from_stock": _round(from_stock),
            "from_wip": _round(from_wip),
            "cover": _round((from_stock + from_wip) / sku.demand, 4) if sku.demand else 1.0,
            "components": sku.components,
        })
    return sorted(rows, key=lambda r: -r["to_produce"])


def _make_vs_buy(items, components, sub_boms, resources, stock):
    """Purchased items that carry their own BOM — buy price against build cost."""
    rows = []
    for code, bom in sub_boms.items():
        master = items.get(code)
        if not master:
            continue
        buy_price = master["price"]
        inputs, make_cost = [], 0.0
        for child in bom["lines"]:
            resource = resources.get(child["child"])
            if resource is not None:
                price = resource["rate"]
                name = resource["name"]
            else:
                child_master = items.get(child["child"], {})
                price = child_master.get("price", 0)
                name = child_master.get("name", child["child"])
            cost = child["per_unit"] * price
            make_cost += cost
            inputs.append({
                "code": child["child"],
                "name": name,
                "per_unit": _round(child["per_unit"], 6),
                "price": _round(price, 4),
                "cost": _round(cost, 4),
                "is_resource": resource is not None,
            })

        material = [i for i in inputs if not i["is_resource"]]
        conversion = [i for i in inputs if i["is_resource"]]
        primary = max(material, key=lambda i: i["cost"], default=None)
        component = components.get(code, {})
        rows.append({
            "item": code,
            "name": master["name"],
            "group": master["group"],
            "buy_price": _round(buy_price, 4),
            "make_cost": _round(make_cost, 4),
            "saving_per_unit": _round(buy_price - make_cost, 4),
            "verdict": "MAKE" if 0 < make_cost < buy_price else "BUY",
            "inputs": inputs,
            "sub": primary["code"] if primary else None,
            "sub_name": primary["name"] if primary else "",
            "sub_per_unit": primary["per_unit"] if primary else 0,
            "sub_price": primary["price"] if primary else 0,
            "sub_onhand": _round(stock.get(primary["code"], {}).get("onhand", 0)) if primary else 0,
            "conv": conversion[0]["name"] if conversion else "",
            "conv_rate": conversion[0]["price"] if conversion else 0,
            "in_requirement": component.get("reqd", 0) > 0,
            "reqd_now": component.get("reqd", 0),
        })
    return sorted(rows, key=lambda r: -r["saving_per_unit"])


def _procurement_actions(components, skus, make_vs_buy, today):
    """What has to be ordered, and whether it is already too late to be on time."""
    makeable = {row["item"]: row for row in make_vs_buy if row["verdict"] == "MAKE"}
    actions = []
    for component in components.values():
        if component["is_resource"] or component["short_strict"] <= 0:
            continue
        need_by = None
        for parent in component["parents"]:
            due = _parse(parent["earliest_due"])
            if due and (need_by is None or due < need_by):
                need_by = due
        lead = component["lead_avg"]
        # Already late if the date it had to be ordered by has passed. With no
        # measured lead time there is no order-by date to miss, so the honest
        # verdict is PLAN and the missing history is shown rather than assumed —
        # a lead time of zero would make every un-measured item scream.
        order_by = need_by - timedelta(days=int(lead)) if need_by and lead is not None else None
        urgent = order_by is not None and order_by <= today
        actions.append({
            "item": component["item"],
            "name": component["name"],
            "group": component["group"],
            "uom": component["uom"],
            "reqd": component["reqd"],
            "onhand": component["onhand"],
            "po_live": component["po_live"],
            "po_stale": component["po_stale"],
            "stale_pos": component["stale_pos"],
            "short": component["short_strict"],
            "short_ex_po": component["short_ex_po"],
            "value": _round(component["short_strict"] * component["price"]),
            "price": component["price"],
            "vendor": component["vendor"],
            "lead_avg": lead,
            "lead_max": component["lead_max"],
            "need_by": need_by.isoformat() if need_by else None,
            "order_by": order_by.isoformat() if order_by else None,
            "days_past_due": max(_days_between(need_by, today), 0) if need_by else 0,
            "can_make": component["item"] in makeable,
            "make": makeable.get(component["item"]),
            "urgency": "CRITICAL" if urgent else "PLAN",
        })
    return sorted(actions, key=lambda a: (a["urgency"] != "CRITICAL", -a["value"]))


def _resource_rows(components):
    """Conversion resources the gap consumes — the filling cost line."""
    rows = []
    for component in components.values():
        if not component["is_resource"]:
            continue
        rate = component["rate"] or 0
        rows.append({
            "code": component["item"],
            "name": component["name"],
            "uom": component["uom"],
            "rate": rate,
            "litres_reqd": component["reqd"],
            "cost": _round(component["reqd"] * rate),
            "skus": component["used_in"],
        })
    return sorted(rows, key=lambda r: -r["litres_reqd"])


def _capacity_check(company_code, skus, gap_codes):
    """Can the lines actually run the gap?

    SAP knows the conversion cost of filling; it does not know how many hours of
    which machine that takes. That comes from the reference template, so when the
    template has not been returned this reports honestly that it cannot answer
    rather than showing a green light nobody has earned.
    """
    from ..models import MachineCapacity, MaterialMachineMap

    machines = {
        m.machine_id: m
        for m in MachineCapacity.objects.filter(company_code=company_code, is_active=True)
    }
    mapping = {
        m.sku_code: m
        for m in MaterialMachineMap.objects.filter(company_code=company_code, is_active=True)
    }
    if not machines or not mapping:
        return {
            "available": False,
            "reason": (
                "Machine capacities and the SKU-to-machine map are not on file. "
                "Upload the JIVO Supply Chain Reference Template to turn the "
                "production gap into a feasibility check."
            ),
            "machines": [], "unmapped": [],
            "totals": {"machines": 0, "over_capacity": 0, "unmapped_skus": 0, "feasible": None},
        }

    loads, unmapped = {}, []
    for code in gap_codes:
        sku = skus[code]
        route = mapping.get(code)
        if route is None:
            unmapped.append({
                "sku": code, "name": sku.name, "to_produce": _round(sku.to_produce),
                "reason": "No machine mapped for this SKU in the reference template.",
            })
            continue
        machine = machines.get(route.primary_machine_id)
        if machine is None:
            unmapped.append({
                "sku": code, "name": sku.name, "to_produce": _round(sku.to_produce),
                "reason": f"Mapped to {route.primary_machine_id}, which is not on the capacity sheet.",
            })
            continue
        rate = float(route.output_on_primary or 0) or float(machine.output_per_hour or 0)
        if rate <= 0:
            unmapped.append({
                "sku": code, "name": sku.name, "to_produce": _round(sku.to_produce),
                "reason": f"No output rate on file for {route.primary_machine_id}.",
            })
            continue
        load = loads.setdefault(machine.machine_id, {"machine": machine, "skus": [], "hours": 0.0})
        hours = sku.to_produce / rate
        load["hours"] += hours
        load["skus"].append({
            "sku": code, "name": sku.name, "to_produce": _round(sku.to_produce),
            "rate_per_hour": _round(rate), "hours": _round(hours, 2),
            "alternates": route.alternates,
        })

    rows = []
    for machine_id, load in loads.items():
        machine = load["machine"]
        available = float(machine.available_hours)
        # One changeover per SKU scheduled on the line. The template collects the
        # minutes and the brief's own step 7 forgets them; a capacity check that
        # ignores changeover passes plans the floor cannot run.
        changeover = float(machine.changeover_minutes or 0) * len(load["skus"]) / 60.0
        usable = max(available - changeover, 0.0)
        required = load["hours"]
        rows.append({
            "machine_id": machine_id,
            "name": machine.name,
            "location": machine.location,
            "available_hours": _round(available, 2),
            "changeover_hours": _round(changeover, 2),
            "usable_hours": _round(usable, 2),
            "required_hours": _round(required, 2),
            "utilisation_percent": _round(100 * required / usable, 1) if usable else None,
            "shortfall_hours": _round(max(required - usable, 0), 2),
            "feasible": required <= usable,
            "skus": sorted(load["skus"], key=lambda s: -s["hours"]),
        })
    rows.sort(key=lambda r: -(r["utilisation_percent"] or 0))
    over = [r for r in rows if not r["feasible"]]
    return {
        "available": True,
        "reason": "",
        "machines": rows,
        "unmapped": sorted(unmapped, key=lambda u: -u["to_produce"]),
        "totals": {
            "machines": len(rows),
            "over_capacity": len(over),
            "unmapped_skus": len(unmapped),
            "feasible": not over and not unmapped,
        },
    }


def _summary(reader, lines, raw_lines, skus, components, actions, resources, unresolved, scope):
    gap_skus = [s for s in skus.values() if s.to_produce > 0]
    shippable = sum(
        min(s.onhand + s.wip, s.demand) / s.demand * s.value
        for s in skus.values() if s.demand > 0
    )
    buyable = [c for c in components.values() if not c["is_resource"]]
    by_company = {}
    for line in lines:
        book = by_company.setdefault(line["company"], {
            "company": line["company"],
            "label": COMPANY_LABELS.get(line["company"], line["company"]),
            "orders": set(), "lines": 0, "units": 0.0, "value": 0.0,
        })
        book["orders"].add(line["entry"])
        book["lines"] += 1
        book["units"] += line["open"]
        book["value"] += line["value"]

    summary = {
        "as_of": timezone.localtime().strftime("%d %b %Y, %H:%M IST"),
        "scope": scope,
        "production_company": COMPANY_LABELS.get(reader.production_company),
        "open_orders": len({l["entry"] for l in lines}),
        "open_lines": len(lines),
        "parties": len({l["card"] for l in lines}),
        "interco_lines": sum(1 for l in raw_lines if l["interco"]),
        "interco_value": _round(sum(l["value"] for l in raw_lines if l["interco"])),
        "demand_units": _round(sum(l["open"] for l in lines)),
        "demand_value": _round(sum(l["value"] for l in lines)),
        "late_lines": sum(1 for l in lines if l["late"] > 0),
        "late_value": _round(sum(l["value"] for l in lines if l["late"] > 0)),
        "same_day_due_lines": sum(1 for l in lines if l["ordered"] == l["due"]),
        "oldest_order_days": max((l["age"] for l in lines), default=0),
        "unplannable_skus": len(unresolved),
        "unplannable_lines": sum(row["lines"] for row in unresolved),
        "unplannable_value": _round(sum(row["value"] for row in unresolved)),
        "skus_demanded": len(skus),
        "skus_fully_covered": len(skus) - len(gap_skus),
        "skus_short": len(gap_skus),
        "skus_without_bom": sum(1 for s in gap_skus if not s.has_bom),
        "units_to_produce": _round(sum(s.to_produce for s in gap_skus)),
        "shippable_value": _round(shippable),
        "components_touched": len(components),
        "components_short": sum(1 for c in buyable if c["short_strict"] > 0),
        "components_stale_po": sum(1 for c in buyable if c["po_stale"] > 0),
        "stale_po_units": _round(sum(c["po_stale"] for c in buyable)),
        "buy_value": _round(sum(a["value"] for a in actions)),
        "critical_actions": sum(1 for a in actions if a["urgency"] == "CRITICAL"),
        "filling_litres": _round(sum(r["litres_reqd"] for r in resources)),
        "filling_cost": _round(sum(r["cost"] for r in resources)),
        "books": [
            {**book, "orders": len(book["orders"]),
             "units": _round(book["units"]), "value": _round(book["value"])}
            for book in sorted(by_company.values(), key=lambda b: -b["value"])
        ],
    }
    summary.update(reader.overdue_purchase_summary())
    return summary


def _caveats(summary, capacity, unavailable_books):
    """What this dashboard cannot tell you — stated, not buried."""
    caveats = [
        f"{COMPANY_LABELS.get(code, code)}: {reason}"
        for code, reason in unavailable_books.items()
    ] + [
        f"Delivery dates are weak in this data: the SAP ship date equals the order "
        f"date on {summary['same_day_due_lines']} of {summary['open_lines']} open "
        f"lines, so age is order age and not a confirmed missed date.",
        f"A purchase order counts as supply here only if its date is in the future "
        f"or slipped {CREDIBLE_PO_SLIP_DAYS} days or less. The open PO book is "
        f"heavily stale — {summary['overdue_po_lines']} overdue lines, "
        f"{summary['overdue_po_over180']} of them over 180 days — so on SAP's own "
        f"'all open POs' basis the buy list would be smaller, and wrong.",
        f"Intercompany lines ({summary['interco_lines']}, "
        f"{summary['interco_value']:,.0f} INR) are the group selling to itself. "
        f"The EXTERNAL scope drops them so the same litres are not planned twice; "
        f"the ALL scope keeps them.",
        "Stock is pooled across the Oil and Mart warehouses, because a Mart order "
        "Mart can fill never reaches the factory. Work orders, components, "
        "purchase orders and lead times are Oil's alone — Oil is the only "
        "production unit.",
        "The two company databases number their items independently, so a Mart "
        "order is matched to the factory's item by NAME and only accepted on the "
        "code when both masters agree. Matching on the code alone would plan the "
        "wrong product entirely.",
        "Make-vs-buy is SAP BOM economics. The factory blowing module holds no "
        "runs and no preform specs, so it is not a measured blowing-line cost.",
    ]
    if summary["unplannable_skus"]:
        caveats.append(
            f"{summary['unplannable_skus']} item(s) on {summary['unplannable_lines']} "
            f"line(s), worth {summary['unplannable_value']:,.0f} INR, could not be "
            f"matched to anything the factory makes. They are listed in full and "
            f"left out of the production gap — not counted as covered."
        )
    if summary["skus_without_bom"]:
        caveats.append(
            f"{summary['skus_without_bom']} SKU(s) with a production gap have no "
            f"bill of materials in SAP, so nothing was exploded for them and their "
            f"components are missing from the buy list."
        )
    if not capacity["available"]:
        caveats.append(capacity["reason"])
    return caveats
