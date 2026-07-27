"""Backfill scan rows for orders that were CONFIRMED without being scanned.

Before the "must scan every Tracking ID before confirm" rule, an order could be
dispatched (Delivery Note posted) with no scan recorded. Those orders now show
truthfully on the Outward board as confirmed-but-unscanned (grey Tracking IDs),
and they can no longer be scanned through the app (a CONFIRMED dispatch rejects
scans as duplicates).

This one-off command reconstructs the scan rows that the normal Tracking-ID scan
WOULD have created for such orders, so their audit trail reads "scanned". It only
writes local scan records — it does NOT touch SAP, re-post a Delivery Note, or
change any stock. It is idempotent (skips dispatches already fully scanned and
barcodes that already exist).

    python manage.py mp_backfill_confirmed_scans                     # dry run
    python manage.py mp_backfill_confirmed_scans --apply             # write scans
    python manage.py mp_backfill_confirmed_scans --order OD1 --order OD2 --apply
    python manage.py mp_backfill_confirmed_scans --user-email ops@jivo.in --apply
"""
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company
from marketplace.models import (
    ComboComponentType,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceScan,
)
from marketplace.services.resolve_service import fg_lines, load_mappings, resolve_lines
from marketplace.services.scan_service import dispatch_is_fully_scanned


class Command(BaseCommand):
    help = "Backfill scan rows for orders confirmed without a scan (local audit only)."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=getattr(settings, "MARKETPLACE_COMPANY_CODE", "JIVO_MART"))
        parser.add_argument("--channel", default="FLIPKART")
        parser.add_argument(
            "--order", action="append", default=[],
            help="Limit to these order IDs (repeatable). Omit to do every confirmed order.",
        )
        parser.add_argument("--user-email", default="", help="Attribute the scans to this user (optional).")
        parser.add_argument("--apply", action="store_true", help="Write scans (otherwise dry run).")

    def handle(self, *args, **opts):
        company_code, channel, apply = opts["company"], opts["channel"], opts["apply"]
        try:
            company = Company.objects.get(code=company_code)
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {company_code!r}.")

        user = None
        if opts["user_email"]:
            user = get_user_model().objects.filter(email=opts["user_email"]).first()
            if user is None:
                raise CommandError(f"No user with email {opts['user_email']!r}.")

        dispatches = (
            MarketplaceDispatch.objects.filter(
                company=company, channel=channel,
                status=MarketplaceDispatchStatus.CONFIRMED,
            )
            .select_related("order")
            .prefetch_related("order__lines", "order__lines__chosen_option__combo__components", "scans")
            .order_by("order__order_id")
        )
        if opts["order"]:
            dispatches = dispatches.filter(order__order_id__in=opts["order"])

        mappings = load_mappings(company, channel)
        mode = "APPLY" if apply else "DRY RUN"
        self.stdout.write(f"[{mode}] {company_code}/{channel} — scanning confirmed dispatches…")

        total_orders = total_scans = skipped_ok = 0
        planned = []  # (order_id, [barcodes]) for reporting

        for dispatch in dispatches:
            if dispatch_is_fully_scanned(dispatch):
                skipped_ok += 1
                continue
            rows = self._missing_scan_rows(dispatch, mappings)
            if not rows:
                continue
            total_orders += 1
            total_scans += len(rows)
            planned.append((dispatch.order.order_id, [r["barcode_raw"] for r in rows]))
            if apply:
                self._write(dispatch, rows, company, user)

        for order_id, barcodes in planned:
            self.stdout.write(f"  {order_id}: +{len(barcodes)} scan(s) — {', '.join(barcodes)}")

        verb = "backfilled" if apply else "would backfill"
        self.stdout.write(self.style.SUCCESS(
            f"{mode}: {verb} {total_scans} scan(s) across {total_orders} order(s); "
            f"{skipped_ok} already fully scanned."
        ))
        if not apply and total_scans:
            self.stdout.write("Re-run with --apply to write these scans.")

    def _missing_scan_rows(self, dispatch, mappings):
        """The scan rows the normal Tracking-ID scan would have created but didn't.

        Mirrors ``scan_service.scan_dispatch_by_tracking``: each Tracking ID
        completes its own item's FG line(s). Orders with no per-line Tracking ID
        fall back to a bare item-code barcode (the finished-goods quantity check)."""
        order = dispatch.order
        existing = {s.barcode_raw for s in dispatch.scans.all()}
        seen = set()
        rows = []

        by_tracking = {}
        for line in order.lines.all():
            tid = (line.tracking_id or order.tracking_id or "").strip()
            by_tracking.setdefault(tid, []).append(line)

        for tid, tlines in by_tracking.items():
            resolved = resolve_lines(tlines, order.sap_warehouse_code or "", mappings)
            for fl in fg_lines(resolved["resolved_lines"]):
                barcode = f"{tid}#{fl['item_code']}" if tid else fl["item_code"]
                if barcode in existing or barcode in seen:
                    continue
                seen.add(barcode)
                rows.append({
                    "barcode_raw": barcode,
                    "item_code": fl["item_code"],
                    "item_name": fl["item_name"],
                    "source_sku": (fl["source_skus"][0] if fl["source_skus"] else ""),
                    "quantity": Decimal(fl["required_quantity"]),
                    "uom": fl["uom"],
                    "warehouse_code": fl["warehouse_code"],
                })
        return rows

    @transaction.atomic
    def _write(self, dispatch, rows, company, user):
        MarketplaceScan.objects.bulk_create([
            MarketplaceScan(
                company=company, dispatch=dispatch, barcode_raw=r["barcode_raw"],
                item_code=r["item_code"], item_name=r["item_name"],
                component_type=ComboComponentType.FG, source_sku=r["source_sku"],
                quantity=r["quantity"], uom=r["uom"], warehouse_code=r["warehouse_code"],
                scanned_by=user,
            )
            for r in rows
        ])
