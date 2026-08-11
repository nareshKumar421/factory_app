"""Pull orders from OMS into the local mirror. Safe to run repeatedly.

    python manage.py sync_oms_orders                    # incremental
    python manage.py sync_oms_orders --full             # re-read everything
    python manage.py sync_oms_orders --status COMPLETED # one status only
    python manage.py sync_oms_orders --order-id 2645    # one order

Reads only. Intended for a 15-minute cron or APScheduler entry, matching how
`maintenance` schedules its jobs — this project has no Celery.
"""
from django.core.management.base import BaseCommand, CommandError

from order_processing.integrations.oms.reader import OmsUnavailable, ping
from order_processing.services.order_sync import current_watermark, sync_orders


class Command(BaseCommand):
    help = "Sync orders from the OMS database into the local mirror."

    def add_arguments(self, parser):
        parser.add_argument("--full", action="store_true",
                            help="Ignore the watermark and re-read every order.")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--status", action="append", dest="statuses", default=None,
                            help="Restrict to an OMS status code. Repeatable.")
        parser.add_argument("--order-id", action="append", type=int, dest="order_ids",
                            default=None, help="Sync specific OMS order ids. Repeatable.")
        parser.add_argument("--check", action="store_true",
                            help="Only test connectivity; sync nothing.")

    def handle(self, *args, **options):
        ok, detail = ping()
        if not ok:
            raise CommandError(f"OMS is not reachable: {detail}")
        self.stdout.write(f"OMS reachable — {detail}")
        if options["check"]:
            return

        watermark = current_watermark()
        self.stdout.write(
            f"Watermark: {watermark or 'none (first run)'}"
            + (" — ignored (--full)" if options["full"] else "")
        )

        try:
            run = sync_orders(
                limit=options["limit"], statuses=options["statuses"],
                order_ids=options["order_ids"], full=options["full"],
                actor="sync_oms_orders",
            )
        except OmsUnavailable as exc:
            raise CommandError(f"Sync failed: {exc}")

        if run.orders_seen == 0:
            self.stdout.write("Nothing changed since the last run.")
            return

        self.stdout.write(self.style.SUCCESS(
            f"{run.orders_seen} order(s): {run.orders_created} new, "
            f"{run.orders_updated} updated, {run.lines_written} line(s)."
        ))
        if run.issues_found:
            # Surfaced, not buried: these are lines the engine cannot fully trust.
            self.stdout.write(self.style.WARNING(
                f"  {run.issues_found} line issue(s) recorded — see the Issues column."
            ))
        self.stdout.write(f"  Watermark now {run.watermark_to}")
