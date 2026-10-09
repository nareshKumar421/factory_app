"""The audit checklist for a vendor's bill against its GRPO.

Pure functions: everything they judge is handed in (``services.gather_context``
does the reading), so each rule can be tested on its own and re-run without
touching SAP. Each returns a ``Finding``; ``run_checks`` returns all nine in
checklist order.

A finding is PASS or FAIL when the app can say so from the records, REVIEW when
it found something only a person can settle (a signature, a QC record that is
not in the app), and UNKNOWN when there is nothing to judge yet -- usually
because the bill has not been read. A person's OK / Not OK on the page outranks
any of them (``models.APInvoiceDraftCheck.effective_status``).
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from .models import CheckStatus

#: The store a packing-material bill must have been received into.
EXPECTED_WAREHOUSE = "BH-PM"
#: Days the store has, from the truck's arrival, to make the GRPO.
GRPO_DAYS_AFTER_ARRIVAL = 3
#: Whose signature the bill's "Rate Check" line must carry.
RATE_CHECKER = "Kulbeer"
#: Who the printed Purchase Order must name as its approver.
PO_APPROVER = "Gagandeep Singh"
#: The gate's over-receipt allowance, as ``raw_material_gatein`` applies it.
OVER_RECEIPT_TOLERANCE = Decimal("1.10")

#: QC states that mean the item has not passed, as the GRPO flow reads them.
QC_NOT_PASSED = {"REJECTED", "HOLD", "PENDING", "ARRIVAL_SLIP_PENDING", "INSPECTION_PENDING"}

PAISA = Decimal("0.01")
GST_AMOUNT_SLACK = Decimal("1.00")


@dataclass
class Finding:
    key: str
    label: str
    status: str
    detail: str
    facts: dict = field(default_factory=dict)


LABELS = {
    "invoice_number": "Invoice number matches the GRPO's reference number",
    "warehouse": f"GRPO is received into {EXPECTED_WAREHOUSE}",
    "gst": "Invoice GST matches the PO's GST",
    "grpo_timing": f"GRPO made within {GRPO_DAYS_AFTER_ARRIVAL} days of arrival",
    "rate_check_signature": f"{RATE_CHECKER}'s signature on the invoice's Rate Check stamp",
    "po_approver": f"PO print approved by {PO_APPROVER}",
    "po_rate": "PO rate matches GRPO rate",
    "over_receipt": "Received within PO + 10% (as on the gate)",
    "qc": "QC approved",
}

ORDER = list(LABELS)

NOT_READ = "The invoice has not been read yet."


def _finding(key, status, detail, **facts) -> Finding:
    return Finding(key=key, label=LABELS[key], status=status, detail=detail, facts=facts)


# ---------------------------------------------------------------------------
# 1. Invoice number
# ---------------------------------------------------------------------------

def _compact(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _alnum(value) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(value or "").upper())


def _rows(invoice: dict) -> list[str]:
    return [row["text"] for row in invoice.get("rows") or []]


INVOICE_NO_LABEL = re.compile(r"invoice\s*no", re.I)
#: A bill number as printed: has a digit and a / or -, e.g. 26-27/1979, NINV/26-27/1113.
BILL_NUMBER = re.compile(r"[A-Z0-9][A-Z0-9/\-]*[/\-][A-Z0-9/\-]*[0-9][A-Z0-9/\-]*", re.I)
#: 2-Oct-26, 30.09.26, 01/10/2026: a real month, one separator. Not 26-27/1979.
DATE_LIKE = re.compile(r"^\d{1,2}([-/.])([A-Z]{3}|0?[1-9]|1[0-2])\1\d{2,4}$", re.I)


def _bill_number_near_label(invoice: dict) -> str:
    """What the bill prints by its "Invoice No." label: in the same box, to its
    right on the same line, or under it in the same column."""
    lines = invoice.get("lines") or []
    for label in (line for line in lines if INVOICE_NO_LABEL.search(line["text"])):
        lx0, ly0, lx1, ly1 = label["box"]
        h = max(1, ly1 - ly0)
        same_box = label["text"][INVOICE_NO_LABEL.search(label["text"]).end():]
        beside = sorted(
            (l for l in lines if l["page"] == label["page"] and l is not label
             and abs((l["box"][1] + l["box"][3]) / 2 - (ly0 + ly1) / 2) <= h / 2
             # OCR boxes overlap a little; what counts is lying to the right.
             and (l["box"][0] + l["box"][2]) / 2 > lx1),
            key=lambda l: l["box"][0],
        )[:2]
        under = sorted(
            (l for l in lines if l["page"] == label["page"]
             and ly1 <= l["box"][1] <= ly1 + 2 * h and l["box"][0] < lx1 + h and l["box"][2] > lx0 - h),
            key=lambda l: l["box"][1],
        )[:1]
        for text in [same_box] + [l["text"] for l in beside + under]:
            for token in BILL_NUMBER.findall(text):
                if not DATE_LIKE.match(token):
                    return token
    return ""


def check_invoice_number(grpo: dict, invoice: Optional[dict]) -> Finding:
    reference = grpo.get("reference") or ""
    if invoice is None:
        return _finding("invoice_number", CheckStatus.UNKNOWN, NOT_READ, grpo_reference=reference)
    rows = _rows(invoice)
    near_label = _bill_number_near_label(invoice)
    facts = {"grpo_reference": reference, "near_label": near_label}
    if not reference:
        return _finding(
            "invoice_number", CheckStatus.FAIL,
            f"The GRPO carries no reference number; the bill reads {near_label or 'no number'}.", **facts,
        )
    wanted = _compact(reference)
    found = next((row for row in rows if wanted in _compact(row)), None)
    if found is None and len(_alnum(reference)) >= 5:
        # OCR sometimes drops a slash or a dash; the characters still have to match.
        found = next((row for row in rows if _alnum(reference) in _alnum(row)), None)
    if found is not None:
        facts["found_in"] = found
        return _finding("invoice_number", CheckStatus.PASS, f"{reference} is printed on the bill.", **facts)
    if near_label and _alnum(near_label) != _alnum(reference):
        return _finding(
            "invoice_number", CheckStatus.FAIL,
            f"The bill's invoice no. reads {near_label}; the GRPO's reference is {reference}.", **facts,
        )
    return _finding(
        "invoice_number", CheckStatus.REVIEW,
        f"{reference} could not be found on the scan. Compare it with the bill by eye.", **facts,
    )


# ---------------------------------------------------------------------------
# 2. Warehouse
# ---------------------------------------------------------------------------

def check_warehouse(grpo: dict) -> Finding:
    lines = [
        {"line": line["line_num"] + 1, "item_code": line["item_code"], "warehouse": line["warehouse"]}
        for line in grpo["lines"]
    ]
    wrong = [line for line in lines if line["warehouse"] != EXPECTED_WAREHOUSE]
    if not lines:
        return _finding("warehouse", CheckStatus.UNKNOWN, "The GRPO has no lines.", lines=lines)
    if not wrong:
        return _finding(
            "warehouse", CheckStatus.PASS,
            f"Every line went into {EXPECTED_WAREHOUSE}.", lines=lines,
        )
    where = ", ".join(f"line {w['line']} ({w['item_code']}) into {w['warehouse'] or 'no warehouse'}" for w in wrong)
    return _finding("warehouse", CheckStatus.FAIL, f"Not {EXPECTED_WAREHOUSE}: {where}.", lines=lines)


# ---------------------------------------------------------------------------
# 3. GST
# ---------------------------------------------------------------------------

def tax_kind_of_code(tax_code: str) -> str:
    """IGST or CGST+SGST from a SAP tax code ("IGST@18", "CG+SG@5", "RIGST...")."""
    code = (tax_code or "").upper()
    if "IGST" in code or code.startswith(("RIGST", "RISGT")):
        return "IGST"
    if code:
        return "CGST+SGST"
    return ""


#: Amounts as bills print them: 1,83,898.05 (Indian), 183,898.05, 9161.00.
AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{1,2})(?![\d])")
PERCENT = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*%|@\s*(\d{1,2}(?:\.\d{1,2})?)")
#: Rows that print a rate which is not GST: the late-payment interest clause.
NOT_TAX = re.compile(r"interest|p\.\s*a\b|\bpa\b|per\s+annum", re.I)
IGST_WORD = re.compile(r"I\s*GST", re.I)
CGST_WORD = re.compile(r"C\s*GST|S\s*GST|UTGST", re.I)


def _amounts(text: str) -> list[Decimal]:
    return [Decimal(m.replace(",", "")) for m in AMOUNT.findall(text)]


def _percents(text: str) -> set[Decimal]:
    found = set()
    for a, b in PERCENT.findall(text):
        value = Decimal(a or b)
        if 0 < value <= 28:
            found.add(value.normalize())
    return found


def bill_gst(invoice: dict, tax_total: Decimal) -> dict:
    """What GST the bill charges, read off its tax rows.

    CGST+SGST counts as charged when a CGST/SGST row carries a rate, or when
    two equal halves of the GRPO's tax are printed. IGST when an IGST row
    carries a rate or the whole tax. A header that only names "SGST" over a
    value column does not count; nor does the interest clause's 18%.
    """
    rows = [row for row in _rows(invoice) if not NOT_TAX.search(row)]
    amounts = [amount for row in rows for amount in _amounts(row)]
    half = tax_total / 2
    halves = sum(1 for amount in amounts if abs(amount - half) <= GST_AMOUNT_SLACK)
    total_printed = any(abs(amount - tax_total) <= GST_AMOUNT_SLACK for amount in amounts)
    kinds = set()
    if halves >= 2 or any(CGST_WORD.search(row) and _percents(row) for row in rows):
        kinds.add("CGST+SGST")
    if any(
        IGST_WORD.search(row)
        and (_percents(row) or any(abs(a - tax_total) <= GST_AMOUNT_SLACK for a in _amounts(row)))
        for row in rows
    ):
        kinds.add("IGST")
    rates = set()
    for row in rows:
        rates |= _percents(row)
    return {"kinds": kinds, "rates": rates, "total_printed": total_printed or halves >= 2}


def check_gst(grpo: dict, invoice: Optional[dict]) -> Finding:
    if invoice is None:
        return _finding("gst", CheckStatus.UNKNOWN, NOT_READ)

    tax_total = Decimal(grpo.get("tax_total") or 0)
    bill = bill_gst(invoice, tax_total)
    bill_kinds = " / ".join(sorted(bill["kinds"])) or "?"
    bill_rates = ", ".join(f"{_out(r)}%" for r in sorted(bill["rates"])) or "?"

    rows, wrong_kind, no_rate = [], [], []
    for line in grpo["lines"]:
        po_rate, po_kind = line.get("po_tax_rate"), tax_kind_of_code(line.get("po_tax_code"))
        if po_rate is None:
            # Not copied from a PO: hold the bill against the GRPO's own code.
            po_rate, po_kind = line["tax_rate"], tax_kind_of_code(line["tax_code"])
        po_rate = Decimal(po_rate)
        row = {
            "line": line["line_num"] + 1,
            "item_code": line["item_code"],
            "po_num": line.get("po_num") or "",
            "po_tax_code": line.get("po_tax_code") or line["tax_code"],
            "invoice_kind": bill_kinds,
            "invoice_rate": bill_rates,
        }
        rows.append(row)
        if bill["kinds"] and po_kind not in bill["kinds"]:
            wrong_kind.append(row)
        # A CGST+SGST bill prints each half's rate (2.5%), the total (5%), or both.
        wanted = {po_rate.normalize()} | ({(po_rate / 2).normalize()} if po_kind == "CGST+SGST" else set())
        if not wanted & bill["rates"]:
            no_rate.append(row)

    facts = {"lines": rows, "grpo_tax_total": _money(tax_total), "tax_total_on_bill": bill["total_printed"]}
    if wrong_kind:
        said = "; ".join(f"line {r['line']} ({r['item_code']}): PO {r['po_tax_code']}" for r in wrong_kind)
        return _finding("gst", CheckStatus.FAIL, f"The bill charges {bill_kinds}. {said}.", **facts)
    if no_rate and bill["rates"]:
        said = "; ".join(f"line {r['line']} ({r['item_code']}): PO {r['po_tax_code']}" for r in no_rate)
        return _finding("gst", CheckStatus.FAIL, f"The bill shows GST at {bill_rates}. {said}.", **facts)
    if no_rate or not bill["kinds"]:
        return _finding(
            "gst", CheckStatus.REVIEW,
            "The bill's GST could not be read clearly off the scan; compare it by eye.", **facts,
        )
    if not bill["total_printed"]:
        return _finding(
            "gst", CheckStatus.REVIEW,
            f"The bill charges {bill_kinds} at {bill_rates} like the PO, but the GRPO's GST of "
            f"{_money(tax_total)} is not printed on it. Check the amounts.",
            **facts,
        )
    return _finding(
        "gst", CheckStatus.PASS,
        f"The bill charges {bill_kinds} at {bill_rates}, {_money(tax_total)}, as the PO.", **facts,
    )


# ---------------------------------------------------------------------------
# 4. GRPO within N days of arrival
# ---------------------------------------------------------------------------

def parse_stamp_date(text: str) -> Optional[date]:
    """A handwritten gate-stamp date, day first: "3/10/26", "01-10-2026"."""
    match = re.search(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{2,4})", text or "")
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def check_grpo_timing(grpo: dict, invoice: Optional[dict], arrival: Optional[dict]) -> Finding:
    """``arrival`` is ``{"at": datetime|date, "source": str}`` from the gate, or None."""
    made = grpo.get("created_on") or grpo.get("doc_date")
    facts: dict[str, Any] = {
        "grpo_created_on": _iso(made),
        "grpo_posting_date": _iso(grpo.get("doc_date")),
    }
    arrived, source = None, ""
    if arrival and arrival.get("at"):
        at = arrival["at"]
        arrived = at.date() if isinstance(at, datetime) else at
        source = arrival.get("source") or "the gate entry"
    elif invoice is not None:
        arrived = parse_stamp_date(invoice.get("gate_stamp_date"))
        source = "the gate stamp on the bill" if arrived else ""
    facts.update({"arrived_on": _iso(arrived), "arrival_source": source})

    if made is None:
        return _finding("grpo_timing", CheckStatus.UNKNOWN, "SAP gave no GRPO date.", **facts)
    if arrived is None:
        if invoice is None:
            return _finding(
                "grpo_timing", CheckStatus.UNKNOWN,
                "The truck's gate entry is not in the app; read the invoice to use its gate stamp.",
                **facts,
            )
        return _finding(
            "grpo_timing", CheckStatus.REVIEW,
            "No arrival date: the gate entry is not in the app and the bill's gate stamp has no readable date.",
            **facts,
        )
    days = (made - arrived).days
    facts["days"] = days
    if days < 0:
        return _finding(
            "grpo_timing", CheckStatus.REVIEW,
            f"The arrival date ({arrived:%d %b %Y}, {source}) is after the GRPO was made "
            f"({made:%d %b %Y}). One of the dates is wrong.",
            **facts,
        )
    said = f"Arrived {arrived:%d %b %Y} ({source}), GRPO made {made:%d %b %Y}: {days} day{'s' if days != 1 else ''}."
    if days <= GRPO_DAYS_AFTER_ARRIVAL:
        return _finding("grpo_timing", CheckStatus.PASS, said, **facts)
    return _finding("grpo_timing", CheckStatus.FAIL, said, **facts)


# ---------------------------------------------------------------------------
# 5. Rate Check signature
# ---------------------------------------------------------------------------

def check_rate_check_signature(invoice: Optional[dict]) -> Finding:
    """From ``invoice["rate_check"]``: the ink left on the stamp's Rate Check line
    once its printed rule is erased (``invoice_reader.rate_check_marks``)."""
    if invoice is None:
        return _finding("rate_check_signature", CheckStatus.UNKNOWN, NOT_READ)
    marks = invoice.get("rate_check") or {}
    facts = {"ink": marks.get("ink"), "written": marks.get("text") or ""}
    if not marks.get("found"):
        return _finding(
            "rate_check_signature", CheckStatus.REVIEW,
            "The Rate Check stamp could not be found on the scan. Check the paper.", **facts,
        )
    signed = marks.get("signed")
    if signed is False:
        return _finding(
            "rate_check_signature", CheckStatus.FAIL, "Nobody has signed the Rate Check line.", **facts,
        )
    if signed is None:
        return _finding(
            "rate_check_signature", CheckStatus.REVIEW,
            "There are faint marks on the Rate Check line. Check the paper.", **facts,
        )
    written = facts["written"]
    if RATE_CHECKER.lower()[:3] in written.lower():
        return _finding(
            "rate_check_signature", CheckStatus.PASS, f"Signed on the Rate Check line: \"{written}\".", **facts,
        )
    return _finding(
        "rate_check_signature", CheckStatus.REVIEW,
        f"The Rate Check line is signed. Confirm it is {RATE_CHECKER}'s.", **facts,
    )


# ---------------------------------------------------------------------------
# 6. PO approver
# ---------------------------------------------------------------------------

def _name_tokens(name: str) -> set:
    return set(re.findall(r"[a-z]+", (name or "").lower()))


def check_po_approver(po_approvals: Optional[list]) -> Finding:
    """``po_approvals``: ``[{"po_num", "is_approved", "approver"}]`` as the PO
    print shows them, or None when they could not be read."""
    if po_approvals is None:
        return _finding("po_approver", CheckStatus.UNKNOWN, "Could not read the POs from SAP.")
    if not po_approvals:
        return _finding("po_approver", CheckStatus.REVIEW, "The GRPO is not based on a PO.", pos=[])
    wanted = _name_tokens(PO_APPROVER)
    problems = []
    for po in po_approvals:
        if not po.get("is_approved"):
            problems.append(f"PO {po['po_num']} is not approved in SAP")
        elif not wanted <= _name_tokens(po.get("approver")):
            problems.append(f"PO {po['po_num']} prints {po.get('approver') or 'no approver'}")
    if problems:
        return _finding("po_approver", CheckStatus.FAIL, "; ".join(problems) + ".", pos=po_approvals)
    numbers = ", ".join(po["po_num"] for po in po_approvals)
    printed = " / ".join(dict.fromkeys(po["approver"] for po in po_approvals))
    return _finding(
        "po_approver", CheckStatus.PASS,
        f"PO {numbers} approved; the print names {printed}.", pos=po_approvals,
    )


# ---------------------------------------------------------------------------
# 7. PO rate vs GRPO rate
# ---------------------------------------------------------------------------

def check_po_rate(grpo: dict) -> Finding:
    rows, off, loose = [], [], []
    for line in grpo["lines"]:
        row = {
            "line": line["line_num"] + 1,
            "item_code": line["item_code"],
            "po_num": line.get("po_num") or "",
            "grpo_price": _out(line["price"], 4),
            "po_price": _out(line.get("po_price"), 4),
        }
        rows.append(row)
        if line.get("po_price") is None:
            loose.append(row)
            continue
        # Equal to the paisa: a PO priced at 3.3217 and a bill at 3.321 agree.
        if line["price"].quantize(PAISA) != Decimal(line["po_price"]).quantize(PAISA):
            off.append(row)
    if off:
        said = "; ".join(f"line {r['line']} ({r['item_code']}): GRPO {r['grpo_price']}, PO {r['po_price']}" for r in off)
        return _finding("po_rate", CheckStatus.FAIL, f"Rate differs. {said}.", lines=rows)
    if loose:
        return _finding(
            "po_rate", CheckStatus.REVIEW,
            f"{len(loose)} line(s) not copied from a PO, so there is no PO rate to hold them to.", lines=rows,
        )
    if not rows:
        return _finding("po_rate", CheckStatus.UNKNOWN, "The GRPO has no lines.", lines=rows)
    return _finding("po_rate", CheckStatus.PASS, "Every line is at the PO's rate.", lines=rows)


# ---------------------------------------------------------------------------
# 8. Over-receipt
# ---------------------------------------------------------------------------

def check_over_receipt(grpo: dict, vendor_exempt: bool) -> Finding:
    rows, over = [], []
    for line in grpo["lines"]:
        open_qty = line.get("po_open_qty")
        ceiling = open_qty * OVER_RECEIPT_TOLERANCE if open_qty is not None else None
        row = {
            "line": line["line_num"] + 1,
            "item_code": line["item_code"],
            "po_num": line.get("po_num") or "",
            "received": _out(line["quantity"], 3),
            "po_open": _out(open_qty, 3),
            "allowed": _out(ceiling, 3),
        }
        rows.append(row)
        if ceiling is not None and line["quantity"] > ceiling:
            over.append(row)
    if vendor_exempt:
        return _finding(
            "over_receipt", CheckStatus.PASS,
            "This vendor is exempt from the 10% limit, as on the gate.", lines=rows, vendor_exempt=True,
        )
    if over:
        said = "; ".join(f"line {r['line']} ({r['item_code']}): {r['received']} received, {r['allowed']} allowed" for r in over)
        return _finding("over_receipt", CheckStatus.FAIL, f"Over PO + 10%. {said}.", lines=rows)
    if not any(row["po_open"] is not None for row in rows):
        return _finding(
            "over_receipt", CheckStatus.REVIEW, "The GRPO is not based on a PO.", lines=rows,
        )
    return _finding(
        "over_receipt", CheckStatus.PASS,
        "Every line is within 110% of what was open on its PO line.", lines=rows,
    )


# ---------------------------------------------------------------------------
# 9. QC
# ---------------------------------------------------------------------------

def check_qc(qc_items: Optional[list]) -> Finding:
    """``qc_items``: ``[{"po_num", "item_code", "status", "report_no"}]`` from the
    app's QC records for the truck, or None when the GRPO was not received here."""
    if qc_items is None:
        return _finding(
            "qc", CheckStatus.REVIEW,
            "The GRPO was not received through the app, so there is no QC record here. Check QC by hand.",
        )
    if not qc_items:
        return _finding("qc", CheckStatus.REVIEW, "No PO items were found for this GRPO's truck.", items=[])
    failed = [i for i in qc_items if i["status"] in QC_NOT_PASSED]
    missing = [i for i in qc_items if i["status"] == "NO_ARRIVAL_SLIP"]
    if failed:
        said = "; ".join(f"{i['item_code']} (PO {i['po_num']}): {i['status']}" for i in failed)
        return _finding("qc", CheckStatus.FAIL, f"Not approved. {said}.", items=qc_items)
    if missing:
        said = ", ".join(i["item_code"] for i in missing)
        return _finding(
            "qc", CheckStatus.REVIEW, f"No QC inspection is on record for {said}. Confirm QC by hand.",
            items=qc_items,
        )
    reports = ", ".join(i["report_no"] for i in qc_items if i.get("report_no"))
    return _finding(
        "qc", CheckStatus.PASS,
        f"Accepted by QC{f' (report {reports})' if reports else ''}.", items=qc_items,
    )


# ---------------------------------------------------------------------------

def run_checks(grpo: dict, invoice: Optional[dict], context: dict) -> list[Finding]:
    """All nine findings, in checklist order. ``invoice`` is None until read."""
    return [
        check_invoice_number(grpo, invoice),
        check_warehouse(grpo),
        check_gst(grpo, invoice),
        check_grpo_timing(grpo, invoice, context.get("arrival")),
        check_rate_check_signature(invoice),
        check_po_approver(context.get("po_approvals")),
        check_po_rate(grpo),
        check_over_receipt(grpo, bool(context.get("vendor_exempt"))),
        check_qc(context.get("qc_items")),
    ]


def unknown_findings(reason: str) -> list[Finding]:
    """Every check at UNKNOWN, for when the GRPO itself could not be read."""
    return [_finding(key, CheckStatus.UNKNOWN, reason) for key in ORDER]


def _out(value, places: int = 2) -> Optional[str]:
    if value is None:
        return None
    value = Decimal(value)
    text = f"{value:.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _money(value) -> str:
    """₹1,83,898.05 — lakhs and crores grouped as on the bills."""
    whole, paise = f"{Decimal(value):.2f}".split(".")
    sign, whole = ("-", whole[1:]) if whole.startswith("-") else ("", whole)
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}₹{','.join(groups + [tail]) if groups else tail}.{paise}"


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None
