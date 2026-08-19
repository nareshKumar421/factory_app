"""Backfill the SAP box/loose split onto docking rows written before it existed.

Docking lines used to store ``total_boxes`` from ``OITM.U_UNE_TOTB`` -- a field the item
master leaves empty on all but two items -- so every row landed with 0 boxes and the
screens fell back to parsing "N PCS" out of the item name. That parse counted a
500-piece line of a loose SKU as 500 boxes (see ``gate_core.services.box_packing``).

This command snapshots ``OITM.SalFactor2`` per item from SAP and rewrites
``sal_factor2`` / ``total_boxes`` / ``total_loose`` on the line items, then rolls the
totals up to the parent document and docking rows.

By default it only touches dockings that have not yet left the gate, so a dispatched
load's historical numbers stay exactly as they were printed. Pass ``--all`` to rewrite
history too, and ``--dry-run`` to see the changes without saving.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from company.models import Company
from gate_core.models import (
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
)
from gate_core.services.box_packing import split_line
from sap_client.context import CompanyContext
from sap_client.hana.connection import HanaConnection

# A docking past this point has already been printed/dispatched; leave its numbers alone.
SETTLED_STATUSES = {
    SalesDispatchGateOutStatus.DISPATCHED,
    SalesDispatchGateOutStatus.CANCELLED,
}


class Command(BaseCommand):
    help = "Backfill sal_factor2 + box/loose split on docking lines from SAP OITM."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company code to limit to (e.g. JIVO_OIL).")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include dispatched/cancelled dockings (rewrites printed history).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report only; save nothing.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        companies = Company.objects.all()
        if options.get("company"):
            companies = companies.filter(code__iexact=options["company"])
        if not companies:
            self.stderr.write("No matching company.")
            return

        for company in companies:
            self._handle_company(company, include_settled=options["all"], dry_run=dry_run)

    def _handle_company(self, company, include_settled: bool, dry_run: bool):
        items = SalesDispatchGateOutItem.objects.filter(
            sales_dispatch__company=company, is_active=True
        ).select_related("sales_dispatch")
        if not include_settled:
            items = items.exclude(sales_dispatch__status__in=SETTLED_STATUSES)

        item_codes = sorted({i.item_code for i in items if i.item_code})
        if not item_codes:
            self.stdout.write(f"{company.code}: no docking lines to backfill.")
            return

        try:
            factors = self._fetch_sal_factors(company.code, item_codes)
        except Exception as exc:  # noqa: BLE001 - surface SAP failures per company
            self.stderr.write(f"{company.code}: SAP lookup failed ({exc}); skipped.")
            return

        changed = []
        for item in items:
            factor = factors.get(item.item_code)
            packing = split_line(item.quantity, factor, item.item_name)
            boxes = Decimal(packing.boxes)
            if (
                item.sal_factor2 == factor
                and item.total_boxes == boxes
                and item.total_loose == packing.loose
            ):
                continue
            item.sal_factor2 = factor
            item.total_boxes = boxes
            item.total_loose = packing.loose
            changed.append(item)

        self.stdout.write(
            f"{company.code}: {len(changed)} of {len(items)} line(s) re-split "
            f"({len(item_codes)} item code(s) read from SAP)."
        )
        if dry_run or not changed:
            for item in changed[:10]:
                self.stdout.write(
                    f"    {item.item_code} qty={item.quantity} f2={item.sal_factor2} "
                    f"-> {item.total_boxes} box + {item.total_loose} loose"
                )
            return

        with transaction.atomic():
            SalesDispatchGateOutItem.objects.bulk_update(
                changed, ["sal_factor2", "total_boxes", "total_loose"], batch_size=500
            )
            self._rollup(changed)

    def _rollup(self, changed_items):
        """Re-sum the parent document + docking totals for every row we touched."""
        document_ids = {i.document_id for i in changed_items if i.document_id}
        documents = SalesDispatchGateOutDocument.objects.filter(id__in=document_ids)
        for document in documents:
            lines = [i for i in document.items.all() if i.is_active]
            document.total_boxes = sum((i.total_boxes or Decimal("0")) for i in lines)
            document.total_loose = sum((i.total_loose or Decimal("0")) for i in lines)
        SalesDispatchGateOutDocument.objects.bulk_update(
            documents, ["total_boxes", "total_loose"], batch_size=500
        )

        docking_ids = {i.sales_dispatch_id for i in changed_items}
        dockings = SalesDispatchGateOut.objects.filter(id__in=docking_ids)
        for docking in dockings:
            docs = [d for d in docking.documents.all() if d.is_active]
            docking.total_boxes = sum((d.total_boxes or Decimal("0")) for d in docs)
            docking.total_loose = sum((d.total_loose or Decimal("0")) for d in docs)
        SalesDispatchGateOut.objects.bulk_update(
            dockings, ["total_boxes", "total_loose"], batch_size=500
        )
        self.stdout.write(
            f"    rolled up {len(documents)} document(s) and {len(dockings)} docking(s)."
        )

    @staticmethod
    def _fetch_sal_factors(company_code: str, item_codes: list[str]) -> dict:
        """``{item_code: SalFactor2}`` from the company's SAP item master."""
        connection = HanaConnection(CompanyContext(company_code).hana)
        schema = connection.schema
        conn = connection.connect()
        try:
            cursor = conn.cursor()
            factors = {}
            # Chunked so a docking history with thousands of distinct SKUs doesn't build
            # one enormous IN list.
            for start in range(0, len(item_codes), 500):
                chunk = item_codes[start : start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                cursor.execute(
                    f'SELECT "ItemCode", "SalFactor2" FROM "{schema}"."OITM" '
                    f"WHERE \"ItemCode\" IN ({placeholders})",
                    chunk,
                )
                for code, factor in cursor.fetchall():
                    factors[code] = Decimal(str(factor or 0))
            cursor.close()
            return factors
        finally:
            conn.close()
