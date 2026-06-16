"""One-off repair: restore correct invoice weight on the 6 existing sales
dispatch entries after the (non-idempotent) backfill was applied twice.

Sets ABSOLUTE known-correct line weights (from the verified dry run) and
recomputes document/entry totals. Idempotent -- safe to run repeatedly.
Dry run by default; pass --apply to persist. Delete this command once the
data is confirmed correct.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from gate_core.models.sales_dispatch import (
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
)

# (entry_id, line_num) -> correct line weight
CORRECT = {
    (2, 0): "3175.000",
    (3, 0): "397.995", (3, 1): "396.656", (3, 2): "1620.000", (3, 3): "1586.624",
    (3, 4): "393.000", (3, 5): "99.711", (3, 6): "162.000", (3, 7): "160.000", (3, 8): "1429.000",
    (4, 0): "3175.000",
    (5, 0): "1390.440", (5, 1): "1454.790", (5, 2): "395.692", (5, 3): "558.408",
    (5, 4): "557.928", (5, 5): "450.860", (5, 6): "3422.757", (5, 7): "3401.850",
    (6, 0): "2540.000", (6, 1): "625.000",
    (7, 0): "3175.000",
}
Z = Decimal("0")


class Command(BaseCommand):
    help = "Restore correct invoice weights after the double-applied backfill."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist (default dry run).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        self.stdout.write("APPLY" if apply_changes else "DRY RUN (nothing written)")
        with transaction.atomic():
            for entry in SalesDispatchGateOut.objects.order_by("id"):
                doc_totals = {}
                entry_total = Z
                for item in entry.items.all():
                    key = (entry.id, item.line_num)
                    new_w = Decimal(CORRECT[key]) if key in CORRECT else (item.total_weight or Z)
                    if apply_changes and key in CORRECT:
                        item.total_weight = new_w
                        item.save(update_fields=["total_weight"])
                    entry_total += new_w
                    if item.document_id is not None:
                        doc_totals[item.document_id] = doc_totals.get(item.document_id, Z) + new_w
                for doc_id, total in doc_totals.items():
                    if apply_changes:
                        SalesDispatchGateOutDocument.objects.filter(id=doc_id).update(total_weight=total)
                self.stdout.write(f"Entry #{entry.id}: {entry.total_weight} -> {entry_total}")
                if apply_changes:
                    entry.total_weight = entry_total
                    entry.save(update_fields=["total_weight"])
            if not apply_changes:
                transaction.set_rollback(True)
        self.stdout.write(self.style.SUCCESS("Applied." if apply_changes else "Dry run complete."))
