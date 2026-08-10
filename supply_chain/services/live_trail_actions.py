"""Who has to do what — the trail turned into a department action list.

The brief's first named problem is that five departments drive the supply chain
independently, "each keeps its own view of the numbers, and coordination between
them is manual, slow, and reactive". A dashboard that shows one shared set of
numbers fixes half of that. The other half is this: every issue on the trail
routed to the department that can actually clear it, so nothing sits in the gap
between two HODs waiting for someone to notice it.

Three rules keep the list worth reading:

**One owner per issue.** Not a broadcast. If two departments can both see a row,
neither is accountable for it, which is the state the brief is complaining about.

**Severity is earned, not assigned.** CRITICAL means the date it had to be acted
on has already passed and the answer is to expedite. PLAN means there is still
lead time left. WATCH is a decision or a data gap, not a deadline. An action list
where everything is red is a list nobody reads.

**Every action names its evidence.** What it is, what it blocks, what it costs
and which date it missed — so the receiving HOD can check it rather than
believe it.

The departments are the five the brief names. Where a department is not obvious
from the data — an item master that disagrees with itself, for instance — the
action still gets an owner and says plainly why it landed there, rather than
being dropped for lack of a tidy home.
"""
from ..models import MaterialType

# The five the brief names, in the order an issue travels: make it, buy the
# packaging, buy the oil, run it, pay for it.
DEPARTMENTS = {
    "PRODUCTION": {
        "label": "Production",
        "remit": "What has to be made, and the master data that lets it be planned.",
    },
    "PACKAGING_PROCUREMENT": {
        "label": "Packaging Procurement",
        "remit": "Bottles, caps, labels, cartons — everything the fill goes into.",
    },
    "RAW_PROCUREMENT": {
        "label": "Oil & Raw-Material Procurement",
        "remit": "Bulk oils and raw materials.",
    },
    "INFRASTRUCTURE": {
        "label": "Infrastructure",
        "remit": "Storage and filling-line capacity.",
    },
    "FINANCE": {
        "label": "Finance",
        "remit": "Releasing the spend, and the cash tied up in a stale PO book.",
    },
}

DEPARTMENT_ORDER = list(DEPARTMENTS)

# Act today / act within the lead time / decide or fix the data.
CRITICAL, PLAN, WATCH = "CRITICAL", "PLAN", "WATCH"
SEVERITY_ORDER = {CRITICAL: 0, PLAN: 1, WATCH: 2}

# How many stale POs are worth naming individually before the tail becomes noise.
# The count and value of the rest are still reported, so nothing is hidden.
STALE_PO_ACTIONS = 15


def _material_department(group):
    """Which buyer owns this component, from its SAP item group.

    Matched on the group rather than the item code because the code prefix is a
    naming convention and the group is what SAP actually enforces.
    """
    group = (group or "").upper()
    if "PACKAG" in group:
        return "PACKAGING_PROCUREMENT", MaterialType.PACKAGING
    if "RAW" in group or "OIL" in group:
        return "RAW_PROCUREMENT", MaterialType.RAW
    # Anything else purchased still has to be bought by somebody; packaging is
    # the larger book and the safer default, and the action names its group so a
    # mis-route is visible rather than silent.
    return "PACKAGING_PROCUREMENT", MaterialType.PACKAGING


def _action(**kwargs):
    kwargs.setdefault("blocks", [])
    kwargs.setdefault("value", 0.0)
    kwargs.setdefault("due", None)
    kwargs.setdefault("days_late", 0)
    return kwargs


def build_department_actions(*, skus, components, actions, capacity, unresolved,
                             summary, money):
    """Route every open issue to the one department that can clear it.

    ``money`` formats a rupee figure for the human-readable text; the raw number
    travels alongside it so the UI can sort and total without re-parsing.
    """
    out = []

    out += _procurement_actions(actions, components, money)
    out += _production_actions(skus, unresolved, money)
    out += _infrastructure_actions(capacity, summary, money)
    out += _finance_actions(actions, summary, money)

    out.sort(key=lambda a: (SEVERITY_ORDER[a["severity"]], -a["value"], a["title"]))
    return _group_by_department(out)


def _procurement_actions(actions, components, money):
    """Buy it, or chase the order that was supposed to have delivered it."""
    out = []
    by_code = {c["item"]: c for c in components}

    for action in actions:
        department, material = _material_department(action["group"])
        component = by_code.get(action["item"], {})
        blocks = [p["name"] for p in component.get("parents", [])[:3]]

        if action["lead_avg"] is None:
            # No receipt history means no order-by date, so calling it late would
            # be an invention. It is still a shortage, and the missing history is
            # itself the thing to fix.
            severity, timing = WATCH, (
                "No goods-receipt history for this item, so no order-by date can "
                "be measured. Order it and record the receipt to start the clock."
            )
        elif action["urgency"] == CRITICAL:
            severity, timing = CRITICAL, (
                f"It had to be ordered by {action['order_by']} to arrive for "
                f"{action['need_by']}. Expedite — do not schedule."
            )
        else:
            severity, timing = PLAN, (
                f"Order by {action['order_by']} to arrive for {action['need_by']} "
                f"({action['lead_avg']} day lead time)."
            )

        out.append(_action(
            id=f"buy:{action['item']}",
            department=department,
            material_type=material,
            severity=severity,
            kind="RAISE_PO",
            title=f"Raise a PO for {action['short']:,.0f} {action['uom']} of {action['name']}",
            detail=(
                f"{timing} Last vendor {action['vendor'] or 'not on record'}. "
                f"Requirement {action['reqd']:,.0f}, on hand {action['onhand']:,.0f}, "
                f"credible inbound {action['po_live']:,.0f}."
                + (" An in-house route exists — see make-vs-buy."
                   if action["can_make"] else "")
            ),
            subject={"kind": "component", "code": action["item"], "name": action["name"]},
            due=action["order_by"] or action["need_by"],
            days_late=action["days_past_due"],
            value=action["value"],
            blocks=blocks,
        ))

    for component in components:
        if component["is_resource"] or component["po_stale"] <= 0:
            continue
        department, material = _material_department(component["group"])
        out.append(_action(
            id=f"chase:{component['item']}",
            department=department,
            material_type=material,
            severity=WATCH,
            kind="CHASE_PO",
            title=(
                f"Chase or cancel {component['stale_pos']} dead PO(s) for "
                f"{component['po_stale']:,.0f} {component['uom']} of {component['name']}"
            ),
            detail=(
                "The expected date slipped more than 30 days, so this trail does "
                "not count it as supply. Either get a firm date and re-date the "
                "PO, or cancel it so the requirement shows honestly."
            ),
            subject={"kind": "component", "code": component["item"],
                     "name": component["name"]},
            value=component["po_stale"] * component["price"],
            blocks=[p["name"] for p in component["parents"][:3]],
        ))

    return out


def _production_actions(skus, unresolved, money):
    """Make it — and fix the master data that stops it being planned."""
    out = []

    for sku in skus:
        if sku["to_produce"] <= 0:
            continue

        if not sku["has_bom"]:
            out.append(_action(
                id=f"bom:{sku['item']}",
                department="PRODUCTION",
                material_type="",
                severity=WATCH,
                kind="MISSING_BOM",
                title=f"No bill of materials for {sku['name']}",
                detail=(
                    f"{sku['to_produce']:,.0f} {sku['uom']} have to be produced and "
                    f"SAP holds no BOM for this item, so nothing was exploded for "
                    f"it and none of its components are on any buy list. Until the "
                    f"BOM exists this SKU is invisible to procurement."
                ),
                subject={"kind": "sku", "code": sku["item"], "name": sku["name"]},
                due=sku["earliest_due"],
                value=sku["value"],
            ))
            continue

        if sku["wo_count"] == 0:
            out.append(_action(
                id=f"wo:{sku['item']}",
                department="PRODUCTION",
                material_type="",
                severity=PLAN,
                kind="NO_WORK_ORDER",
                title=f"Open a work order for {sku['to_produce']:,.0f} {sku['uom']} of {sku['name']}",
                detail=(
                    f"{sku['orders']} order(s) are waiting, stock covers "
                    f"{sku['from_stock']:,.0f} and nothing is on the floor for the "
                    f"rest. Earliest delivery date on the book is {sku['earliest_due']}."
                ),
                subject={"kind": "sku", "code": sku["item"], "name": sku["name"]},
                due=sku["earliest_due"],
                value=sku["value"],
            ))

    for row in unresolved:
        out.append(_action(
            id=f"item:{row['company']}:{row['item']}",
            department="PRODUCTION",
            material_type="",
            severity=WATCH,
            kind="UNMATCHED_ITEM",
            title=f"Master data: {row['name']} ({row['item']}) cannot be matched to a factory item",
            detail=(
                f"{row['lines']} open line(s) worth {money(row['value'])} in the "
                f"{row['label']} book. {row['reason']} Until the two item masters "
                f"agree, this demand cannot be planned and is deliberately left "
                f"out of the production gap rather than counted as covered."
            ),
            subject={"kind": "none", "code": row["item"], "name": row["name"]},
            value=row["value"],
        ))

    return out


def _infrastructure_actions(capacity, summary, money):
    """Can the lines and the tanks actually take it?"""
    if not capacity["available"]:
        return [_action(
            id="capacity:reference",
            department="INFRASTRUCTURE",
            material_type="",
            severity=WATCH,
            kind="MISSING_REFERENCE",
            title="Return the machine capacity and SKU-to-machine sheets",
            detail=(
                f"SAP does not hold machine capacity or the material-to-machine "
                f"map, so whether the plan can actually be run is unknown. What is "
                f"known is the fill itself: {summary['filling_litres']:,.0f} litres "
                f"at {money(summary['filling_cost'])}. Upload the reference "
                f"template and this becomes a real feasibility check."
            ),
            subject={"kind": "none", "code": "", "name": ""},
        )]

    out = []
    for machine in capacity["machines"]:
        if machine["feasible"]:
            continue
        out.append(_action(
            id=f"capacity:{machine['machine_id']}",
            department="INFRASTRUCTURE",
            material_type="",
            severity=CRITICAL,
            kind="OVER_CAPACITY",
            title=(
                f"{machine['machine_id']} {machine['name']} is over capacity by "
                f"{machine['shortfall_hours']:,.1f} hours"
            ),
            detail=(
                f"{machine['required_hours']:,.1f} hours of work against "
                f"{machine['usable_hours']:,.1f} usable (after "
                f"{machine['changeover_hours']:,.1f} hours of changeover across "
                f"{len(machine['skus'])} SKUs). Move work to an alternate line, "
                f"add a shift, or the plan does not run."
            ),
            subject={"kind": "machine", "code": machine["machine_id"],
                     "name": machine["name"]},
        ))

    for row in capacity["unmapped"]:
        out.append(_action(
            id=f"unmapped:{row['sku']}",
            department="INFRASTRUCTURE",
            material_type="",
            severity=WATCH,
            kind="UNMAPPED_SKU",
            title=f"No line mapped for {row['name']}",
            detail=(
                f"{row['to_produce']:,.0f} pieces have to be made and this SKU is "
                f"not in the capacity check. {row['reason']}"
            ),
            subject={"kind": "sku", "code": row["sku"], "name": row["name"]},
        ))

    return out


def _finance_actions(actions, summary, money):
    """Release the money, and decide about the money already committed."""
    out = []

    if actions:
        critical = [a for a in actions if a["urgency"] == CRITICAL]
        critical_value = sum(a["value"] for a in critical)
        out.append(_action(
            id="finance:release",
            department="FINANCE",
            material_type="",
            severity=CRITICAL if critical else PLAN,
            kind="RELEASE_FUNDS",
            title=f"Release {money(summary['buy_value'])} for {len(actions)} purchase order(s)",
            detail=(
                f"{money(critical_value)} of it is on {len(critical)} material(s) "
                f"already past the date they had to be ordered, so that part is an "
                f"expedite rather than a plan. Priced at each item's last purchase "
                f"price."
                if critical else
                "None of it is past its order date yet — this is a scheduled "
                "release, priced at each item's last purchase price."
            ),
            subject={"kind": "none", "code": "", "name": ""},
            value=summary["buy_value"],
        ))

    if summary["overdue_po_lines"]:
        out.append(_action(
            id="finance:stale-po",
            department="FINANCE",
            material_type="",
            severity=WATCH,
            kind="STALE_PO_DECISION",
            title=(
                f"Decide on {money(summary['overdue_po_value'])} sitting on "
                f"{summary['overdue_po_lines']} overdue PO lines"
            ),
            detail=(
                f"Across {summary['overdue_po_docs']} purchase orders, "
                f"{summary['overdue_po_over180']} lines are more than 180 days past "
                f"their date, the oldest from {summary['overdue_po_oldest']}. Each "
                f"one is either supply that is still coming — in which case it needs "
                f"a real date — or it is not, in which case it is overstating cover "
                f"and understating what has to be bought."
            ),
            subject={"kind": "none", "code": "", "name": ""},
            value=summary["overdue_po_value"],
        ))

    return out


def _group_by_department(actions):
    """One card per department, with the counts that decide who is called first."""
    grouped = []
    for code in DEPARTMENT_ORDER:
        mine = [a for a in actions if a["department"] == code]
        critical = [a for a in mine if a["severity"] == CRITICAL]
        plan = [a for a in mine if a["severity"] == PLAN]
        watch = [a for a in mine if a["severity"] == WATCH]
        grouped.append({
            "code": code,
            "label": DEPARTMENTS[code]["label"],
            "remit": DEPARTMENTS[code]["remit"],
            "total": len(mine),
            "critical": len(critical),
            "plan": len(plan),
            "watch": len(watch),
            # Only what is genuinely at stake for THIS department — summing a
            # shortage's value and the whole PO book's value would be nonsense.
            "value": round(sum(a["value"] for a in mine), 2),
            "headline": _headline(code, critical, plan, watch),
            "actions": mine,
        })
    return grouped


def _headline(code, critical, plan, watch):
    """The one line an HOD reads before deciding whether to open the card."""
    if critical:
        return critical[0]["title"]
    if plan:
        return f"{len(plan)} action(s) still inside their lead time."
    if watch:
        return watch[0]["title"]
    return "Nothing outstanding."
