"""The whole chain in one command, for cron.

    python manage.py run_order_pipeline

    sync OMS -> process orders -> explode BOMs -> plan procurement

Ordered deliberately: processing after syncing means the engine never decides
against yesterday's orders, and planning last means BOMs are exploded against
requirements computed moments earlier. Every step is idempotent, so a doubled or
missed run is harmless.
"""
from django.core.management.base import BaseCommand

from order_processing import jobs


class Command(BaseCommand):
    help = "Run the full order-processing pipeline once."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--bom-depth", type=int, default=1)
        parser.add_argument("--skip-sync", action="store_true")

    def handle(self, *args, **options):
        if not options["skip_sync"]:
            run = jobs.sync_oms_orders_job()
            if run is None:
                # Continue anyway: the mirror still holds yesterday's orders, and
                # a stock re-check on those is more useful than doing nothing.
                self.stdout.write(self.style.WARNING(
                    "OMS unreachable — processing the existing mirror instead."))
            else:
                self.stdout.write(
                    f"Sync: {run.orders_seen} order(s), {run.lines_written} line(s), "
                    f"{run.issues_found} issue(s)."
                )

        tally = jobs.process_orders_job(limit=options["limit"])
        self.stdout.write("Processed: " + (", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
                                           or "nothing pending"))

        summary = jobs.plan_materials_job(bom_depth=options["bom_depth"])
        self.stdout.write(
            f"Materials: {summary['exploded']} exploded, {summary['no_bom']} without a BOM, "
            f"{summary['failed']} failed."
        )
        self.stdout.write(
            f"Procurement: {summary['procurement_created']} new, "
            f"{summary['procurement_updated']} updated, {summary['procurement_retired']} retired."
        )
