"""Retire scans left over from a CHANGED tracking id, so they don't double-count.

Background: when Flipkart re-manifests an order it re-lists the order with a new
Tracking ID. The order's line tracking is updated, but any scan made against the OLD
tracking stays active — so the item is counted twice and confirming the order fails
with "Scan counts deviate from the order." (``confirm_service`` SCAN_DEVIATION).

This command finds every non-cancelled dispatch that has an active scan whose
tracking prefix (``barcode_raw`` before ``#``) is NOT one of the order's CURRENT line
tracking IDs, deactivates those stale scans, and verifies the order's scanned
quantities now match its resolved finished goods.

Idempotent and repeatable. Dry-run by default; pass ``--apply`` to write.

    python manage.py mp_fix_stale_scans                    # dry run (JIVO_MART/FLIPKART)
    python manage.py mp_fix_stale_scans --apply            # deactivate stale scans
    python manage.py mp_fix_stale_scans --channel FLIPKART --company JIVO_MART --apply
"""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company
from marketplace.models import (
    MarketplaceChannel, MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceScan,
)
from marketplace.services.resolve_service import fg_lines, load_mappings, resolve_lines
from marketplace.services.scan_service import build_progress


def _stale_scans(dispatch):
    """Active scans whose tracking prefix is not one of the order's current line
    tracking IDs (leftovers from a since-changed tracking)."""
    tids = {
        (l.tracking_id or "").strip()
        for l in dispatch.order.lines.all()
        if (l.tracking_id or "").strip()
    }
    if not tids:
        return []
    return [
        s for s in dispatch.scans.all()
        if s.is_active and (s.barcode_raw or "").split("#", 1)[0] not in tids
    ]


def _deviation(dispatch, mappings):
    """Over/under rows comparing active-scan quantities to resolved finished goods."""
    smap = {}
    for s in dispatch.scans.all():
        if not s.is_active:
            continue
        k = (s.item_code or "").upper()
        smap[k] = smap.get(k, Decimal("0")) + Decimal(s.quantity)
    lines = list(dispatch.order.lines.all())
    flines = fg_lines(
        resolve_lines(lines, dispatch.order.sap_warehouse_code or "", mappings)["resolved_lines"]
    )
    return [r for r in build_progress(flines, smap) if r["status"] in ("UNDER", "OVER")]


class Command(BaseCommand):
    help = "Retire scans left over from a changed tracking id (fixes SCAN_DEVIATION)."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=getattr(settings, "MARKETPLACE_COMPANY_CODE", "JIVO_MART"))
        parser.add_argument("--channel", default="FLIPKART")
        parser.add_argument("--apply", action="store_true", help="Write changes (otherwise dry run).")

    def handle(self, *args, **opts):
        company_code, channel, apply = opts["company"], opts["channel"], opts["apply"]
        try:
            company = Company.objects.get(code=company_code)
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {company_code!r}")

        mappings = load_mappings(company, channel)
        qs = (
            MarketplaceDispatch.objects.filter(company=company, channel=channel)
            .exclude(status=MarketplaceDispatchStatus.CANCELLED)
            .prefetch_related("scans", "order__lines")
        )

        affected, retired, cleaned, still_bad = 0, 0, 0, []

        @transaction.atomic
        def run():
            nonlocal affected, retired, cleaned, still_bad
            for d in qs.iterator(chunk_size=300):
                stale = _stale_scans(d)
                if not stale:
                    continue
                affected += 1
                retired += len(stale)
                for s in stale:
                    s.is_active = False
                MarketplaceScan.objects.bulk_update(stale, ["is_active"])
                # Re-read and verify the deviation is gone.
                d2 = MarketplaceDispatch.objects.prefetch_related("scans", "order__lines").get(pk=d.pk)
                if _deviation(d2, mappings):
                    still_bad.append(d2.order.order_id)
                else:
                    cleaned += 1
                self.stdout.write(f"  {d.order.order_id}: retired {len(stale)} stale scan(s)")
            if not apply:
                transaction.set_rollback(True)

        run()
        verb = "WROTE" if apply else "DRY RUN — would write"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb}: {affected} orders, {retired} stale scans retired · "
            f"{cleaned} now clean · {len(still_bad)} still deviating (needs review)."
        ))
        if still_bad:
            self.stdout.write(self.style.WARNING(f"Still deviating (genuine mis-scan?): {still_bad}"))
        if not apply:
            self.stdout.write("Re-run with --apply to commit.")
