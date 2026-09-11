"""
Prints the non-moving report straight from SAP, for one company.

The dashboard's numbers are now computed by our own query rather than read out
of ``REPORT_BP_NON_MOVING_RM``, so this is how you check them against SAP's own
Inventory Audit / stock-age screens without going through the API and the UI.

Usage:
    python manage.py check_non_moving_report --company JIVO_OIL
    python manage.py check_non_moving_report --company JIVO_OIL --age 45 --item-group 105
    python manage.py check_non_moving_report --company JIVO_OIL --age 0 --show-sql
"""

from django.core.management.base import BaseCommand, CommandError

from non_moving_rm.hana_reader import COMPANY_BRANCH_LABELS
from non_moving_rm.services import NonMovingRMService
from sap_client.exceptions import SAPConnectionError, SAPDataError


class Command(BaseCommand):
    help = "Print the non-moving stock report for one company, read live from SAP HANA."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            required=True,
            help=f"Company code, one of {', '.join(sorted(COMPANY_BRANCH_LABELS))}",
        )
        parser.add_argument(
            "--age",
            type=int,
            default=45,
            help="Days since last movement; 0 for all stock (default 45)",
        )
        parser.add_argument(
            "--item-group",
            type=int,
            default=0,
            help="OITB item group code, or 0 for all groups (default 0)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="How many item rows to print (default 20)",
        )
        parser.add_argument(
            "--show-sql",
            action="store_true",
            help="Print the generated query instead of running it",
        )

    def handle(self, *args, **options):
        company = options["company"]
        age = options["age"]
        item_group = options["item_group"]

        if age < 0:
            raise CommandError("--age cannot be negative")
        if item_group < 0:
            raise CommandError("--item-group cannot be negative")

        try:
            service = NonMovingRMService(company_code=company)
        except Exception as exc:
            raise CommandError(f"Unknown or unconfigured company {company!r}: {exc}") from exc

        if options["show_sql"]:
            query, params = service.reader._build_report_query(
                age=age,
                item_group=item_group,
            )
            self.stdout.write(query)
            self.stdout.write(f"\n-- params: {params}")
            return

        try:
            report = service.get_report(age=age, item_group=item_group)
        except (SAPConnectionError, SAPDataError) as exc:
            raise CommandError(f"SAP read failed: {exc}") from exc

        summary = report["summary"]
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"{company} — stock untouched for more than {age} days"
                + (f", item group {item_group}" if item_group else ", all item groups")
            )
        )
        self.stdout.write(
            f"  items {summary['total_items']}"
            f"   rows {len(report['data'])}"
            f"   quantity {summary['total_quantity']:,.2f}"
            f"   value {summary['total_value']:,.2f}"
        )

        self.stdout.write("\nBy warehouse:")
        for row in report["warehouse_summary"]:
            self.stdout.write(
                f"  {row['warehouse']:<12} {row['warehouse_name'][:28]:<30}"
                f" items {row['item_count']:>5}"
                f"  qty {row['total_quantity']:>14,.2f}"
                f"  value {row['total_value']:>16,.2f}"
            )

        limit = options["limit"]
        self.stdout.write(f"\nOldest {min(limit, len(report['data']))} rows:")
        self.stdout.write(
            "  (basis 'production' = packing material, aged on production alone;"
            " 'whs' is that warehouse's own last movement, transfers included)"
        )
        for row in report["data"][:limit]:
            self.stdout.write(
                f"  {row['days_since_last_movement']:>6}d"
                f"  {str(row['last_movement_date'])[:10]:<12}"
                f"  {row.get('movement_basis', 'any'):<11}"
                f"  whs {row.get('days_since_warehouse_movement', 0):>6}d"
                f"  {row['item_code']:<16} {row['item_name'][:34]:<36}"
                f"  {row['warehouse']:<10}"
                f"  qty {row['quantity']:>12,.2f}"
                f"  value {row['value']:>14,.2f}"
                f"  used {row['consumption_ratio']:>8,.2f}%"
            )
