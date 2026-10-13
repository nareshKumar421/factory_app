"""Marking cash-sale bills out at the gate.

The bills come from the same book the A/R Invoices cash-sale screen reads —
the cash-sale customers' invoices in SAP, whether the counter raised them in
SAP or through the app (``HanaARInvoiceReader.cash_sale_invoices``) — so the
gate and the counter never disagree about what a cash sale is.

A bill is "still inside" until it is on an active cash sale out, or on a
docking (a cash sale big enough to go by truck goes the normal way and is
marked out with that truck). A cancelled bill is never offered: nothing left
the factory against it.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from gate_core.models import (
    CashSaleGateOut,
    CashSaleGateOutBill,
    CashSaleGateOutStatus,
    SalesDispatchDocumentType,
    SalesDispatchGateOutDocument,
)
from gate_core.models.sales_dispatch import ACTIVE_DOCUMENT_STATUSES
from sap_client.exceptions import SAPConnectionError, SAPDataError

# Counter goods leave the day they are billed, the odd one the day after. A
# week covers that with room to spare, and keeps the list from offering every
# cash bill raised before the gate started recording them.
PENDING_BILL_DAYS = 7
PENDING_BILL_LIMIT = 300


class CashSaleGateOutError(Exception):
    """A request the gate must refuse, with a message the guard can act on."""


def _cash_sale_book(company):
    """The SAP client and cash-sale CardCodes for one company.

    Imported here, not at module scope: the cash-sale book belongs to A/R
    Invoices, and the gate only borrows its reading of it.
    """
    from ar_invoice.services import ARInvoiceService

    service = ARInvoiceService(company.code)
    return service.sap(), service.cash_sale_customer_codes()


def _item_summary(lines):
    parts = []
    for line in lines:
        name = line.get("description") or line.get("item_code") or "Item"
        quantity = Decimal(str(line.get("quantity") or 0)).normalize()
        uom = line.get("uom") or ""
        parts.append(f"{name} x {quantity:f}{f' {uom}' if uom else ''}")
    return "; ".join(parts)


def _bill_row(company, invoice):
    """One SAP cash-sale invoice, shaped for the picker and the snapshot."""
    lines = invoice.get("lines") or []
    return {
        "company_id": company.id,
        "company_code": company.code,
        "company_name": company.name,
        "doc_entry": invoice["doc_entry"],
        "doc_num": invoice.get("doc_num"),
        "doc_date": invoice.get("doc_date"),
        "customer_code": invoice.get("customer_code") or "",
        "customer_name": invoice.get("customer_name") or "",
        "customer_ref": invoice.get("customer_ref") or "",
        "comments": invoice.get("comments") or "",
        "doc_total": invoice.get("doc_total"),
        "branch_name": invoice.get("branch_name") or "",
        "item_summary": _item_summary(lines),
        "total_quantity": sum(Decimal(str(line.get("quantity") or 0)) for line in lines),
        "line_count": len(lines),
    }


def bills_already_out(company_id, doc_entries):
    """Of these bills, the ones already on an active cash sale out."""
    return set(
        CashSaleGateOutBill.objects.filter(
            company_id=company_id, sap_doc_entry__in=list(doc_entries), is_active=True
        ).values_list("sap_doc_entry", flat=True)
    )


def bills_on_dockings(company_id, doc_entries):
    """Of these bills, the ones loaded on a live (or dispatched) docking."""
    return set(
        SalesDispatchGateOutDocument.objects.filter(
            company_id=company_id,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry__in=list(doc_entries),
            is_active=True,
            sales_dispatch__is_active=True,
            sales_dispatch__status__in=ACTIVE_DOCUMENT_STATUSES,
        ).values_list("sap_doc_entry", flat=True)
    )


def pending_bills(companies, search=None, days=PENDING_BILL_DAYS):
    """The cash-sale bills of these companies not yet out of the gate.

    One company's SAP being down must not blank the others' bills, so a
    failure is reported per company beside whatever did come back.
    """
    date_to = timezone.localdate()
    date_from = date_to - timedelta(days=max(int(days), 0))
    bills, errors, truncated = [], [], False

    for company in companies:
        try:
            sap, card_codes = _cash_sale_book(company)
            invoices = sap.ar_cash_sale_invoices(
                card_codes=card_codes,
                date_from=str(date_from),
                date_to=str(date_to),
                search=(search or "").strip() or None,
                limit=PENDING_BILL_LIMIT,
            )
        except (SAPConnectionError, SAPDataError) as exc:
            errors.append(
                {"company_code": company.code, "company_name": company.name, "detail": str(exc)}
            )
            continue

        truncated = truncated or len(invoices) >= PENDING_BILL_LIMIT
        live = [invoice for invoice in invoices if not invoice.get("is_cancelled")]
        entries = [invoice["doc_entry"] for invoice in live]
        gone = bills_already_out(company.id, entries) | bills_on_dockings(company.id, entries)
        bills.extend(
            _bill_row(company, invoice) for invoice in live if invoice["doc_entry"] not in gone
        )

    return {
        "date_from": str(date_from),
        "date_to": str(date_to),
        "bills": bills,
        "errors": errors,
        "truncated": truncated,
    }


def _doc_label(rows_by_entry, doc_entries):
    return ", ".join(
        str((rows_by_entry.get(entry) or {}).get("doc_num") or entry)
        for entry in sorted(doc_entries)
    )


def mark_out(
    *,
    user,
    company,
    doc_entries,
    taken_by_name,
    taken_by_mobile="",
    vehicle_no="",
    remarks="",
):
    """Record these bills as gone out with this person, and return the entry.

    The bills are read back from SAP rather than taken from the request: the
    snapshot is what the board shows from then on, and only SAP can say the
    bill is a live cash sale of this company.
    """
    doc_entries = sorted({int(entry) for entry in doc_entries})
    if not doc_entries:
        raise CashSaleGateOutError("Pick at least one bill.")

    sap, card_codes = _cash_sale_book(company)
    invoices = sap.ar_cash_sale_invoices(
        card_codes=card_codes, doc_entries=doc_entries, limit=len(doc_entries)
    )
    by_entry = {invoice["doc_entry"]: invoice for invoice in invoices}

    missing = [entry for entry in doc_entries if entry not in by_entry]
    if missing:
        raise CashSaleGateOutError(
            f"Not a cash-sale bill of {company.name} in SAP: "
            f"{', '.join(str(entry) for entry in missing)}."
        )
    cancelled = [entry for entry in doc_entries if by_entry[entry].get("is_cancelled")]
    if cancelled:
        raise CashSaleGateOutError(
            f"Cancelled in SAP, so nothing can go out against it: "
            f"{_doc_label(by_entry, cancelled)}."
        )
    on_dockings = bills_on_dockings(company.id, doc_entries)
    if on_dockings:
        raise CashSaleGateOutError(
            f"Already on a truck in Docking — mark it out with that truck: "
            f"{_doc_label(by_entry, on_dockings)}."
        )

    rows = [_bill_row(company, by_entry[entry]) for entry in doc_entries]
    vehicle_no = "".join((vehicle_no or "").split()).upper()

    # Two guards marking the same bill at once, or two entries drawing the same
    # number: the constraints catch both. A taken bill is the guard's to hear
    # about; a taken number is only ours, so draw the next one and go again.
    for _attempt in range(3):
        already_out = bills_already_out(company.id, doc_entries)
        if already_out:
            raise CashSaleGateOutError(
                f"Already marked out: {_doc_label(by_entry, already_out)}."
            )
        try:
            with transaction.atomic():
                entry = CashSaleGateOut.objects.create(
                    company=company,
                    entry_no=CashSaleGateOut.generate_entry_no(),
                    taken_by_name=taken_by_name.strip(),
                    taken_by_mobile=(taken_by_mobile or "").strip(),
                    vehicle_no=vehicle_no,
                    remarks=(remarks or "").strip(),
                    created_by=user,
                    updated_by=user,
                )
                CashSaleGateOutBill.objects.bulk_create(
                    [
                        CashSaleGateOutBill(
                            gate_out=entry,
                            company=company,
                            sap_doc_entry=row["doc_entry"],
                            sap_doc_num=row["doc_num"],
                            doc_date=row["doc_date"] or None,
                            customer_code=row["customer_code"][:50],
                            customer_name=row["customer_name"][:200],
                            customer_ref=row["customer_ref"][:100],
                            doc_total=row["doc_total"],
                            item_summary=row["item_summary"],
                            total_quantity=row["total_quantity"],
                        )
                        for row in rows
                    ]
                )
            return entry
        except IntegrityError:
            continue
    raise CashSaleGateOutError("Could not save the entry. Please try again.")


def cancel(*, user, entry, reason):
    """Void a cash sale out, freeing its bills to be marked out again."""
    if entry.status == CashSaleGateOutStatus.CANCELLED:
        raise CashSaleGateOutError("This entry is already cancelled.")
    with transaction.atomic():
        entry.status = CashSaleGateOutStatus.CANCELLED
        entry.cancel_reason = reason.strip()
        entry.cancelled_by = user
        entry.cancelled_at = timezone.now()
        entry.updated_by = user
        entry.save(
            update_fields=[
                "status",
                "cancel_reason",
                "cancelled_by",
                "cancelled_at",
                "updated_by",
                "updated_at",
            ]
        )
        entry.bills.update(is_active=False)
    return entry
