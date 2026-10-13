"""The exact JSON each SAP document is sent as (spec §2.6).

Pure functions: rows in, dicts out, no database and no SAP. The output is
JSON-native -- floats and ISO date strings -- so it can be stored on the row
(``po_payload`` / ``payload``) and sent as it is; ``json.dumps`` needs no
encoder.

``series=None`` means the ``Series`` key is left out: ``CASH_SAP_SEND_SERIES``
is off, or the send is simulated (a simulated send reads nothing from SAP, so
it cannot know the series).

Rules shared by every builder
-----------------------------
* A blank ``CostingCode*`` is omitted; ``CostingCode4`` is never sent.
* ``CostingCode2`` is the voucher's bill month (``MM-YYYY``), on the PO line
  and again on the GRPO line, so both carry the bill's own month (D5).
* Comments end with the marker (``[JI-CB PO 42]``) and ``by <person>``; the
  marker always survives truncation to 254 characters, because it is how the
  document is found again after a timeout.

SAP's own checks these builders satisfy (``SBO_SP_TransactionNotification``
of JIVO_OIL and JIVO_BEVERAGES, read on 9 Oct 2026):

* 2200012 -- a PO whose Comments are not upper case is refused, so the PO's
  Comments are always upper case (the marker is upper case already, so it is
  still found).
* 220001/3/4, 200001/7/9, 1120001/8/9 -- every expense line of a PO, a GRPO
  and an A/P draft needs Dim1 (variety), Dim2 (month) and Dim3 (budget), and a
  "Factory"/"FACT_COM" budget needs SAP branch 2. The A/P service draft
  therefore carries Dim3 too; head office may still change it before adding.
* 1120018 / 180023 -- an A/P total must be whole rupees, so a draft whose
  amount has paise asks SAP to round it (``Rounding``), as head office does.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable, List, Optional

from rest_framework.exceptions import ValidationError

from sap_purchasing.readback import po_line_free_text

from .constants import (
    COMMENTS_MAX,
    GNO_SEPARATOR,
    ITEM_DESCRIPTION_MAX,
    MAX_DOT_SUFFIX,
    NUM_AT_CARD_MAX,
)

MONTH_ABBR = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


# ---------------------------------------------------------------------------
# Small formatters
# ---------------------------------------------------------------------------


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def format_amount(amount) -> str:
    """``350.00`` -> ``"350"``, ``350.50`` -> ``"350.50"`` (the NumAtCard style)."""
    value = _dec(amount).quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return str(int(value))
    return f"{value:.2f}"


def iso(day) -> Optional[str]:
    if day is None:
        return None
    if isinstance(day, datetime):
        day = day.date()
    if isinstance(day, date):
        return day.isoformat()
    return str(day)


def month_code(day: date) -> str:
    """Dim2: ``"09-2026"``."""
    return f"{day.month:02d}-{day.year}"


def grpo_num_at_card(amount, bill_date: date) -> str:
    """``"500/24-09-26"``: amount / bill date, as USER22 keys it."""
    return f"{format_amount(amount)}/{bill_date:%d-%m-%y}"


def ap_num_at_card(month: date, bunch_total, amount) -> str:
    """``"SEP 26/4050/800"``: month, the envelope's total, this document's amount.

    The month name is spelt here rather than with ``%b``, which follows the
    server's locale.
    """
    return (
        f"{MONTH_ABBR[month.month - 1]} {month:%y}"
        f"/{format_amount(bunch_total)}/{format_amount(amount)}"
    )


def compose_comments(text: str, marker: str, by: str = "", *, upper: bool = False) -> str:
    """``text`` cut so `` <marker> by <by>`` always fits in 254 characters.

    The person's name is dropped before the marker is ever cut: a very long
    name loses itself, never the reference.
    """
    tail = f" {marker}" + (f" by {by}" if by else "")
    if len(tail) > 120:
        tail = f" {marker}"
    out = ((text or "")[: COMMENTS_MAX - len(tail)] + tail).strip()
    return out.upper() if upper else out


def person_label(user) -> str:
    """The name SAP's Comments carry for whoever pressed Post (D15)."""
    if user is None:
        return ""
    name = (getattr(user, "full_name", "") or "").strip()
    if not name and hasattr(user, "get_full_name"):
        try:
            name = (user.get_full_name() or "").strip()
        except Exception:  # noqa: BLE001 -- a label must never fail a post
            name = ""
    return name or (getattr(user, "email", "") or "")


def next_free_num_at_card(base: str, is_taken: Callable[[str], bool]) -> str:
    """``base``, ``base.``, ``base..`` ... the first one ``is_taken`` says is free.

    SAP's own users suffix a repeated vendor reference with ``.``; doing the
    same keeps the two apart without inventing a new format.
    """
    for dots in range(MAX_DOT_SUFFIX + 1):
        candidate = base + "." * dots
        if len(candidate) > NUM_AT_CARD_MAX:
            break
        if not is_taken(candidate):
            return candidate
    raise ValidationError({
        "detail": f"The vendor reference {base} is already used {MAX_DOT_SUFFIX + 1} "
                  f"times for this vendor. Change the bill date or amount, or key it "
                  f"in SAP by hand."
    })


def _dims(line: dict, v) -> dict:
    for key, value in (
        ("CostingCode", v.ocr_code),
        ("CostingCode2", v.ocr_code2),
        ("CostingCode3", v.ocr_code3),
        ("CostingCode5", v.ocr_code5),
    ):
        if value:
            line[key] = value
    return line


def _header(card_code: str, bpl_id, series) -> dict:
    payload = {"CardCode": card_code}
    if bpl_id is not None:
        payload["BPL_IDAssignedToInvoice"] = int(bpl_id)
    if series is not None:
        payload["Series"] = int(series)
    return payload


# ---------------------------------------------------------------------------
# §2.6.1 Imprest PO
# ---------------------------------------------------------------------------


def build_po_payload(batch, vouchers: Iterable, *, series, by: str, upper: bool) -> dict:
    """One PO line per ITEM voucher, quantity 1 x amount, in ``po_line_num`` order.

    ``FreeText`` = ``"JI V<entry id>"`` is how each voucher finds its line again
    (``map_lines_by_free_text``). No NumAtCard on the PO. The Comments are
    upper case whatever ``upper`` says: SAP refuses a PO otherwise (2200012).
    """
    vouchers = list(vouchers)
    count = len(vouchers)
    payload = _header(batch.vendor_code, batch.bpl_id, series)
    payload["DocDate"] = iso(batch.po_doc_date)
    payload["TaxDate"] = iso(batch.po_doc_date)
    payload["DocDueDate"] = iso(batch.po_due_date)
    text = (
        f"Imprest bunch {batch.bunch.number} · {count} voucher{'s' if count != 1 else ''}"
        f" · {batch.sap_company_code}"
    )
    payload["Comments"] = compose_comments(text, batch.marker, by, upper=True)
    lines = []
    for v in vouchers:
        line = {
            "ItemCode": v.item_code,
            "Quantity": 1,
            "UnitPrice": float(_dec(v.amount)),
            "WarehouseCode": v.warehouse_code,
            "AccountCode": v.account_code,
            "TaxCode": v.tax_code,
        }
        _dims(line, v)
        line["ShipDate"] = iso(v.bill_date)
        line["FreeText"] = po_line_free_text(v.entry_id)
        lines.append(line)
    payload["DocumentLines"] = lines
    return payload


# ---------------------------------------------------------------------------
# §2.6.2 Imprest GRPO
# ---------------------------------------------------------------------------


def grpo_comments_text(grpo, batch) -> str:
    """``Based On Purchase Orders <PO>.\\rG.no.<n> Bill <b>`` -- SAP's own copy text."""
    po = batch.po_doc_num if batch.po_doc_num is not None else "(simulated)"
    text = f"Based On Purchase Orders {po}."
    gno = (grpo.gate_entry_no or "").strip()
    if gno:
        text += GNO_SEPARATOR + f"G.no.{gno}"
    bill = (grpo.supplier_bill_no or "").strip()
    if bill:
        text += f" Bill {bill}"
    return text


def build_grpo_payload(grpo, members: Iterable, batch, *, series, attachment_entry,
                       by: str, upper: bool) -> dict:
    """One line per member, each based on its own PO line (``BaseLine = po_line_num``).

    DocDate/DocDueDate are the posting date; TaxDate and the NumAtCard date stay
    the bill's (D5). ``AttachmentEntry`` is left out only when None.
    """
    payload = _header(batch.vendor_code, batch.bpl_id, series)
    payload["DocDate"] = iso(grpo.posting_date)
    payload["DocDueDate"] = iso(grpo.posting_date)
    payload["TaxDate"] = iso(grpo.bill_date)
    payload["NumAtCard"] = grpo.num_at_card
    payload["Comments"] = compose_comments(
        grpo_comments_text(grpo, batch), grpo.marker, by, upper=upper
    )
    if attachment_entry is not None:
        payload["AttachmentEntry"] = int(attachment_entry)
    lines = []
    for v in members:
        line = {
            "ItemCode": v.item_code,
            "Quantity": 1,
            "BaseType": 22,
            "BaseEntry": batch.po_doc_entry,
            "BaseLine": v.po_line_num,
            "WarehouseCode": v.warehouse_code,
            "UnitPrice": float(_dec(v.amount)),
            "TaxCode": v.tax_code,
            "AccountCode": v.account_code,
        }
        _dims(line, v)
        lines.append(line)
    payload["DocumentLines"] = lines
    return payload


# ---------------------------------------------------------------------------
# §2.6.3 / §2.6.4 A/P drafts
# ---------------------------------------------------------------------------


def _round_if_paise(payload: dict, amount) -> dict:
    """Ask SAP to round a total with paise (``Rounding``): SAP refuses an A/P
    whose total is not whole rupees (1120018 on the draft, 180023 on the A/P),
    and head office ticks the same box by hand today."""
    if amount is not None:
        value = _dec(amount)
        if value != value.to_integral_value():
            payload["Rounding"] = "tYES"
    return payload


def grpo_line_numbers(grpo, member_count: int) -> List[int]:
    """The GRPO's own line numbers: from ``line_map``, else 0..k-1."""
    mapped = sorted({int(n) for n in (grpo.line_map or {}).values() if n is not None})
    if mapped:
        return mapped
    return list(range(member_count))


def build_ap_item_draft_payload(draft, grpos: Iterable, *, series, by: str, upper: bool,
                                member_counts: Optional[dict] = None) -> dict:
    """An A/P item draft based on the batch's posted GRPOs, one line per GRPO line.

    ``member_counts`` (``{grpo id: members}``) is the fallback when a GRPO has
    no ``line_map``; it defaults to one line.
    """
    grpos = list(grpos)
    member_counts = member_counts or {}
    batch = draft.batch
    payload = {"DocObjectCode": "oPurchaseInvoices", "DocType": "dDocument_Items"}
    payload.update(_header(draft.vendor_code, batch.bpl_id, series if batch.bpl_id is not None else None))
    payload["DocDate"] = iso(draft.doc_date)
    payload["TaxDate"] = iso(draft.doc_date)
    payload["NumAtCard"] = draft.num_at_card
    text = "Based On Goods Receipt PO " + " ".join(f"{g.grpo_doc_num}." for g in grpos)
    payload["Comments"] = compose_comments(text, draft.marker, by, upper=upper)
    lines = []
    for g in grpos:
        for line_num in grpo_line_numbers(g, member_counts.get(g.id, 1)):
            lines.append({"BaseType": 20, "BaseEntry": g.grpo_doc_entry, "BaseLine": line_num})
    payload["DocumentLines"] = lines
    return _round_if_paise(payload, getattr(draft, "amount", None))


def service_heads(vouchers: Iterable) -> List[str]:
    seen, heads = set(), []
    for v in vouchers:
        name = (v.gl_account_name or v.gl_account_code or "").strip().upper()
        if name and name not in seen:
            seen.add(name)
            heads.append(name)
    return heads


def service_line_description(v) -> str:
    entry = v.entry
    serial = entry.serial_number or entry.id
    what = (entry.item or "").strip() or (entry.detail or "").strip()
    return f"Sr {serial} {what}".strip()[:ITEM_DESCRIPTION_MAX]


def build_ap_service_draft_payload(draft, vouchers: Iterable, *, series, by: str,
                                   upper: bool) -> dict:
    """An A/P service draft: one service line per SERVICE voucher.

    Dim3 (budget head) is filled from the routing like every other dimension:
    SAP refuses a draft line without it (1120009), so it can no longer be left
    for head office (the old D11). Head office may still change it before
    adding the draft. Branch and series are left out when the batch has no SAP
    branch.
    """
    vouchers = list(vouchers)
    batch = draft.batch
    payload = {"DocObjectCode": "oPurchaseInvoices", "DocType": "dDocument_Service"}
    payload.update(_header(draft.vendor_code, batch.bpl_id, series if batch.bpl_id is not None else None))
    payload["DocDate"] = iso(draft.doc_date)
    payload["TaxDate"] = iso(draft.doc_date)
    payload["NumAtCard"] = draft.num_at_card
    dated = f"{draft.doc_date:%d-%m-%Y}" if draft.doc_date else ""
    text = (
        f"BEING EXPENSE BOOKED AGAINST {', '.join(service_heads(vouchers))} AMOUNT "
        f"{format_amount(draft.amount)}/-, INVOICE NO. {draft.num_at_card}, DATED {dated}"
    )
    payload["Comments"] = compose_comments(text, draft.marker, by, upper=upper)
    lines = []
    for v in vouchers:
        line = {
            "ItemDescription": service_line_description(v),
            "AccountCode": v.account_code,
            "LineTotal": float(_dec(v.amount)),
            "TaxCode": v.tax_code,
        }
        _dims(line, v)
        lines.append(line)
    payload["DocumentLines"] = lines
    return _round_if_paise(payload, getattr(draft, "amount", None))
