"""Backfill corrected invoice weight on existing sales dispatch entries.

Historic snapshots were created with the old formula `Quantity * U_Gross_Weight`,
where `U_Gross_Weight` is the gross weight of a *case* but `Quantity` is in
pieces -- so the stored weight is inflated by the pieces-per-case factor
(`OITM.SalFactor2`). This command divides each stored line weight by that pack
size and recomputes the document and entry totals.

Dry run by default; pass --apply to persist.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from gate_core.models.sales_dispatch import (
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
)
from sap_client.context import CompanyContext
from sap_client.hana.connection import HanaConnection

Q = Decimal("0.001")


class Command(BaseCommand):
    help = (
        "Recompute invoice weight on existing sales dispatch entries by "
        "dividing the per-case gross weight by SalFactor2 (pieces per case)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the corrected weights (default is a read-only dry run).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        self.stdout.write(
            self.style.WARNING("DRY RUN -- no changes will be written. Pass --apply to persist.")
            if not apply_changes
            else self.style.WARNING("APPLY -- changes WILL be written.")
        )

        pack_cache = {}  # (company_code, item_code) -> Decimal pack size >= 1
        entries = SalesDispatchGateOut.objects.all().order_by("id")

        with transaction.atomic():
            for entry in entries:
                company_code = getattr(entry.company, "code", None)
                items = list(entry.items.all())
                self._load_pack_sizes(pack_cache, company_code, [it.item_code for it in items])

                self.stdout.write(
                    f"\nEntry #{entry.id} ({company_code}, doc={entry.sap_doc_num}) "
                    f"old total_weight={entry.total_weight}"
                )

                entry_total = Decimal("0")
                doc_totals = {}
                for it in items:
                    pack = pack_cache.get((company_code, it.item_code), Decimal("1"))
                    old = it.total_weight or Decimal("0")
                    new = (old / pack).quantize(Q) if pack else old
                    self.stdout.write(
                        f"    {it.item_code:<14} qty={it.quantity} pack={pack} "
                        f"{old} -> {new}"
                    )
                    if apply_changes and new != old:
                        it.total_weight = new
                        it.save(update_fields=["total_weight"])
                    entry_total += new
                    if it.document_id is not None:
                        doc_totals[it.document_id] = doc_totals.get(it.document_id, Decimal("0")) + new

                for doc_id, total in doc_totals.items():
                    total = total.quantize(Q)
                    if apply_changes:
                        SalesDispatchGateOutDocument.objects.filter(id=doc_id).update(total_weight=total)

                entry_total = entry_total.quantize(Q)
                self.stdout.write(
                    self.style.SUCCESS(f"  => entry total_weight {entry.total_weight} -> {entry_total}")
                )
                if apply_changes:
                    entry.total_weight = entry_total
                    entry.save(update_fields=["total_weight"])

            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write(
            self.style.SUCCESS("\nApplied." if apply_changes else "\nDry run complete (nothing written).")
        )

    def _load_pack_sizes(self, cache, company_code, item_codes):
        codes = sorted({c for c in item_codes if c and (company_code, c) not in cache})
        if not codes:
            return
        ctx = CompanyContext(company_code)
        conn = HanaConnection(ctx.hana)
        schema = conn.schema
        cursor = conn.connect().cursor()
        placeholders = ",".join("?" for _ in codes)
        cursor.execute(
            f'SELECT "ItemCode", "SalFactor2" FROM "{schema}"."OITM" WHERE "ItemCode" IN ({placeholders})',
            codes,
        )
        found = {}
        for code, sal_factor2 in cursor.fetchall():
            try:
                value = Decimal(str(sal_factor2)) if sal_factor2 is not None else Decimal("1")
            except (ValueError, ArithmeticError):
                value = Decimal("1")
            found[code] = value if value > 0 else Decimal("1")
        cursor.close()
        for code in codes:
            cache[(company_code, code)] = found.get(code, Decimal("1"))
