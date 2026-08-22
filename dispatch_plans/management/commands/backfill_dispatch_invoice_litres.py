"""Recompute stored litres on sales dispatch entries and dispatch plans from SAP.

Litres are ``Quantity * OITM.SalPackUn`` — SalPackUn is the volume of one billed
piece and is what SAP's own reports run on (its PRODUCTION_RELEASE_OIL view
computes "Liter" as ``PlannedQty * SalPackUn``). ``U_IsLitre`` is the gate:
cartons, caps and preforms carry a SalPackUn too, so without it a packaging line
would report litres.

Live reads already work this way. Rows written before the change hold litres from
the old cascade (item-name volume -> production BOM -> a 910 g/L weight guess),
which under-counted combos and CSD cartons and over-counted nothing reliably.
This command rewrites those snapshots to the absolute corrected value, so it is
idempotent — running it repeatedly converges.

Items SAP holds no volume for (``U_IsLitre`` = 'N') get 0 litres, matching the
live query. Dry run by default; pass --apply to persist.

Usage:
    python manage.py backfill_dispatch_invoice_litres
    python manage.py backfill_dispatch_invoice_litres --apply
    python manage.py backfill_dispatch_invoice_litres --apply --company JIVO_OIL
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from dispatch_plans.models import DispatchPlan
from gate_core.models.sales_dispatch import (
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
)
from sap_client.context import CompanyContext
from sap_client.hana.connection import HanaConnection

Q = Decimal("0.001")
Z = Decimal("0")


class Command(BaseCommand):
    help = "Recompute stored litres from SAP OITM.SalPackUn (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the corrected litres (default is a read-only dry run).",
        )
        parser.add_argument("--company", help="Limit to a single company code.")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        company_code_filter = options.get("company")
        self.stdout.write("APPLY" if apply_changes else "DRY RUN (nothing written)")

        # (company_code, item_code) -> litres per piece, 0 when SAP holds none.
        litre_cache = {}

        with transaction.atomic():
            self._backfill_dispatch_entries(litre_cache, company_code_filter, apply_changes)
            self._backfill_plans(litre_cache, company_code_filter, apply_changes)
            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            "Applied." if apply_changes else "Dry run complete."))

    # -- sales dispatch entries (line -> document -> entry) ----------------
    def _backfill_dispatch_entries(self, cache, company_code_filter, apply_changes):
        entries = SalesDispatchGateOut.objects.order_by("id")
        if company_code_filter:
            entries = entries.filter(company__code=company_code_filter)

        for entry in entries:
            company_code = getattr(entry.company, "code", None)
            items = list(entry.items.all())
            self._load_litres(cache, company_code, [it.item_code for it in items])

            doc_totals = {}
            entry_total = Z
            for item in items:
                per_piece = cache.get((company_code, item.item_code), Z)
                new_litres = ((item.quantity or Z) * per_piece).quantize(Q)
                if apply_changes and new_litres != (item.total_litres or Z):
                    item.total_litres = new_litres
                    item.save(update_fields=["total_litres"])
                entry_total += new_litres
                if item.document_id is not None:
                    doc_totals[item.document_id] = (
                        doc_totals.get(item.document_id, Z) + new_litres
                    )

            for doc_id, total in doc_totals.items():
                if apply_changes:
                    SalesDispatchGateOutDocument.objects.filter(id=doc_id).update(
                        total_litres=total.quantize(Q)
                    )

            self.stdout.write(
                f"Entry #{entry.id} ({company_code}): "
                f"{entry.total_litres} -> {entry_total.quantize(Q)}"
            )
            if apply_changes:
                entry.total_litres = entry_total.quantize(Q)
                entry.save(update_fields=["total_litres"])

    # -- dispatch plans (no stored lines — re-read the bill) ---------------
    def _backfill_plans(self, cache, company_code_filter, apply_changes):
        """A plan holds only the bill total, so its litres come from a fresh read.

        Batched per company through the same reader the dashboards use, so the
        backfilled number is exactly what a live read would show.
        """
        from dispatch_plans.hana_reader import HanaDispatchBillReader

        plans = DispatchPlan.objects.select_related("company").order_by("id")
        if company_code_filter:
            plans = plans.filter(company__code=company_code_filter)

        by_company = {}
        for plan in plans:
            code = getattr(plan.company, "code", None)
            if code and plan.sap_invoice_doc_entry:
                by_company.setdefault(code, []).append(plan)

        for company_code, company_plans in by_company.items():
            reader = HanaDispatchBillReader(CompanyContext(company_code))
            doc_entries = sorted({p.sap_invoice_doc_entry for p in company_plans})
            litres_by_entry = {}
            for chunk in self._chunks(doc_entries, 200):
                for bill in reader.list_bills_by_doc_entries(chunk):
                    litres_by_entry[bill["doc_entry"]] = Decimal(
                        str(bill.get("total_litres") or 0)
                    ).quantize(Q)

            for plan in company_plans:
                new_litres = litres_by_entry.get(plan.sap_invoice_doc_entry)
                if new_litres is None:
                    # Bill no longer readable in SAP (cancelled, purged) — leave it.
                    self.stdout.write(self.style.WARNING(
                        f"Plan #{plan.id} ({company_code}) doc_entry "
                        f"{plan.sap_invoice_doc_entry}: not found in SAP — SKIPPED"))
                    continue
                self.stdout.write(
                    f"Plan #{plan.id} ({company_code}): {plan.total_litres} -> {new_litres}")
                if apply_changes and new_litres != (plan.total_litres or Z):
                    plan.total_litres = new_litres
                    plan.save(update_fields=["total_litres"])

    # -- helpers -----------------------------------------------------------
    def _load_litres(self, cache, company_code, item_codes):
        codes = sorted({c for c in item_codes if c and (company_code, c) not in cache})
        if not codes:
            return
        conn = HanaConnection(CompanyContext(company_code).hana)
        schema = conn.schema
        connection = conn.connect()
        cursor = connection.cursor()
        placeholders = ",".join("?" for _ in codes)
        cursor.execute(
            f'SELECT "ItemCode", "SalPackUn" FROM "{schema}"."OITM" '
            f'WHERE "ItemCode" IN ({placeholders}) '
            f"AND UPPER(IFNULL(\"U_IsLitre\", 'N')) = 'Y'",
            codes,
        )
        found = {code: self._to_decimal(value) for code, value in cursor.fetchall()}
        cursor.close()
        for code in codes:
            cache[(company_code, code)] = found.get(code, Z)

    @staticmethod
    def _chunks(values, size):
        for start in range(0, len(values), size):
            yield values[start:start + size]

    @staticmethod
    def _to_decimal(value):
        try:
            return Decimal(str(value)) if value is not None else Z
        except (ValueError, ArithmeticError):
            return Z
