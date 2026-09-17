"""
One-off export: bills planned in the app, against what SAP holds for them.

Read-only. Opens the app's Postgres for the dispatch plans and SAP's HANA for
the invoices, joins them on the SAP DocEntry the plan already stores, and writes
one spreadsheet. Writes nothing to either system.

Run:
    .venv/Scripts/python.exe export_planned_vs_sap.py
"""

import os

# Stand-alone rather than `manage.py shell`, which evaluates a piped file one
# line at a time and chokes on any indented block.
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from collections import defaultdict
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from company.models import Company
from dispatch_plans.models import DispatchPlan
from sap_client.context import CompanyContext
from sap_client.hana.connection import HanaConnection

# The month under review. Plans are windowed on the date somebody scheduled
# them for, which is the only date that makes "planned" mean anything.
SINCE = "2026-09-01"
UNTIL = "2026-09-30"
COMPANY_CODES = ["JIVO_OIL", "JIVO_MART"]

OUT = os.path.join(
    os.path.expanduser("~"), "Downloads", "planned_bills_vs_sap.xlsx"
)


def sap_invoices(company_code, doc_entries):
    """DocEntry -> what SAP holds, for the entries we were given.

    Chunked: HANA will not take an unbounded IN list, and the point of the
    export is that nothing is silently dropped.
    """
    if not doc_entries:
        return {}

    context = CompanyContext(company_code)
    connection = HanaConnection(context.hana)
    schema = connection.schema
    found = {}

    conn = connection.connect()
    try:
        cursor = conn.cursor()
        entries = sorted({int(e) for e in doc_entries})
        for start in range(0, len(entries), 500):
            chunk = entries[start : start + 500]
            placeholders = ", ".join(["?"] * len(chunk))
            cursor.execute(
                f'''
                SELECT H."DocEntry", H."DocNum", H."DocDate", H."CardCode", H."CardName",
                       H."U_Dipatch_Date", H."CANCELED", H."DocTotal"
                FROM "{schema}"."OINV" H
                WHERE H."DocEntry" IN ({placeholders})
                ''',
                chunk,
            )
            for row in cursor.fetchall():
                found[int(row[0])] = {
                    "doc_num": str(row[1] or ""),
                    "doc_date": row[2],
                    "card_code": str(row[3] or ""),
                    "card_name": str(row[4] or ""),
                    "sap_dispatch_date": row[5],
                    "cancelled": str(row[6] or ""),
                    "doc_total": float(row[7] or 0),
                }
        cursor.close()
    finally:
        conn.close()

    return found


def fmt(value):
    if value is None:
        return ""
    try:
        return value.strftime("%Y-%m-%d")
    except AttributeError:
        return str(value)


def main():
    since = date.fromisoformat(SINCE)
    until = date.fromisoformat(UNTIL)

    plans_by_company = defaultdict(list)
    for plan in (
        DispatchPlan.objects.filter(
            is_active=True,
            dispatch_date__isnull=False,
            dispatch_date__gte=since,
            dispatch_date__lte=until,
            company__code__in=COMPANY_CODES,
        )
        .select_related("company", "vehicle", "transporter")
        .order_by("company__code", "-dispatch_date")
    ):
        plans_by_company[plan.company.code].append(plan)

    rows = []
    for company_code in COMPANY_CODES:
        plans = plans_by_company.get(company_code, [])
        doc_entries = [p.sap_invoice_doc_entry for p in plans if p.sap_invoice_doc_entry]
        sap = sap_invoices(company_code, doc_entries)

        for plan in plans:
            entry = plan.sap_invoice_doc_entry
            hit = sap.get(int(entry)) if entry else None
            sap_disp = hit["sap_dispatch_date"] if hit else None

            rows.append(
                {
                    "company": company_code,
                    "plan_id": plan.id,
                    "doc_entry": entry or "",
                    "app_invoice_no": plan.sap_invoice_doc_num or "",
                    "customer": plan.customer_name or "",
                    "app_dispatch_date": fmt(plan.dispatch_date),
                    "booking_status": plan.booking_status,
                    "vehicle": getattr(plan.vehicle, "vehicle_number", "") or "",
                    "in_sap": "YES" if hit else "NO",
                    "sap_invoice_no": hit["doc_num"] if hit else "",
                    "sap_doc_date": fmt(hit["doc_date"]) if hit else "",
                    "sap_customer": hit["card_name"] if hit else "",
                    "sap_cancelled": (hit["cancelled"] if hit else ""),
                    "sap_has_dispatch_date": ("YES" if sap_disp else "NO") if hit else "",
                    "sap_dispatch_date": fmt(sap_disp) if hit else "",
                    "agrees": (
                        ""
                        if not hit
                        else "MATCH"
                        if fmt(sap_disp) == fmt(plan.dispatch_date)
                        else "DIFFERENT"
                        if sap_disp
                        else "NOT IN SAP"
                    ),
                }
            )

    wb = Workbook()
    ws = wb.active
    ws.title = "Planned vs SAP"

    headers = [
        ("company", "Company", 12),
        ("plan_id", "Plan id", 10),
        ("doc_entry", "SAP DocEntry", 14),
        ("app_invoice_no", "Invoice no (app)", 18),
        ("customer", "Customer (app)", 38),
        ("app_dispatch_date", "Dispatch date (app)", 19),
        ("booking_status", "Booking status", 16),
        ("vehicle", "Vehicle", 14),
        ("in_sap", "In SAP?", 9),
        ("sap_invoice_no", "Invoice no (SAP)", 18),
        ("sap_doc_date", "Invoice date (SAP)", 18),
        ("sap_customer", "Customer (SAP)", 38),
        ("sap_cancelled", "SAP cancelled", 14),
        ("sap_has_dispatch_date", "SAP has dispatch date?", 22),
        ("sap_dispatch_date", "Dispatch date (SAP)", 19),
        ("agrees", "Dates agree?", 14),
    ]

    head_fill = PatternFill("solid", fgColor="1E40AF")
    for index, (_key, label, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=index, value=label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(index)].width = width

    warn = PatternFill("solid", fgColor="FEE2E2")
    note = PatternFill("solid", fgColor="FEF3C7")

    for r, row in enumerate(rows, start=2):
        for c, (key, _label, _width) in enumerate(headers, start=1):
            ws.cell(row=r, column=c, value=row[key])
        # The two answers somebody opens this file to find.
        if row["in_sap"] == "NO":
            ws.cell(row=r, column=9).fill = warn
        if row["sap_has_dispatch_date"] == "NO":
            ws.cell(row=r, column=14).fill = note

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # A second sheet with the counts, so the file answers the question before
    # anybody sorts a column.
    summary = wb.create_sheet("Summary")
    total = len(rows)
    in_sap = sum(1 for r in rows if r["in_sap"] == "YES")
    stamped = sum(1 for r in rows if r["sap_has_dispatch_date"] == "YES")
    unstamped = sum(1 for r in rows if r["sap_has_dispatch_date"] == "NO")
    match = sum(1 for r in rows if r["agrees"] == "MATCH")
    differ = sum(1 for r in rows if r["agrees"] == "DIFFERENT")

    lines = [
        ("Window", f"plans dated {since} to {until}"),
        ("", ""),
        ("Planned in the app (dispatch date set)", total),
        ("  of which found in SAP", in_sap),
        ("  of which NOT found in SAP", total - in_sap),
        ("", ""),
        ("In SAP and dispatch-stamped (U_Dipatch_Date)", stamped),
        ("In SAP and NOT stamped", unstamped),
        ("", ""),
        ("Stamp date equals the app's plan date", match),
        ("Stamp date differs from the app's plan date", differ),
    ]
    for company_code in COMPANY_CODES:
        company_rows = [r for r in rows if r["company"] == company_code]
        lines.append(("", ""))
        lines.append((f"{company_code} planned", len(company_rows)))
        lines.append(
            (f"{company_code} stamped in SAP",
             sum(1 for r in company_rows if r["sap_has_dispatch_date"] == "YES"))
        )

    for r, (label, value) in enumerate(lines, start=1):
        summary.cell(row=r, column=1, value=label).font = Font(
            bold=not str(label).startswith(" ") and bool(label)
        )
        summary.cell(row=r, column=2, value=value)
    summary.column_dimensions["A"].width = 48
    summary.column_dimensions["B"].width = 34

    wb.save(OUT)

    print(f"rows: {total}")
    print(f"in SAP: {in_sap}   not in SAP: {total - in_sap}")
    print(f"SAP stamped: {stamped}   SAP not stamped: {unstamped}")
    print(f"dates match: {match}   differ: {differ}")
    for company_code in COMPANY_CODES:
        print(company_code, sum(1 for r in rows if r["company"] == company_code))
    print(f"saved: {OUT}")


main()
