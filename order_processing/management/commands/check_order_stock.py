"""Answer "can we fulfil this?" for pending orders. Reads SAP; changes nothing.

    python manage.py check_order_stock --limit 20
    python manage.py check_order_stock --order 2645

Nothing is reserved and nothing is written to SAP -- this is the read that tells
an operator where an order stands.
"""
from django.core.management.base import BaseCommand

from order_processing.models import OmsOrder
from order_processing.services import availability


class Command(BaseCommand):
    help = "Check finished-goods availability for pending OMS orders."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--order", type=int, dest="order_id", default=None,
                            help="A single OMS order id.")
        parser.add_argument("--short-only", action="store_true",
                            help="Only show orders that cannot be fully fulfilled.")

    def handle(self, *args, **options):
        if options["order_id"]:
            orders = OmsOrder.objects.filter(oms_order_id=options["order_id"])
            if not orders:
                self.stderr.write(f"No mirrored order {options['order_id']}.")
                return
        else:
            orders = availability.pending_orders(limit=options["limit"])

        totals = {}
        for order in orders:
            result = availability.check_order(order)
            totals[result.verdict] = totals.get(result.verdict, 0) + 1
            if options["short_only"] and result.verdict == availability.Verdict.AVAILABLE:
                continue

            style = {
                "AVAILABLE": self.style.SUCCESS,
                "PARTIAL": self.style.WARNING,
                "SHORT": self.style.ERROR,
            }.get(result.verdict, lambda t: t)
            self.stdout.write(style(
                f"\n{result.order_number}  [{result.verdict}]  company={result.company_code}"
                f" -> sap={result.sap_company or '(unresolved)'}"
            ))
            for line in result.lines:
                self.stdout.write(
                    f"    {line.item_code:<12s} need={line.required:>10} "
                    f"free={line.available:>10} short={line.short:>10}  {line.verdict}"
                )
                for note in line.notes:
                    self.stdout.write(f"        - {note}")
            for err in dict.fromkeys(result.errors):
                self.stdout.write(self.style.WARNING(f"    ! {err}"))

        self.stdout.write("\n" + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
