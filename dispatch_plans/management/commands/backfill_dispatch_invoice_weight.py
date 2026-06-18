"""Recompute invoice weight on existing sales dispatch entries from SAP.

The correct line weight is `Quantity * U_Gross_Weight / SalFactor2`, where
`U_Gross_Weight` is the gross weight of one sales case, `SalFactor2` is the
number of pieces per case, and `Quantity` is in pieces (mirrors the live
hana_reader query). This command looks those factors up per item from the
SAP item master and SETS the absolute corrected weight, so it is idempotent
-- running it repeatedly converges to the same values.

Items whose SAP `U_Gross_Weight` is 0/missing are SKIPPED (left unchanged),
which protects entries whose company still points at a schema without weight
data (e.g. a test schema). Dry run by default; pass --apply to persist.
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
Z = Decimal("0")
ONE = Decimal("1")


class Command(BaseCommand):
    help = "Recompute invoice weight on existing sales dispatch entries from SAP (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the corrected weights (default is a read-only dry run).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        self.stdout.write("APPLY" if apply_changes else "DRY RUN (nothing written)")

        factor_cache = {}  # (company_code, item_code) -> (gross_weight_per_case, pieces_per_case)
        with transaction.atomic():
            for entry in SalesDispatchGateOut.objects.order_by("id"):
                company_code = getattr(entry.company, "code", None)
                items = list(entry.items.all())
                self._load_item_factors(factor_cache, company_code, [it.item_code for it in items])

                doc_totals = {}
                entry_total = Z
                skipped = []
                for item in items:
                    gross, pack = factor_cache.get((company_code, item.item_code), (Z, ONE))
                    if gross <= 0:
                        # No reliable SAP weight for this item; leave as-is.
                        skipped.append(item.item_code)
                        new_weight = item.total_weight or Z
                    else:
                        new_weight = ((item.quantity or Z) * gross / pack).quantize(Q)
                        if apply_changes and new_weight != (item.total_weight or Z):
                            item.total_weight = new_weight
                            item.save(update_fields=["total_weight"])
                    entry_total += new_weight
                    if item.document_id is not None:
                        doc_totals[item.document_id] = doc_totals.get(item.document_id, Z) + new_weight

                for doc_id, total in doc_totals.items():
                    if apply_changes:
                        SalesDispatchGateOutDocument.objects.filter(id=doc_id).update(
                            total_weight=total.quantize(Q)
                        )

                note = f"  [skipped (no SAP weight): {skipped}]" if skipped else ""
                self.stdout.write(
                    f"Entry #{entry.id} ({company_code}): {entry.total_weight} -> "
                    f"{entry_total.quantize(Q)}{note}"
                )
                if apply_changes:
                    entry.total_weight = entry_total.quantize(Q)
                    entry.save(update_fields=["total_weight"])

            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS("Applied." if apply_changes else "Dry run complete."))

    def _load_item_factors(self, cache, company_code, item_codes):
        codes = sorted({c for c in item_codes if c and (company_code, c) not in cache})
        if not codes:
            return
        ctx = CompanyContext(company_code)
        conn = HanaConnection(ctx.hana)
        schema = conn.schema
        cursor = conn.connect().cursor()
        placeholders = ",".join("?" for _ in codes)
        cursor.execute(
            f'SELECT "ItemCode", "U_Gross_Weight", "SalFactor2" '
            f'FROM "{schema}"."OITM" WHERE "ItemCode" IN ({placeholders})',
            codes,
        )
        found = {}
        for code, gross, sal_factor2 in cursor.fetchall():
            found[code] = (self._to_decimal(gross, Z), self._pack(sal_factor2))
        cursor.close()
        for code in codes:
            cache[(company_code, code)] = found.get(code, (Z, ONE))

    @staticmethod
    def _to_decimal(value, default):
        try:
            return Decimal(str(value)) if value is not None else default
        except (ValueError, ArithmeticError):
            return default

    @classmethod
    def _pack(cls, value):
        pack = cls._to_decimal(value, ONE)
        return pack if pack > 0 else ONE
