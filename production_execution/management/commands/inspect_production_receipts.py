"""Inspect raw SAP OINM rows behind the production reconciliation numbers.

Use this to trace where a reconciliation "SAP" quantity comes from — which
transaction types, which documents, which quantity/UOM, and who created them —
so the reconciliation query can be pointed at the right TransType.

Usage:
    python manage.py inspect_production_receipts --company JIVO_OIL
    python manage.py inspect_production_receipts --company JIVO_OIL --item FG0000379
    python manage.py inspect_production_receipts --company JIVO_OIL \
        --item "MUSTARD KACHI GHANI 1 LTR 20 PCS ROUND BOTTLE" \
        --warehouse BH-PF --date 2026-07-27
"""

from collections import defaultdict
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from sap_client.context import CompanyContext
from sap_client.hana.connection import HanaConnection


class Command(BaseCommand):
    help = "Inspect raw SAP OINM rows for an item/warehouse/date (trace reconciliation)."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Company code, e.g. JIVO_OIL")
        parser.add_argument("--warehouse", default="BH-PF")
        parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
        parser.add_argument("--date-to", default=None, help="YYYY-MM-DD (default: same as --date)")
        parser.add_argument("--item", default=None, help="Item code or name filter (optional)")

    def handle(self, *args, **opts):
        company = opts["company"]
        whs = opts["warehouse"]
        d_from = opts["date"] or date.today().isoformat()
        d_to = opts["date_to"] or d_from
        item = (opts["item"] or "").strip().upper()

        try:
            ctx = CompanyContext(company)
        except Exception as e:  # noqa: BLE001
            raise CommandError(f"Unknown/unconfigured company '{company}': {e}") from e

        helper = HanaConnection(ctx.hana)
        schema = helper.schema

        clauses = ['O."Warehouse" = ?', 'O."DocDate" >= ?', 'O."DocDate" <= ?']
        params = [whs, d_from, d_to]
        if item:
            clauses.append(
                '(UPPER(O."ItemCode") LIKE ? OR UPPER(COALESCE(I."ItemName", \'\')) LIKE ?)'
            )
            params += [f"%{item}%", f"%{item}%"]
        where = " AND ".join(clauses)

        query = f"""
SELECT
    O."DocDate", O."TransType", O."TransNum", O."ItemCode",
    COALESCE(I."ItemName", ''), O."Warehouse",
    COALESCE(O."InQty", 0), COALESCE(O."OutQty", 0),
    COALESCE(O."BASE_REF", ''), O."CreatedBy"
FROM "{schema}"."OINM" O
LEFT JOIN "{schema}"."OITM" I ON I."ItemCode" = O."ItemCode"
WHERE {where}
ORDER BY O."ItemCode", O."TransType", O."DocDate"
"""

        conn = helper.connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            conn.close()

        self.stdout.write(
            f"OINM rows — company={company} warehouse={whs} "
            f"date {d_from}..{d_to}" + (f" item~'{item}'" if item else "")
        )
        if not rows:
            self.stdout.write("  (no rows)")
            return

        self.stdout.write(
            f"{'Date':11} {'Type':>4} {'DocNum':>8} {'ItemCode':14} "
            f"{'In':>12} {'Out':>12} {'By':>6}  Ref"
        )
        by_type = defaultdict(lambda: [0.0, 0.0, 0])
        for r in rows:
            dd = r[0].isoformat() if r[0] else ""
            tt = int(r[1] or 0)
            inq = float(r[6] or 0)
            outq = float(r[7] or 0)
            self.stdout.write(
                f"{dd:11} {tt:>4} {str(r[2]):>8} {str(r[3]):14} "
                f"{inq:12.3f} {outq:12.3f} {str(r[9] or ''):>6}  {r[8]}"
            )
            b = by_type[tt]
            b[0] += inq
            b[1] += outq
            b[2] += 1

        self.stdout.write("\nTotals by TransType (59=Goods Receipt, 202=Production Order, 60=Goods Issue):")
        for tt, (inq, outq, n) in sorted(by_type.items()):
            self.stdout.write(f"  TransType {tt}: rows={n}  InQty={inq:.3f}  OutQty={outq:.3f}")
        self.stdout.write(
            "\nTip: the reconciliation currently sums InQty for TransType (59, 202). "
            "If your SAP production entry is one type only, we should filter to that one."
        )
