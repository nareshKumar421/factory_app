"""Re-open a re-manifested parcel on an ALREADY-imported sheet, without re-uploading.

LEGACY-DATA TOOL. Sheets are now independent scanning sessions: an order re-listed on
a later sheet is imported there as its OWN row and scanned there, so no order is ever
carried over and no new DISPATCHED skip is created. This command only has work to do
on sheets imported under the OLD carry-over behaviour, which left such an order as a
"carried over" skip (``MarketplaceImportSkip`` reason=DISPATCHED) instead. On a sheet
imported since, it finds no skips and does nothing.

This command applies the same correction in place, so an operator who can't
re-upload the sheet still gets the parcel into "To scan". For each targeted
DISPATCHED skip on ``--batch`` whose kept order's latest live dispatch is CONFIRMED
and whose sheet Tracking ID now differs from the stored line tracking, it:

  * re-tracks the order's lines to the new Tracking ID (via the same
    _retrack_carried_over used at import; the CONFIRMED dispatch is left intact),
  * opens a fresh DRAFT dispatch so the parcel leads and is scannable,
  * moves the order onto ``--batch`` (the sheet the operator is looking at), and
  * deletes the now-obsolete carried-over skip.

Idempotent: a second run finds the tracking already updated and does nothing.

    python manage.py mp_rescan_remanifested --batch 51                       # dry run
    python manage.py mp_rescan_remanifested --batch 51 --order-id OD4381...   # one order
    python manage.py mp_rescan_remanifested --batch 51 --apply
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from marketplace.models import (
    ImportSkipReason,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceImportSkip,
    MarketplaceOrder,
    OrderImportBatch,
)
from marketplace.services.order_import_service import (
    _group_by_order,
    _retrack_carried_over,
    parse_rows_for,
)


class Command(BaseCommand):
    help = "Re-open re-manifested (changed-tracking) parcels stuck as carried-over on a sheet."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, required=True,
                            help="OrderImportBatch id of the sheet showing the carried-over order(s).")
        parser.add_argument("--order-id", default="",
                            help="Only process this order id (default: every eligible skip on the batch).")
        parser.add_argument("--file", default="",
                            help="Path to the sheet CSV. Falls back to the batch's retained raw_file, "
                                 "then (for single-tracking orders) the skip's stored tracking_ids.")
        parser.add_argument("--apply", action="store_true", help="Write changes (else dry run).")

    def handle(self, *args, **opts):
        batch = OrderImportBatch.objects.filter(id=opts["batch"]).select_related("company").first()
        if batch is None:
            raise CommandError(f"No OrderImportBatch with id {opts['batch']}.")
        company, channel = batch.company, batch.channel

        # Prefer the actual sheet (exact per-line Tracking IDs). Fall back to the skip's
        # stored tracking_ids for unambiguous single-tracking orders when the sheet isn't
        # reachable (e.g. run off-server where media isn't mounted).
        by_order = {}
        content = None
        if opts["file"]:
            try:
                with open(opts["file"], "rb") as fh:
                    content = fh.read()
            except OSError as exc:
                raise CommandError(f"Could not read {opts['file']!r}: {exc}")
        elif batch.raw_file:
            try:
                content = batch.raw_file.read()
            except Exception as exc:  # media not reachable from here
                self.stdout.write(self.style.WARNING(
                    f"Retained sheet not readable ({exc}); using stored skip tracking_ids where unambiguous."))
        if content is not None:
            by_order, _skipped = _group_by_order(
                parse_rows_for(channel, content=content, filename=(opts["file"] or batch.raw_file.name)))

        # Carried-over (DISPATCHED) skips recorded on this sheet — the candidates.
        skips = MarketplaceImportSkip.objects.filter(
            company=company, import_batch=batch, reason=ImportSkipReason.DISPATCHED,
        ).select_related("kept_order")
        if opts["order_id"]:
            skips = skips.filter(order_id=opts["order_id"])

        mode = "APPLY" if opts["apply"] else "DRY RUN"
        self.stdout.write(f"[{mode}] batch {batch.id} ({batch.filename}) — {skips.count()} DISPATCHED skip(s)")

        acted = skipped_same = skipped_notconfirmed = 0
        for skip in skips:
            order = skip.kept_order
            oid = skip.order_id
            order_rows = by_order.get(oid)
            if not order_rows:
                # No sheet rows — synthesize from the skip's stored tracking_ids, but only
                # when unambiguous: a single-line order (one line -> the one new tracking).
                lines = list(order.lines.all())
                new_tids = [t.strip() for t in (skip.tracking_ids or []) if t.strip()]
                if len(lines) == 1 and len(set(new_tids)) == 1:
                    ln = lines[0]
                    order_rows = [{"tracking": new_tids[0], "order_item_id": ln.order_item_id,
                                   "sku": ln.marketplace_sku}]
                else:
                    self.stdout.write(
                        f"   {oid}: sheet not available and mapping is ambiguous "
                        f"({len(lines)} line(s), trackings={new_tids}) — re-run on the server "
                        "or pass --file. Skipped.")
                    continue

            latest = (
                MarketplaceDispatch.objects.filter(company=company, order=order)
                .exclude(status=MarketplaceDispatchStatus.CANCELLED)
                .order_by("-created_at", "-id").first()
            )
            if latest is None:
                self.stdout.write(f"   {oid}: no live dispatch — skip")
                continue
            if latest.status != MarketplaceDispatchStatus.CONFIRMED:
                # A live-but-unconfirmed carry-over is the in-place re-track case; the
                # normal path handles it. Left for safety — report and move on.
                skipped_notconfirmed += 1
                self.stdout.write(f"   {oid}: latest dispatch {latest.status} (not CONFIRMED) — skip")
                continue

            old_tids = sorted({(l.tracking_id or "").strip() for l in order.lines.all() if (l.tracking_id or "").strip()})
            new_tids = sorted({(r.get("tracking") or "").strip() for r in order_rows if (r.get("tracking") or "").strip()})
            if old_tids == new_tids:
                skipped_same += 1
                self.stdout.write(f"   {oid}: tracking unchanged {old_tids} — true carry-over, leave as is")
                continue

            self.stdout.write(
                f"   {oid}: {old_tids} → {new_tids}  (re-track, fresh DRAFT dispatch, move to batch {batch.id})")
            # Under the current model the sheet may already hold its OWN row for this
            # order, and moving this one onto the batch would collide with
            # uniq_mp_order_per_sheet. Say so plainly instead of raising IntegrityError:
            # that sheet already has the order and there is nothing to move.
            if MarketplaceOrder.objects.filter(
                company=company, channel=channel, order_id=oid, import_batch=batch,
            ).exclude(pk=order.pk).exists():
                self.stdout.write(
                    f"   {oid}: batch {batch.id} already has its own row for this order "
                    f"— scan it there; nothing to move")
                continue

            if opts["apply"]:
                with transaction.atomic():
                    # Re-track lines to the new parcel; pass dispatch=None so the CONFIRMED
                    # dispatch's scans are preserved (history), not retired.
                    if _retrack_carried_over(order, order_rows, dispatch=None):
                        MarketplaceDispatch.objects.create(
                            company=company, channel=channel, order=order,
                            import_batch=batch,
                            sap_warehouse_code=order.sap_warehouse_code or "",
                            status=MarketplaceDispatchStatus.DRAFT,
                        )
                        order.import_batch = batch
                        order.updated_at = timezone.now()
                        order.save(update_fields=["import_batch", "tracking_id", "updated_at"])
                        skip.delete()
                        acted += 1

        self.stdout.write(
            f"eligible-acted={acted}, unchanged-tracking={skipped_same}, not-confirmed={skipped_notconfirmed}")
        if not opts["apply"]:
            self.stdout.write("Re-run with --apply to write.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Done. Re-opened {acted} parcel(s) into 'To scan' on batch {batch.id}."))
