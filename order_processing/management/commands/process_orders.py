"""Run pending orders through the processing engine.

    python manage.py process_orders --limit 50
    python manage.py process_orders --order 2645

Validates, checks stock, and raises or retires production requirements. Reads SAP;
writes nothing to SAP or OMS. Idempotent -- safe to run on a schedule.
"""
from django.core.management.base import BaseCommand

from order_processing.models import OmsOrder
from order_processing.services import processing


class Command(BaseCommand):
    help = "Process pending OMS orders: check stock and raise production requirements."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--order", type=int, dest="order_id", default=None)
        parser.add_argument("--show-requirements", action="store_true")

    def handle(self, *args, **options):
        if options["order_id"]:
            order = OmsOrder.objects.filter(oms_order_id=options["order_id"]).first()
            if order is None:
                self.stderr.write(f"No mirrored order {options['order_id']}.")
                return
            _o, _c, result = processing.process_order(order, actor="process_orders")
            verdict = result.verdict if result else "SKIPPED"
            self.stdout.write(f"{order.order_number}: {verdict} -> {order.state}")
            tally = {verdict: 1}
        else:
            tally = processing.process_pending(limit=options["limit"], actor="process_orders")
            self.stdout.write("Processed: " + ", ".join(
                f"{k}={v}" for k, v in sorted(tally.items())) or "nothing")

        if options["show_requirements"]:
            reqs = processing.open_requirements()[:20]
            if not reqs:
                self.stdout.write("\nNo open production requirements.")
                return
            self.stdout.write(self.style.WARNING(f"\nOpen production requirements ({len(reqs)}):"))
            self.stdout.write(f"  {'item':<12s} {'warehouse':<9s} {'qty':>12s} {'needed by':>12s}  orders")
            for r in reqs:
                orders = ", ".join(s.order.order_number for s in r.sources.all()[:3])
                more = r.sources.count() - 3
                self.stdout.write(
                    f"  {r.item_code:<12s} {r.warehouse_code:<9s} {r.quantity:>12} "
                    f"{str(r.needed_by or '-'):>12s}  {orders}" + (f" +{more}" if more > 0 else "")
                )
