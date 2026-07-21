"""Backfill stock settlement for dockings that were dispatched before the
docking flow settled its boxes.

Historically ``mark_docking_dispatched`` did not touch the barcode Box/Pallet
records, so boxes scanned onto a dispatched docking stayed ACTIVE and their
pallets kept occupying their Warehouse Ops bins (phantom stock). This command
replays the now-standard settlement over already-DISPATCHED dockings: it marks
their still-active scanned boxes DISPATCHED and reconciles the pallets, freeing
the stranded WMS bins. It is idempotent (already-dispatched boxes are skipped),
so it can be run repeatedly.

Examples::

    python manage.py backfill_docking_box_dispatch --dry-run
    python manage.py backfill_docking_box_dispatch --company JIVO_OIL
    python manage.py backfill_docking_box_dispatch --entry 601
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from barcode.models import Box, BoxStatus
from barcode.services.dispatch_settlement import settle_dispatched_boxes
from gate_core.models import SalesDispatchGateOutStatus
from gate_core.models.sales_dispatch import SalesDispatchGateOut


class Command(BaseCommand):
    help = "Settle barcode stock for dockings dispatched before box settlement existed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", dest="company", default=None,
            help="Limit to one company by code (e.g. JIVO_OIL).",
        )
        parser.add_argument(
            "--entry", dest="entry", type=int, default=None,
            help="Limit to a single SalesDispatchGateOut id.",
        )
        parser.add_argument(
            "--dry-run", dest="dry_run", action="store_true",
            help="Report what would be settled without writing anything.",
        )

    def handle(self, *args, **options):
        qs = (
            SalesDispatchGateOut.objects
            .filter(status=SalesDispatchGateOutStatus.DISPATCHED)
            .select_related("company")
            .order_by("id")
        )
        if options["company"]:
            qs = qs.filter(company__code=options["company"])
        if options["entry"]:
            qs = qs.filter(id=options["entry"])

        dry_run = options["dry_run"]
        total_entries = 0
        total_boxes = 0
        total_pallets = 0

        for entry in qs.iterator():
            scans = (
                entry.box_scans.filter(is_active=True)
                .select_related("box", "box__pallet")
            )
            boxes = []
            for scan in scans:
                box = scan.box
                if box is None and scan.box_barcode:
                    box = Box.objects.filter(
                        company=entry.company, box_barcode=scan.box_barcode
                    ).select_related("pallet").first()
                if box is not None:
                    boxes.append(box)

            pending = [b for b in boxes if b.status in (BoxStatus.ACTIVE, BoxStatus.PARTIAL)]
            if not pending:
                continue

            total_entries += 1
            pallet_ids = {b.pallet_id for b in pending if b.pallet_id}

            if dry_run:
                total_boxes += len(pending)
                total_pallets += len(pallet_ids)
                self.stdout.write(
                    f"[dry-run] docking #{entry.id} ({entry.company.code}): "
                    f"would settle {len(pending)} box(es) across {len(pallet_ids)} pallet(s)."
                )
                continue

            with transaction.atomic():
                result = settle_dispatched_boxes(entry.company, pending, user=None)
            total_boxes += result["boxes_dispatched"]
            total_pallets += result["pallets_reconciled"]
            self.stdout.write(
                f"docking #{entry.id} ({entry.company.code}): "
                f"settled {result['boxes_dispatched']} box(es), "
                f"reconciled {result['pallets_reconciled']} pallet(s)."
            )

        verb = "Would settle" if dry_run else "Settled"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {total_boxes} box(es) across {total_pallets} pallet(s) "
            f"in {total_entries} docking(s)."
        ))
