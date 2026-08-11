"""List the order lines the engine cannot fully trust.

    python manage.py show_line_issues
    python manage.py show_line_issues --issue QTY_DISAGREES
    python manage.py show_line_issues --issue any --limit 40

Defaults to NO_WAREHOUSE, which is the large one: OMS sends no WarehouseCode for
BEVERAGES, so those lines can never reach a stock answer. They are printed rather
than counted because a number nobody can act on is not a finding.
"""
from collections import Counter

from django.core.management.base import BaseCommand

from order_processing.models import LineIssue, OmsOrderLine


class Command(BaseCommand):
    help = "Show order lines carrying a data-quality flag."

    def add_arguments(self, parser):
        parser.add_argument("--issue", default=LineIssue.NO_WAREHOUSE.value,
                            help="A LineIssue code, or 'any'.")
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--by-item", action="store_true",
                            help="Group by item instead of listing lines.")

    def handle(self, *args, **options):
        all_flagged = OmsOrderLine.objects.flagged()
        tally = Counter(i for row in all_flagged.values_list("issues", flat=True) for i in row)
        total_lines = OmsOrderLine.objects.count()

        self.stdout.write(f"{all_flagged.count()} of {total_lines} line(s) flagged:")
        for name, count in tally.most_common():
            self.stdout.write(f"  {name:<16s} {count:>6}  ({count * 100 / max(total_lines, 1):.1f}%)")

        issue = options["issue"]
        qs = (OmsOrderLine.objects.with_issue(issue)
              .select_related("order").order_by("-order__oms_created_at", "item_code"))

        if not qs.exists():
            self.stdout.write(self.style.SUCCESS(f"\nNothing flagged as {issue}."))
            return

        self.stdout.write(self.style.WARNING(
            f"\n{issue}: {qs.count()} line(s) across "
            f"{qs.values('order_id').distinct().count()} order(s)"
        ))

        if options["by_item"]:
            by_item = Counter(qs.values_list("item_code", flat=True))
            self.stdout.write(f"\n  {'item':<14s} {'lines':>6s}  name")
            for code, n in by_item.most_common(options["limit"]):
                name = qs.filter(item_code=code).values_list("item_name", flat=True).first() or ""
                self.stdout.write(f"  {code:<14s} {n:>6}  {name[:44]}")
            return

        self.stdout.write(
            f"\n  {'order':<22s} {'item':<12s} {'qty':>12s} {'warehouse':<14s} customer"
        )
        for line in qs[:options["limit"]]:
            self.stdout.write(
                f"  {line.order.order_number:<22s} {line.item_code:<12s} "
                f"{line.quantity:>12} {line.warehouse_label:<14s} "
                f"{line.order.customer_name[:28]}"
            )
        remaining = qs.count() - options["limit"]
        if remaining > 0:
            self.stdout.write(f"  …and {remaining} more")
