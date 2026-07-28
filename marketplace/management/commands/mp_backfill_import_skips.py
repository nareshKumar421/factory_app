"""Backfill carried-over (import-skip) records for a PAST sheet from its CSV.

New imports record skips automatically (see order_import_service.ingest). Existing
batches can't be explained retroactively because the original CSV order IDs were
discarded and the uploaded file is not retained (OrderImportBatch.raw_file is unset
for historical batches). This command reconstructs the skips for one batch when the
operator re-supplies the exact CSV that produced it.

Only the DISPATCHED skip is reconstructable: an order that is in the CSV, exists,
did NOT land on this batch, and has a live dispatch was left on its original sheet.
DUPLICATE skips (skip_duplicates) aren't reconstructable without the original choice
and are reported but not written. Idempotent (unique per batch+order_id).

    python manage.py mp_backfill_import_skips --batch 47 --file "/path/Order-CSV ….csv"          # dry run
    python manage.py mp_backfill_import_skips --batch 47 --file "/path/Order-CSV ….csv" --apply
"""
from django.core.management.base import BaseCommand, CommandError

from marketplace.models import (
    ImportSkipReason,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceImportSkip,
    MarketplaceOrder,
    OrderImportBatch,
)
from marketplace.services.order_import_service import _group_by_order, parse_rows


class Command(BaseCommand):
    help = "Reconstruct carried-over import-skip records for a past sheet from its CSV."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, required=True, help="OrderImportBatch id.")
        parser.add_argument("--file", required=True, help="Path to the original uploaded CSV.")
        parser.add_argument("--apply", action="store_true", help="Write records (else dry run).")

    def handle(self, *args, **opts):
        batch = OrderImportBatch.objects.filter(id=opts["batch"]).select_related("company").first()
        if batch is None:
            raise CommandError(f"No OrderImportBatch with id {opts['batch']}.")
        try:
            with open(opts["file"], encoding="utf-8-sig") as fh:
                text = fh.read()
        except OSError as exc:
            raise CommandError(f"Could not read {opts['file']!r}: {exc}")

        company, channel = batch.company, batch.channel
        by_order, _skipped = _group_by_order(parse_rows(text))

        existing = {
            o.order_id: o
            for o in MarketplaceOrder.objects.filter(
                company=company, channel=channel, order_id__in=list(by_order)
            ).select_related("import_batch")
        }
        ids = [o.id for o in existing.values()]
        dispatched = set(
            MarketplaceDispatch.objects.filter(company=company, order_id__in=ids)
            .exclude(status=MarketplaceDispatchStatus.CANCELLED)
            .values_list("order_id", flat=True)
        ) if ids else set()

        recs = []
        for oid, order_rows in by_order.items():
            o = existing.get(oid)
            if o is None or o.import_batch_id == batch.id or o.id not in dispatched:
                continue  # created here / imported here / not a dispatched-skip
            recs.append(MarketplaceImportSkip(
                company=company, import_batch=batch, kept_order=o, order_id=oid,
                reason=ImportSkipReason.DISPATCHED, row_count=len(order_rows),
                tracking_ids=[r["tracking"].strip() for r in order_rows if r["tracking"].strip()],
            ))

        expected = (batch.summary or {}).get("dispatched_skipped")
        mode = "APPLY" if opts["apply"] else "DRY RUN"
        self.stdout.write(f"[{mode}] batch {batch.id} ({batch.filename})")
        for r in recs:
            self.stdout.write(f"   {r.order_id} → kept on batch {r.kept_order.import_batch_id} "
                              f"({r.row_count} row(s))")
        self.stdout.write(
            f"reconstructed {len(recs)} DISPATCHED skip(s); "
            f"summary.dispatched_skipped = {expected}."
        )
        if expected is not None and len(recs) != expected:
            self.stdout.write(self.style.WARNING(
                "  count differs from summary — data may have changed since import, "
                "or the supplied CSV isn't the exact original."
            ))
        if opts["apply"] and recs:
            created = MarketplaceImportSkip.objects.bulk_create(recs, ignore_conflicts=True)
            self.stdout.write(self.style.SUCCESS(f"Wrote {len(created)} skip record(s) (idempotent)."))
        elif not opts["apply"]:
            self.stdout.write("Re-run with --apply to write.")
