"""Checking the bills handed out from the copy, once SAP answers again.

While HANA was down, dispatch worked from a copy that could be up to 15
minutes old -- and SAP itself might have been reachable to the billing desk the
whole time. So every bill handed out from the copy (``ServedBill``) is compared,
after the next successful refresh, with what SAP now holds:

* unchanged -- or changed only in the dispatch stamp, which the app writes
  itself at gate-out -- is marked so and nobody is bothered;
* cancelled, credited, or changed in what was loaded (customer, ship-to,
  GSTIN, total, items, quantities, warehouses) is told to whoever started work
  on it from the copy (the docking, barcode session, bill summary or short
  dispatch) and to the SAP alert recipients, naming those records and whether
  the truck has already left.

It tells; it does not stop anything. Holding a gatepass is a decision for the
person reading the alert.
"""

import logging

from django.utils import timezone

from .codec import unpack
from .models import MirroredBill, ServedBill, ServedBillOutcome

logger = logging.getLogger(__name__)

#: Header fields that change what was loaded or where it goes. The dispatch
#: stamp (dates, bilty, vehicle, transporter, e-way bill) is left out: the app
#: writes it at gate-out, and SAP filling it in later is the bill moving on.
HEADER_FIELDS = (
    ("card_code", "Customer"),
    ("ship_to_code", "Ship-to"),
    ("ship_to_address", "Ship-to address"),
    ("state", "State"),
    ("bp_gstin", "GSTIN"),
    ("branch_id", "Branch"),
)
LINE_FIELDS = (("item_code", "item"), ("quantity", "quantity"), ("warehouse_code", "warehouse"))


def recheck_served(company, reader, now=None):
    """Compare this company's pending served bills with SAP; returns them checked."""
    now = now or timezone.now()
    pending = list(ServedBill.objects.filter(company=company, outcome=ServedBillOutcome.PENDING))
    if not pending:
        return []

    entries = [served.doc_entry for served in pending]
    current = {
        row.doc_entry: (unpack(row.bill), unpack(row.lines), row.credited)
        for row in MirroredBill.objects.filter(company=company, doc_entry__in=entries)
    }
    # Not in the copy: cancelled, or older than its window. Ask SAP which.
    missing = [entry for entry in entries if entry not in current]
    if missing:
        live = reader.list_bills({"doc_entries": missing, "limit": len(missing)})
        lines = reader.list_bill_lines_for([bill["doc_entry"] for bill in live])
        credited = reader.credited_doc_entries(min(_created(bill) for bill in live)) if live else set()
        for bill in live:
            entry = bill["doc_entry"]
            current[entry] = (bill, lines.get(entry, []), entry in credited)

    checked = []
    for served in pending:
        was = unpack(served.served)
        if served.doc_entry not in current:
            outcome, differences = ServedBillOutcome.CANCELLED, ["Cancelled in SAP."]
        else:
            bill, lines, credited = current[served.doc_entry]
            differences = compare(was["bill"], was["lines"], bill, lines)
            if credited:
                outcome = ServedBillOutcome.CREDITED
                differences = ["A credit note in SAP is now based on it."] + differences
            else:
                outcome = ServedBillOutcome.CHANGED if differences else ServedBillOutcome.UNCHANGED
        served.outcome = outcome
        served.checked_at = now
        served.differences = differences
        if outcome != ServedBillOutcome.UNCHANGED:
            served.linked = linked_records(company, served.doc_entry)
            served.notified = notify(served, was["bill"])
        served.save(update_fields=["outcome", "checked_at", "differences", "linked", "notified"])
        checked.append(served)
    flagged = sum(1 for served in checked if served.outcome != ServedBillOutcome.UNCHANGED)
    logger.warning(
        "Re-checked %s bills served from the SAP copy for %s: %s flagged",
        len(checked), company.code, flagged,
    )
    return checked


def _created(bill):
    from datetime import date

    return date.fromisoformat(bill.get("create_date") or "2000-01-01")


def compare(was_bill, was_lines, bill, lines):
    """What changed in what was loaded, as lines a person can read."""
    differences = []
    for field, label in HEADER_FIELDS:
        before, after = was_bill.get(field), bill.get(field)
        if (before or "") != (after or ""):
            differences.append(f"{label}: {before or '-'} → {after or '-'}")
    if abs(float(was_bill.get("doc_total") or 0) - float(bill.get("doc_total") or 0)) >= 0.01:
        differences.append(f"Bill total: {was_bill.get('doc_total')} → {bill.get('doc_total')}")

    before = {line["line_num"]: line for line in was_lines}
    after = {line["line_num"]: line for line in lines}
    for num in sorted(set(before) | set(after)):
        old, new = before.get(num), after.get(num)
        label = f"Line {num + 1}"
        if new is None:
            differences.append(f"{label} ({old.get('item_code')}) removed")
        elif old is None:
            differences.append(
                f"{label} added: {new.get('item_code')} {_qty(new.get('quantity'))} {new.get('uom') or ''}".rstrip()
            )
        else:
            for field, name in LINE_FIELDS:
                if _value(old.get(field)) != _value(new.get(field)):
                    differences.append(
                        f"{label} ({old.get('item_code')}): {name} "
                        f"{_qty(old.get(field))} → {_qty(new.get(field))}"
                    )
    return differences


def _value(value):
    return round(float(value), 3) if isinstance(value, (int, float)) else (value or "")


def _qty(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value if value not in (None, "") else "-")


# ---------------------------------------------------------------------------
# who started work on it, and telling them
# ---------------------------------------------------------------------------

def linked_records(company, doc_entry):
    """The app records that took this bill: what they are, their state, who made them.

    Each source is read on its own, so one model changing shape costs that
    source, not the alert.
    """
    found = []

    def collect(read):
        try:
            found.extend(read())
        except Exception:  # noqa: BLE001
            logger.exception("Could not read records linked to bill %s", doc_entry)

    def dockings():
        from django.db.models import Q

        from gate_core.models.sales_dispatch import SalesDispatchGateOut

        rows = SalesDispatchGateOut.objects.filter(company=company).filter(
            Q(document_type="INVOICE", sap_doc_entry=doc_entry)
            | Q(documents__sap_doc_entry=doc_entry)
        ).distinct()
        return [
            {
                "what": f"Docking {row.entry_no}",
                "status": row.get_status_display(),
                "left": row.status == "DISPATCHED",
                "user_id": row.created_by_id,
                "link": f"/dispatch/docking/{row.pk}",
            }
            for row in rows
        ]

    def barcode_sessions():
        from barcode.models import DispatchSession

        rows = DispatchSession.objects.filter(company=company, sap_doc_entry=str(doc_entry))
        return [
            {
                "what": f"Barcode dispatch of bill {row.bill_number}",
                "status": row.get_status_display(),
                "left": False,
                "user_id": getattr(row, "created_by_id", None),
                "link": "",
            }
            for row in rows
        ]

    def bill_summaries():
        from dispatch_plans.models_bill_summary import BillSummary

        rows = BillSummary.objects.filter(company=company, sap_invoice_doc_entry=doc_entry)
        return [
            {
                "what": f"Bill summary {row.entry_no}",
                "status": row.get_status_display() if hasattr(row, "get_status_display") else "",
                "left": False,
                "user_id": getattr(row, "created_by_id", None),
                "link": "",
            }
            for row in rows
        ]

    def short_dispatches():
        from short_dispatch.models import ShortDispatch

        rows = ShortDispatch.objects.filter(company=company, sap_invoice_doc_entry=doc_entry)
        return [
            {
                "what": f"Short dispatch {row.entry_no}",
                "status": row.get_status_display(),
                "left": False,
                "user_id": row.created_by_id,
                "link": f"/warehouse/short-dispatch/{row.pk}",
            }
            for row in rows
        ]

    for read in (dockings, barcode_sessions, bill_summaries, short_dispatches):
        collect(read)
    return found


def notify(served, was_bill):
    """Tell the people concerned; returns how many were told."""
    from django.contrib.auth import get_user_model

    from notifications.services import NotificationService
    from sap_client.health import _alert_recipients

    what = {
        ServedBillOutcome.CANCELLED: "was cancelled in SAP",
        ServedBillOutcome.CREDITED: "has a credit note against it in SAP",
        ServedBillOutcome.CHANGED: "was changed in SAP",
    }[served.outcome]
    customer = was_bill.get("card_name") or was_bill.get("card_code") or ""
    title = f"Bill {served.doc_num} {what}"
    as_of = timezone.localtime(served.copy_as_of).strftime("%d %b %H:%M") if served.copy_as_of else "?"
    parts = [
        f"Bill {served.doc_num} ({customer}) was handed out from the SAP copy of {as_of} "
        f"while SAP was down, and {what}."
    ]
    if served.differences and served.outcome != ServedBillOutcome.CANCELLED:
        parts.append("; ".join(served.differences[:6]))
    left = [record for record in served.linked if record["left"]]
    waiting = [record for record in served.linked if not record["left"]]
    if waiting:
        parts.append(
            "Started from it: "
            + ", ".join(f"{r['what']} ({r['status']})" for r in waiting)
            + ". Check before it goes further."
        )
    if left:
        parts.append(
            "Already gone: " + ", ".join(r["what"] for r in left)
            + ". The truck has left with this bill."
        )
    if not served.linked:
        parts.append("Nothing in the app was started from it.")
    body = " ".join(parts)
    link = next((record["link"] for record in served.linked if record["link"]), "")

    users = {user.pk: user for user in _alert_recipients()}
    starters = [r["user_id"] for r in served.linked if r.get("user_id")]
    for user in get_user_model().objects.filter(pk__in=starters, is_active=True):
        users[user.pk] = user
    told = 0
    for user in users.values():
        try:
            NotificationService.send_notification_to_user(
                user=user, title=title[:255], body=body[:1000], click_action_url=link,
                reference_type="sap_served_bill", reference_id=served.pk,
                company=served.company,
            )
            told += 1
        except Exception:  # noqa: BLE001
            logger.exception("Could not tell %s about served bill %s", user.pk, served.pk)
    return told
