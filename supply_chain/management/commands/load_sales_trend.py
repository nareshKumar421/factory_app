"""Load three-month sales per item — the base of the brief's 35% floor.

    python manage.py load_sales_trend --company JIVO_OIL --csv trend.csv

CSV columns: ``item_code,three_month_qty`` (``item_name`` optional). A CSV rather
than a live ERP pull because the brief's "derived from sales — automatic" already
happens inside the HANA procedure; what is missing locally is the sales FIGURE the
35% applies to, without which the procedure's ``min_stock`` cannot be checked
against the brief's rule at all.
"""
import csv
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from supply_chain.models import SalesTrend


class Command(BaseCommand):
    help = "Load the trailing three-month sales trend used to compute the stock floor."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--csv", required=True, help="Path to item_code,three_month_qty CSV.")
        parser.add_argument("--source", default="CSV")

    def handle(self, *args, **options):
        path = options["csv"]
        try:
            handle = open(path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CommandError(f"Could not read {path}: {exc}")

        loaded, skipped = 0, 0
        with handle:
            for row in csv.DictReader(handle):
                code = (row.get("item_code") or "").strip()
                if not code:
                    skipped += 1
                    continue
                try:
                    qty = Decimal((row.get("three_month_qty") or "0").strip() or "0")
                except InvalidOperation:
                    self.stderr.write(f"  {code}: unreadable quantity — skipped.")
                    skipped += 1
                    continue
                SalesTrend.objects.update_or_create(
                    company_code=options["company"], item_code=code,
                    defaults={
                        "item_name": (row.get("item_name") or "")[:255],
                        "three_month_qty": qty,
                        "source": options["source"][:40],
                    },
                )
                loaded += 1

        self.stdout.write(self.style.SUCCESS(
            f"Loaded {loaded} sales-trend row(s) for {options['company']}"
            + (f", skipped {skipped}." if skipped else ".")
        ))
