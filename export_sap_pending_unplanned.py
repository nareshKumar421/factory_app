"""
Adds the other half of the picture to `planned_bills_vs_sap.xlsx`.

The first export answered "what is planned in the app, and is it in SAP" -- so
by construction it could only ever list bills that HAVE a plan. The bills SAP
calls pending but nobody has planned are the ones missing from it, and they are
the whole reason the board's tile reads low: 370-odd Mart invoices sitting
outside the planning workflow entirely.

This script adds two sheets:

  "SAP pending, no plan"  every live, uncredited, un-dispatch-stamped invoice
                          with no DispatchPlan against its DocEntry, with its
                          tonnage.
  "Pending summary"       the counts and tonnes those rows add up to.

Read-only. Nothing is written to Postgres or to SAP.

Run:
    .venv/Scripts/python.exe export_sap_pending_unplanned.py
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from datetime import date

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from dispatch_plans.models import DispatchPlan
from sap_client.context import CompanyContext
from sap_client.hana.connection import HanaConnection

# The month under review, on the invoice's own date.
SINCE = "2026-09-01"
UNTIL = "2026-09-30"
COMPANY_CODES = ["JIVO_OIL", "JIVO_MART"]
OUT = os.path.join(os.path.expanduser("~"), "Downloads", "planned_bills_vs_sap.xlsx")


def sap_pending(company_code, since, until):
    """
    Invoices SAP still considers undispatched, with tonnage.

    The three tests are the ones the SAP report uses and the ones we settled on
    for the board:

      - live (`CANCELED = 'N'`)
      - not dispatch-stamped (`U_Dipatch_Date IS NULL`)
      - no live credit note against it (matched on `BaseEntry`, never `DocNum`)

    Tonnage follows the bills reader's own fallback -- `Weight1`, then
    `Weight2`, then the item's gross weight spread over its case factor -- so
    the figure here and the figure on the board come from the same arithmetic.
    """
    context = CompanyContext(company_code)
    connection = HanaConnection(context.hana)
    schema = connection.schema

    query = f'''
        SELECT H."DocEntry", H."DocNum", H."DocDate", H."CardCode", H."CardName",
               DAYS_BETWEEN(H."DocDate", CURRENT_DATE) AS age_days,
               SUM(
                   CASE
                       WHEN IFNULL(L."Weight1", 0) > 0 THEN L."Weight1"
                       WHEN IFNULL(L."Weight2", 0) > 0 THEN L."Weight2"
                       ELSE IFNULL(L."Quantity", 0) * IFNULL(M."U_Gross_Weight", 0)
                            / (CASE WHEN IFNULL(M."SalFactor2", 0) > 0
                                    THEN M."SalFactor2" ELSE 1 END)
                   END
               ) AS kg,
               SUM(CASE WHEN M."U_IsLitre" = 'N' THEN 0
                        ELSE IFNULL(M."SalPackUn", 0) * IFNULL(L."Quantity", 0) END) AS litres,
               STRING_AGG(IFNULL(L."WhsCode", ''), ', ') AS warehouses,
               H."DocTotal"
        FROM "{schema}"."OINV" H
        JOIN "{schema}"."INV1" L ON L."DocEntry" = H."DocEntry"
        JOIN "{schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
        WHERE H."CANCELED" = 'N'
          AND H."U_Dipatch_Date" IS NULL
          AND H."DocDate" >= ?
          AND H."DocDate" <= ?
          AND NOT EXISTS (
              SELECT 1 FROM "{schema}"."RIN1" CN
              JOIN "{schema}"."ORIN" CH ON CH."DocEntry" = CN."DocEntry"
              WHERE CN."BaseType" = 13 AND CN."BaseEntry" = H."DocEntry"
                AND IFNULL(CH."CANCELED", 'N') = 'N'
          )
        GROUP BY H."DocEntry", H."DocNum", H."DocDate", H."CardCode", H."CardName",
                 DAYS_BETWEEN(H."DocDate", CURRENT_DATE), H."DocTotal"
        ORDER BY DAYS_BETWEEN(H."DocDate", CURRENT_DATE) DESC
    '''

    conn = connection.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(query, [since.strftime("%Y%m%d"), until.strftime("%Y%m%d")])
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    return [
        {
            "doc_entry": int(r[0]),
            "doc_num": str(r[1] or ""),
            "doc_date": r[2],
            "card_code": str(r[3] or ""),
            "card_name": str(r[4] or ""),
            "age_days": int(r[5] or 0),
            "kg": float(r[6] or 0),
            "litres": float(r[7] or 0),
            # HANA's STRING_AGG takes no DISTINCT, so a bill with six lines in
            # one warehouse comes back as that code six times. De-duplicated
            # here, order preserved.
            "warehouses": ", ".join(
                dict.fromkeys(part.strip() for part in str(r[8] or "").split(",") if part.strip())
            ),
            "doc_total": float(r[9] or 0),
        }
        for r in rows
    ]


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

    rows = []
    for company_code in COMPANY_CODES:
        pending = sap_pending(company_code, since, until)

        # Which of them the app has ever planned. Read once per company rather
        # than per bill -- the whole point is to find the ones with NO plan.
        planned_entries = set(
            DispatchPlan.objects.filter(
                company__code=company_code,
                is_active=True,
                sap_invoice_doc_entry__in=[p["doc_entry"] for p in pending],
            ).values_list("sap_invoice_doc_entry", flat=True)
        )

        for bill in pending:
            has_plan = bill["doc_entry"] in planned_entries
            rows.append(
                {
                    "company": company_code,
                    "doc_entry": bill["doc_entry"],
                    "doc_num": bill["doc_num"],
                    "doc_date": fmt(bill["doc_date"]),
                    "age_days": bill["age_days"],
                    "card_code": bill["card_code"],
                    "card_name": bill["card_name"],
                    "warehouses": bill["warehouses"],
                    "tonnes": round(bill["kg"] / 1000, 3),
                    "litres": round(bill["litres"], 1),
                    "doc_total": round(bill["doc_total"], 2),
                    "planned_in_app": "YES" if has_plan else "NO",
                }
            )

    unplanned = [r for r in rows if r["planned_in_app"] == "NO"]

    wb = load_workbook(OUT)
    for name in ("SAP pending, no plan", "Pending summary"):
        if name in wb.sheetnames:
            del wb[name]

    ws = wb.create_sheet("SAP pending, no plan")
    headers = [
        ("company", "Company", 12),
        ("doc_num", "Invoice no", 16),
        ("doc_entry", "DocEntry", 11),
        ("doc_date", "Invoice date", 14),
        ("age_days", "Days old", 10),
        ("card_code", "Customer code", 16),
        ("card_name", "Customer", 40),
        ("warehouses", "Warehouse(s)", 18),
        ("tonnes", "Tonnes", 11),
        ("litres", "Litres", 12),
        ("doc_total", "Invoice value", 15),
        ("planned_in_app", "Planned in app?", 16),
    ]
    head_fill = PatternFill("solid", fgColor="1E40AF")
    for index, (_key, label, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=index, value=label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(index)].width = width

    # Every row here is pending in SAP; the ones with no plan are the finding,
    # so those are the ones flagged.
    warn = PatternFill("solid", fgColor="FEE2E2")
    for r, row in enumerate(rows, start=2):
        for c, (key, _label, _width) in enumerate(headers, start=1):
            ws.cell(row=r, column=c, value=row[key])
        if row["planned_in_app"] == "NO":
            ws.cell(row=r, column=12).fill = warn
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    summary = wb.create_sheet("Pending summary")
    lines = [
        ("Window", f"invoices dated {since} to {until}"),
        ("Pending means", "live, not credited, no U_Dipatch_Date in SAP"),
        ("", ""),
        ("Pending invoices in SAP", len(rows)),
        ("  with a plan in the app", len(rows) - len(unplanned)),
        ("  with NO plan in the app", len(unplanned)),
        ("", ""),
        ("Tonnes pending, total", round(sum(r["tonnes"] for r in rows), 1)),
        ("Tonnes pending with NO plan", round(sum(r["tonnes"] for r in unplanned), 1)),
    ]
    for company_code in COMPANY_CODES:
        sub = [r for r in rows if r["company"] == company_code]
        sub_unplanned = [r for r in sub if r["planned_in_app"] == "NO"]
        lines.append(("", ""))
        lines.append((f"{company_code} pending invoices", len(sub)))
        lines.append((f"{company_code} of which unplanned", len(sub_unplanned)))
        lines.append((f"{company_code} tonnes pending", round(sum(r["tonnes"] for r in sub), 1)))
        lines.append(
            (f"{company_code} tonnes unplanned",
             round(sum(r["tonnes"] for r in sub_unplanned), 1))
        )

    for r, (label, value) in enumerate(lines, start=1):
        summary.cell(row=r, column=1, value=label).font = Font(
            bold=not str(label).startswith(" ") and bool(label)
        )
        summary.cell(row=r, column=2, value=value)
    summary.column_dimensions["A"].width = 44
    summary.column_dimensions["B"].width = 44

    # ---------------------------------------------------------------- customers
    #
    # The same pending set rolled up per customer, with the app/SAP split kept
    # side by side rather than summed: "planned" and "not planned" are two
    # different operational states and a single total hides which one a
    # customer's backlog is sitting in.
    by_customer = {}
    for row in rows:
        key = (row["company"], row["card_code"], row["card_name"])
        bucket = by_customer.setdefault(
            key,
            {
                "company": row["company"],
                "card_code": row["card_code"],
                "card_name": row["card_name"],
                "invoices": 0,
                "tonnes": 0.0,
                "litres": 0.0,
                "value": 0.0,
                "planned_invoices": 0,
                "planned_tonnes": 0.0,
                "unplanned_invoices": 0,
                "unplanned_tonnes": 0.0,
                "oldest_days": 0,
            },
        )
        bucket["invoices"] += 1
        bucket["tonnes"] += row["tonnes"]
        bucket["litres"] += row["litres"]
        bucket["value"] += row["doc_total"]
        bucket["oldest_days"] = max(bucket["oldest_days"], row["age_days"])
        if row["planned_in_app"] == "YES":
            bucket["planned_invoices"] += 1
            bucket["planned_tonnes"] += row["tonnes"]
        else:
            bucket["unplanned_invoices"] += 1
            bucket["unplanned_tonnes"] += row["tonnes"]

    customers = sorted(by_customer.values(), key=lambda b: b["tonnes"], reverse=True)
    for bucket in customers:
        for key in ("tonnes", "litres", "planned_tonnes", "unplanned_tonnes"):
            bucket[key] = round(bucket[key], 3)
        bucket["value"] = round(bucket["value"], 2)

    if "Customer wise" in wb.sheetnames:
        del wb["Customer wise"]
    cs = wb.create_sheet("Customer wise")
    cust_headers = [
        ("company", "Company", 12),
        ("card_code", "Customer code", 16),
        ("card_name", "Customer", 44),
        ("invoices", "Pending invoices", 17),
        ("tonnes", "Tonnes", 12),
        ("litres", "Litres", 14),
        ("value", "Invoice value", 16),
        ("oldest_days", "Oldest (days)", 14),
        ("planned_invoices", "Planned in app", 15),
        ("planned_tonnes", "Planned tonnes", 15),
        ("unplanned_invoices", "NOT planned", 13),
        ("unplanned_tonnes", "Unplanned tonnes", 17),
    ]
    for index, (_key, label, width) in enumerate(cust_headers, start=1):
        cell = cs.cell(row=1, column=index, value=label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cs.column_dimensions[get_column_letter(index)].width = width
    for r, bucket in enumerate(customers, start=2):
        for c, (key, _label, _width) in enumerate(cust_headers, start=1):
            cs.cell(row=r, column=c, value=bucket[key])
    cs.freeze_panes = "A2"
    cs.auto_filter.ref = cs.dimensions

    wb.save(OUT)

    print(f"pending rows: {len(rows)}   unplanned: {len(unplanned)}   customers: {len(customers)}")
    for company_code in COMPANY_CODES:
        sub = [r for r in rows if r["company"] == company_code]
        sub_un = [r for r in sub if r["planned_in_app"] == "NO"]
        print(
            company_code,
            "pending", len(sub),
            "unplanned", len(sub_un),
            "tonnes", round(sum(r["tonnes"] for r in sub), 1),
            "unplanned tonnes", round(sum(r["tonnes"] for r in sub_un), 1),
        )
    print(f"saved: {OUT}")


main()
