"""Delete ONLY the sheet-driven-flow test data, keeping master data.

Removes: order import batches, warehouse issue requests (+ lines), and the orders
that were created/refreshed via a sheet import (``import_batch`` set) together with
their dispatches, scans, returns and internal billing.

KEEPS: SKU mappings, combos, and marketplace warehouses (master data).

Safe by default — a dry run that only reports counts. Pass ``--yes`` to actually
delete. Optionally scope to one company with ``--company CODE``.

    # preview (no deletion)
    python manage.py clear_marketplace_sheet_data
    # actually delete
    python manage.py clear_marketplace_sheet_data --yes
    # scope to one company
    python manage.py clear_marketplace_sheet_data --company JIVO_MART --yes
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from marketplace.models import (
    MarketplaceDispatch,
    MarketplaceIssueRequest,
    MarketplaceOrder,
    MarketplaceOrderBilling,
    MarketplaceReturn,
    OrderImportBatch,
)


class Command(BaseCommand):
    help = "Delete sheet-flow test data (batches, issue requests, imported orders). Keeps masters."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Restrict to a company code (default: all).")
        parser.add_argument("--yes", action="store_true", help="Actually delete (default: dry run).")

    def handle(self, *args, **opts):
        company_code = opts.get("company")

        batches = OrderImportBatch.objects.all()
        requests = MarketplaceIssueRequest.objects.all()
        imported_orders = MarketplaceOrder.objects.filter(import_batch__isnull=False)
        if company_code:
            batches = batches.filter(company__code=company_code)
            requests = requests.filter(company__code=company_code)
            imported_orders = imported_orders.filter(company__code=company_code)

        order_ids = list(imported_orders.values_list("id", flat=True))
        order_keys = list(imported_orders.values_list("order_id", flat=True))

        dispatches = MarketplaceDispatch.objects.filter(order_id__in=order_ids)
        returns = MarketplaceReturn.objects.filter(order_id__in=order_ids)
        billings = MarketplaceOrderBilling.objects.filter(order_id__in=order_keys)
        if company_code:
            billings = billings.filter(company__code=company_code)

        counts = {
            "import_batches": batches.count(),
            "issue_requests": requests.count(),
            "imported_orders": len(order_ids),
            "dispatches": dispatches.count(),
            "returns": returns.count(),
            "billings": billings.count(),
        }

        self.stdout.write(self.style.WARNING("Sheet-flow data to remove (masters kept):"))
        for name, n in counts.items():
            self.stdout.write(f"  {name:16} {n}")

        if not opts["yes"]:
            self.stdout.write(self.style.NOTICE("\nDRY RUN — nothing deleted. Re-run with --yes to delete."))
            return

        with transaction.atomic():
            # Order matters: clear PROTECTed refs to orders, then orders, then batches.
            dispatches.delete()          # cascades scans
            returns.delete()             # cascades return scans + photos
            billings.delete()
            imported_orders.delete()     # cascades order lines
            requests.delete()            # cascades issue lines
            batches.delete()             # cascades any remaining issue requests

        self.stdout.write(self.style.SUCCESS("\nDeleted. Master data (mappings, combos, warehouses) untouched."))
