"""Oil contracts, read live: SAP's purchase orders for raw-material oils, every
truck received against them, and what the oil cost to bring in.

WHERE EACH FIGURE COMES FROM
 - The contract is the SAP PO's oil: vendor, oil, quantity, rate, what is still
   open. EXIM's register held the same PO numbers typed in by hand. Purchase
   splits a PO line as trucks arrive - the received part becomes a line of its
   own at the adjusted price (bill over weighed quantity) and the last line
   keeps the rest at the contract rate - so a contract is every line of one oil
   on the PO, at the last line's rate.
 - A truck already received is its SAP GRPO line. The GRPO's quantity is what
   was weighed in (unloaded); its value is what the supplier billed, so the
   quantity loaded is that value over the PO's rate. The GRPO header carries the
   vehicle, transporter, bilty and the supplier's invoice number.
 - A truck not received yet is its gate entry here (``raw_material_gatein``):
   in from the moment the gate books the bill, with the weighbridge's gross and
   tare once it has been weighed.
 - Delivery terms, freight and brokerage per tonne are the one thing SAP does
   not hold; they are ``ContractTerms``, one row per PO.

LANDED COST, as EXIM's DC sheet worked it out, per truck:
    basic      = the billed value (loaded x rate)
    freight    = unloaded tonnes x freight per tonne   (EXW contracts)
    brokerage  = loaded tonnes x brokerage per tonne
    landed     = (basic + freight + brokerage) / unloaded
and per litre at 1,098.9 L to the tonne. The transit shortage is the loaded
less the unloaded; the supplier bears what is past 0.25% of the loaded
quantity, at the contract rate (the deduction). Like the sheet, the landed
cost does not subtract that deduction.
"""

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from django.db.models import Q
from django.utils import timezone

from .models_contract import ContractTerms, DeliveryTerms
from .services_licence import EximError
from .services_lot import ALLOWED_SHORTAGE
from .services_tank import DENSITY, SapUnavailable

#: Litres in a tonne of oil (1,000 kg at 1.0989 L to the kg).
LITRES_PER_MT = DENSITY * 1000
#: Units whose quantity can be put in tonnes; a PO in pieces has no landed cost per tonne.
TONNES_PER_UNIT = {"MTS": Decimal("1"), "MT": Decimal("1"), "KGS": Decimal("0.001"), "KG": Decimal("0.001")}

_CENT = Decimal("0.01")
_MILL = Decimal("0.001")


def _q(value, places=_CENT) -> Optional[Decimal]:
    return None if value is None else Decimal(value).quantize(places, rounding=ROUND_HALF_UP)


def financial_year(start_year: int) -> tuple[date, date]:
    """1 April to 31 March: the year EXIM's register was kept by."""
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def current_financial_year(today: Optional[date] = None) -> int:
    today = today or timezone.localdate()
    return today.year if today.month >= 4 else today.year - 1


def _sap(call, *args, **kwargs):
    from sap_client.exceptions import SAPConnectionError, SAPDataError

    try:
        return call(*args, **kwargs)
    except (SAPConnectionError, SAPDataError) as exc:
        raise SapUnavailable(
            "SAP is not answering, so the contracts cannot be read now. Try again shortly.",
            "sap_unavailable",
            {},
        ) from exc


# ---------------------------------------------------------------------------
# A truck, received or at the gate
# ---------------------------------------------------------------------------

def _received_load(grpo: dict, line: dict, terms: Optional[ContractTerms], gate: dict) -> dict:
    """One GRPO line against the contract line, costed."""
    rate = line["rate"]
    unloaded = grpo["quantity"]
    basic = grpo["value"]
    loaded = basic / rate if rate else unloaded
    shortage = loaded - unloaded
    allowed = loaded * ALLOWED_SHORTAGE
    deduction_qty = max(shortage - allowed, Decimal("0"))
    per_mt = TONNES_PER_UNIT.get(line["unit"])
    freight = brokerage = Decimal("0")
    if terms is not None and per_mt is not None:
        freight = unloaded * per_mt * terms.freight_per_mt
        brokerage = loaded * per_mt * terms.brokerage_per_mt
    landed_total = basic + freight + brokerage
    landed_per_unit = landed_total / unloaded if unloaded else None
    landed_per_mt = landed_per_unit / per_mt if landed_per_unit is not None and per_mt else None
    entry = gate.get(grpo["grpo_number"])
    return {
        "kind": "RECEIVED",
        "grpo_number": grpo["grpo_number"],
        "grpo_date": grpo["grpo_date"],
        "invoice_no": grpo["invoice_no"],
        "vehicle_number": grpo["vehicle_number"] or (entry or {}).get("vehicle_number", ""),
        "transporter": grpo["transporter"] or (entry or {}).get("transporter", ""),
        "bilty_number": grpo["bilty_number"],
        "gate_entry": (entry or {}).get("entry_no"),
        "gate_date": (entry or {}).get("entry_time"),
        "loaded": _q(loaded, _MILL),
        "unloaded": _q(unloaded, _MILL),
        "shortage": _q(shortage, _MILL),
        "allowed": _q(allowed, _MILL),
        "deduction_qty": _q(deduction_qty, _MILL),
        "deduction_amount": _q(deduction_qty * rate),
        "basic": _q(basic),
        "freight": _q(freight),
        "brokerage": _q(brokerage),
        "landed_total": _q(landed_total),
        "landed_per_unit": _q(landed_per_unit),
        "landed_per_mt": _q(landed_per_mt),
        "landed_per_litre": _q(landed_per_mt / LITRES_PER_MT, _MILL) if landed_per_mt is not None else None,
    }


def _gate_loads(company, po_numbers) -> tuple[dict, dict]:
    """Our gate entries against these POs.

    Returns ``(by_grpo, pending)``: the entry each posted GRPO came from, by
    GRPO number; and, by (PO number, item code), the trucks the gate has booked
    that no GRPO has taken in yet."""
    from grpo.models import GRPOPosting, GRPOStatus
    from raw_material_gatein.models import POItemReceipt

    items = (
        POItemReceipt.objects.filter(
            po_receipt__po_number__in=list(po_numbers),
            po_receipt__vehicle_entry__company=company,
        )
        .exclude(po_receipt__vehicle_entry__status="CANCELLED")
        .select_related(
            "po_receipt__vehicle_entry__vehicle__transporter",
            "po_receipt__vehicle_entry__weighment",
        )
    )
    receipts = {item.po_receipt_id for item in items}
    posted = defaultdict(set)
    for posting in GRPOPosting.objects.filter(
        Q(po_receipt_id__in=receipts) | Q(po_receipts__in=receipts), status=GRPOStatus.POSTED,
    ).prefetch_related("po_receipts"):
        linked = {posting.po_receipt_id} | {r.pk for r in posting.po_receipts.all()}
        for receipt in linked & receipts:
            if posting.sap_doc_num:
                posted[receipt].add(str(posting.sap_doc_num))

    by_grpo, pending = {}, defaultdict(list)
    for item in items:
        receipt = item.po_receipt
        entry = receipt.vehicle_entry
        vehicle = entry.vehicle
        weighment = getattr(entry, "weighment", None)
        facts = {
            "entry_no": entry.entry_no,
            "entry_time": timezone.localtime(entry.entry_time) if entry.entry_time else None,
            "status": entry.status,
            "vehicle_number": vehicle.vehicle_number if vehicle else "",
            "transporter": vehicle.transporter.name if vehicle and vehicle.transporter else "",
        }
        for grpo_number in posted.get(receipt.pk, ()):
            by_grpo[grpo_number] = facts
        if receipt.pk in posted:
            continue
        gross = weighment.gross_weight if weighment else None
        tare = weighment.tare_weight if weighment else None
        weighed_kg = gross - tare if gross and tare else None
        pending[(receipt.po_number, item.po_item_code)].append({
            "kind": "AT_GATE",
            **facts,
            "invoice_no": receipt.invoice_no,
            "billed": _q(item.received_qty, _MILL),
            "billed_unit": (item.uom or "").upper(),
            "gross_kg": _q(gross, _MILL) if gross else None,
            "tare_kg": _q(tare, _MILL) if tare else None,
            "weighed_kg": _q(weighed_kg, _MILL),
        })
    return by_grpo, pending


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

def _terms_of(company, po_numbers) -> dict:
    return {t.po_number: t for t in ContractTerms.objects.filter(company=company, po_number__in=list(po_numbers))}


def terms_dict(terms: Optional[ContractTerms]) -> dict:
    if terms is None:
        return {"delivery_terms": "", "freight_per_mt": None, "brokerage_per_mt": None, "note": ""}
    return {
        "delivery_terms": terms.delivery_terms,
        "freight_per_mt": terms.freight_per_mt,
        "brokerage_per_mt": terms.brokerage_per_mt,
        "note": terms.note,
    }


def _stage(line: dict, received: Decimal, at_gate: int) -> str:
    if line["closed"] or line["open_qty"] <= 0:
        return "COMPLETE"
    if received > 0 or at_gate:
        return "ARRIVING"
    return "AWAITING"


def _contract(line: dict, grpos: list, pending: list, terms, gate_by_grpo: dict, with_loads: bool) -> dict:
    loads = [_received_load(g, line, terms, gate_by_grpo) for g in grpos]
    received = sum((load["unloaded"] for load in loads), Decimal("0"))
    loaded = sum((load["loaded"] for load in loads), Decimal("0"))
    landed = sum((load["landed_total"] for load in loads), Decimal("0"))
    deduction = sum((load["deduction_amount"] for load in loads), Decimal("0"))
    billed_at_gate = sum((p["billed"] or 0 for p in pending), Decimal("0"))
    per_mt = TONNES_PER_UNIT.get(line["unit"])
    landed_per_unit = landed / received if received else None
    landed_per_mt = landed_per_unit / per_mt if landed_per_unit is not None and per_mt else None
    row = {
        "po_number": line["po_number"],
        "po_lines": line["lines"],
        "po_date": line["po_date"],
        "vendor_code": line["vendor_code"],
        "vendor_name": line["vendor_name"],
        "item_code": line["item_code"],
        "item_name": line["item_name"],
        "unit": line["unit"],
        "quantity": line["quantity"],
        "rate": line["rate"],
        "value": line["value"],
        "open_qty": line["open_qty"],
        "closed": line["closed"],
        "stage": _stage(line, received, len(pending)),
        "received": _q(received, _MILL),
        "loaded": _q(loaded, _MILL),
        "trucks_received": len(loads),
        "trucks_at_gate": len(pending),
        "at_gate": _q(billed_at_gate, _MILL),
        "to_come": _q(max(line["open_qty"] - billed_at_gate, Decimal("0")), _MILL) if not line["closed"] else Decimal("0"),
        "deduction_amount": _q(deduction),
        "landed_per_unit": _q(landed_per_unit),
        "landed_per_mt": _q(landed_per_mt),
        "landed_per_litre": _q(landed_per_mt / LITRES_PER_MT, _MILL) if landed_per_mt is not None else None,
        "terms": terms_dict(terms),
    }
    if with_loads:
        row["loads"] = loads
        row["at_gate_loads"] = pending
    return row


def _contracts_of(lines: list) -> list:
    """SAP's PO lines, one contract per oil on each PO: the quantities summed,
    at the contract rate - the last line's, the one purchase leaves unsplit."""
    groups = {}
    for line in lines:
        key = (line["doc_entry"], line["item_code"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {**line, "lines": [], "quantity": Decimal("0"), "open_qty": Decimal("0"),
                                   "closed": True, "line": line["line"]}
        group["lines"].append(line["line"])
        group["quantity"] += line["quantity"]
        group["open_qty"] += line["open_qty"]
        group["closed"] = group["closed"] and line["closed"]
        if line["line"] >= group["line"]:
            group["line"], group["rate"] = line["line"], line["rate"]
    for group in groups.values():
        group["value"] = _q(group["quantity"] * group["rate"])
    return list(groups.values())


def _build(company, lines: list, *, with_loads: bool) -> list:
    from .hana_reader import oil_grpo_lines

    grpos = _sap(oil_grpo_lines, company.code, [line["doc_entry"] for line in lines]) if lines else []
    item_of_line = {(line["doc_entry"], line["line"]): line["item_code"] for line in lines}
    by_contract = defaultdict(list)
    for grpo in grpos:
        item = item_of_line.get((grpo["po_doc_entry"], grpo["po_line"]))
        if item is not None:
            by_contract[(grpo["po_doc_entry"], item)].append(grpo)
    po_numbers = {line["po_number"] for line in lines}
    gate_by_grpo, pending = _gate_loads(company, po_numbers)
    terms = _terms_of(company, po_numbers)
    return [
        _contract(
            contract,
            by_contract.get((contract["doc_entry"], contract["item_code"]), []),
            pending.get((contract["po_number"], contract["item_code"]), []),
            terms.get(contract["po_number"]),
            gate_by_grpo,
            with_loads,
        )
        for contract in _contracts_of(lines)
    ]


def _totals(rows: list) -> dict:
    tonnes = lambda row, field: (row[field] or 0) * TONNES_PER_UNIT.get(row["unit"], Decimal("0"))  # noqa: E731
    in_mt = [row for row in rows if row["unit"] in TONNES_PER_UNIT]
    received_mt = sum((tonnes(r, "received") for r in in_mt), Decimal("0"))
    landed = sum(
        ((r["landed_per_mt"] or 0) * tonnes(r, "received") for r in in_mt if r["landed_per_mt"] is not None),
        Decimal("0"),
    )
    return {
        "contracts": len(rows),
        "open": sum(1 for r in rows if r["stage"] != "COMPLETE"),
        "value": _q(sum((r["value"] for r in rows), Decimal("0"))),
        "contracted_mt": _q(sum((tonnes(r, "quantity") for r in in_mt), Decimal("0")), _MILL),
        "received_mt": _q(received_mt, _MILL),
        "at_gate_mt": _q(sum((tonnes(r, "at_gate") for r in in_mt), Decimal("0")), _MILL),
        "to_come_mt": _q(sum((tonnes(r, "to_come") for r in in_mt), Decimal("0")), _MILL),
        "trucks_at_gate": sum(r["trucks_at_gate"] for r in rows),
        "deduction_amount": _q(sum((r["deduction_amount"] for r in rows), Decimal("0"))),
        "landed_per_mt": _q(landed / received_mt) if received_mt else None,
        "landed_per_litre": _q(landed / received_mt / LITRES_PER_MT, _MILL) if received_mt else None,
    }


def contracts(company, *, year: Optional[int] = None, open_only: bool = False) -> dict:
    """The oil contracts of a financial year (by PO date), or every one still
    open whatever its year, with what each has received and cost."""
    from .hana_reader import oil_po_lines

    if open_only:
        lines = _sap(oil_po_lines, company.code, open_only=True)
        span = {"year": None, "from": None, "to": None}
    else:
        year = year if year is not None else current_financial_year()
        start, end = financial_year(year)
        lines = _sap(oil_po_lines, company.code, date_from=start, date_to=end)
        span = {"year": year, "from": start, "to": end}
    rows = _build(company, lines, with_loads=False)
    return {**span, "open_only": open_only, "contracts": rows, "totals": _totals(rows)}


def contract(company, po_number: str) -> dict:
    """One purchase order: each oil line on it with every truck, received or at the gate."""
    from .hana_reader import oil_po_lines

    po_number = str(po_number).strip()
    if not po_number.isdigit():
        raise EximError("A PO number is digits only.", "bad_po_number", {"po_number": po_number})
    lines = _sap(oil_po_lines, company.code, po_number=po_number)
    if not lines:
        raise EximError(
            f"SAP has no oil purchase order {po_number}.", "contract_not_found", {"po_number": po_number}
        )
    rows = _build(company, lines, with_loads=True)
    head = lines[0]
    return {
        "po_number": po_number,
        "po_date": head["po_date"],
        "vendor_code": head["vendor_code"],
        "vendor_name": head["vendor_name"],
        "lines": rows,
        "terms": rows[0]["terms"],
        "totals": _totals(rows),
    }


def set_terms(company, po_number: str, *, user, delivery_terms: str, freight_per_mt, brokerage_per_mt,
              note: str = "") -> ContractTerms:
    """Record how a PO is delivered and what freight and brokerage it carries.
    The PO must be one of SAP's oil purchase orders."""
    from .hana_reader import oil_po_lines

    po_number = str(po_number).strip()
    freight = Decimal(freight_per_mt or 0)
    brokerage = Decimal(brokerage_per_mt or 0)
    if freight < 0 or brokerage < 0:
        raise EximError("Freight and brokerage cannot be negative.", "negative_rate", {})
    if delivery_terms == DeliveryTerms.FOR and freight > 0:
        raise EximError(
            "On a FOR contract the supplier delivers and the price includes the freight.",
            "freight_on_for",
            {},
        )
    if not po_number.isdigit() or not _sap(oil_po_lines, company.code, po_number=po_number):
        raise EximError(
            f"SAP has no oil purchase order {po_number}.", "contract_not_found", {"po_number": po_number}
        )
    terms, _ = ContractTerms.objects.get_or_create(company=company, po_number=po_number)
    terms.delivery_terms = delivery_terms
    terms.freight_per_mt = freight
    terms.brokerage_per_mt = brokerage
    terms.note = (note or "").strip()[:255]
    terms.updated_by = user
    terms.save()
    return terms
