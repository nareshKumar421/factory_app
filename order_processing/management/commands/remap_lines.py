"""Re-apply the warehouse and issue rules to lines already mirrored.

    python manage.py remap_lines
    python manage.py remap_lines --dry-run

Warehouse resolution happens at sync time, so changing OMS_CATEGORY_WAREHOUSE
leaves every line already stored carrying the OLD answer. A full re-sync would
fix that, but it re-reads thousands of orders from OMS to recompute something
derivable from what we already hold — this does only the recompute.

Touches nothing that came from OMS. Only `warehouse_code` and `issues`, both of
which are ours.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from order_processing.integrations.oms.mapper import warehouse_for
from order_processing.models import LineIssue, OmsOrderLine


class Command(BaseCommand):
    help = "Re-derive warehouse and issue flags on already-mirrored order lines."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--category", default=None,
                            help="Limit to one category, e.g. BEVERAGES.")

    def handle(self, *args, **options):
        qs = OmsOrderLine.objects.all()
        if options["category"]:
            qs = qs.filter(category__iexact=options["category"])

        changed, gained, lost = [], 0, 0
        for line in qs.iterator(chunk_size=1000):
            warehouse = warehouse_for(line.category)
            issues = [i for i in (line.issues or []) if i != LineIssue.NO_WAREHOUSE.value]
            if not warehouse:
                issues.append(LineIssue.NO_WAREHOUSE.value)
            if warehouse == line.warehouse_code and sorted(issues) == sorted(line.issues or []):
                continue
            if warehouse and not line.warehouse_code:
                gained += 1
            elif line.warehouse_code and not warehouse:
                lost += 1
            line.warehouse_code = warehouse[:40]
            line.issues = issues
            changed.append(line)

        if options["dry_run"]:
            self.stdout.write(
                f"Would update {len(changed)} line(s): {gained} gaining a warehouse, "
                f"{lost} losing one."
            )
            return

        with transaction.atomic():
            OmsOrderLine.objects.bulk_update(changed, ["warehouse_code", "issues"],
                                             batch_size=500)
        self.stdout.write(self.style.SUCCESS(
            f"Updated {len(changed)} line(s): {gained} gained a warehouse, {lost} lost one."
        ))
        remaining = OmsOrderLine.objects.with_issue(LineIssue.NO_WAREHOUSE.value).count()
        if remaining:
            self.stdout.write(self.style.WARNING(
                f"  {remaining} line(s) still have no warehouse rule."
            ))
