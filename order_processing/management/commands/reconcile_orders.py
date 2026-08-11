"""Compare the local mirror against OMS and report the drift.

    python manage.py reconcile_orders
    python manage.py reconcile_orders --limit 500

Reports; never repairs. A reconciliation that silently fixes things hides the
fault that caused the drift -- and the fault is the useful part.
"""
from django.core.management.base import BaseCommand

from order_processing.services import reconciliation


class Command(BaseCommand):
    help = "Reconcile the local order mirror against OMS."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None,
                            help="Compare only the most recently changed N orders.")
        parser.add_argument("--show", type=int, default=10)

    def handle(self, *args, **options):
        report = reconciliation.reconcile_orders(limit=options["limit"])
        if not report["ok"]:
            self.stderr.write(f"Could not reconcile: {report['error']}")
            return

        self.stdout.write(
            f"Compared {report['compared']} OMS order(s)"
            + ("" if report["full_scan"] else " (windowed — 'missing in OMS' not checked)")
        )
        if report["clean"]:
            self.stdout.write(self.style.SUCCESS("Mirror matches OMS."))
            return

        show = options["show"]
        sections = [
            ("missing_here", "In OMS but NOT mirrored — demand nobody is planning for",
             self.style.ERROR),
            ("missing_in_oms", "Mirrored but gone from OMS", self.style.WARNING),
            ("status_drift", "Status disagrees", self.style.WARNING),
            ("line_drift", "Line count disagrees", self.style.WARNING),
            ("sap_created_drift", "sap_created disagrees", self.style.WARNING),
        ]
        for key, title, style in sections:
            rows = report[key]
            if not rows:
                continue
            self.stdout.write(style(f"\n{title}: {len(rows)}"))
            for row in rows[:show]:
                detail = " ".join(f"{k}={v}" for k, v in row.items() if k != "oms_order_id")
                self.stdout.write(f"  #{row['oms_order_id']} {detail}")
            if len(rows) > show:
                self.stdout.write(f"  …and {len(rows) - show} more")

        self.stdout.write("\nRe-run `sync_oms_orders --full` to close a gap, "
                          "then reconcile again to confirm.")
