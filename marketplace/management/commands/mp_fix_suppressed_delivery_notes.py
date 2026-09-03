"""Find dispatches wrongly marked NOT_REQUIRED and put them back in the DN queue.

``confirm_service._already_shipped_elsewhere`` used to ask "has any other row of this
Order ID confirmed?" instead of "did another row ship THIS parcel?". A multi-parcel
order whose boxes went out on different sheets tripped it: T1 shipped and got its
note, then T2 — scanned on a later sheet as its own order row — was marked
NOT_REQUIRED and pointed at T1's note.

``awaiting_dispatches`` skips NOT_REQUIRED, so those parcels never reach the cut
screen on any path, and ``retry_delivery_note`` refuses them by design. The goods
left the warehouse; SAP was never told. That is un-issued stock sitting in inventory
with nothing flagged.

This finds them by the rule the fix now applies — a suppression is only right when a
sibling actually shipped this dispatch's Tracking IDs — and returns the wrong ones to
PENDING so the bulk cut picks them up. It posts nothing to SAP itself.

    python manage.py mp_fix_suppressed_delivery_notes --company JIVO_MART
    python manage.py mp_fix_suppressed_delivery_notes --company JIVO_MART --apply
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from company.models import Company
from marketplace.models import (
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceSapPostStatus,
)
from marketplace.services.confirm_service import _shipped_by_siblings
from marketplace.services.scan_service import dispatch_lines


class Command(BaseCommand):
    help = "Return wrongly-suppressed dispatches to the delivery-note queue."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Company code, e.g. JIVO_MART")
        parser.add_argument("--channel", default="", help="Limit to one channel")
        parser.add_argument("--apply", action="store_true", help="Actually re-queue them")
        parser.add_argument("--out", default="", help="Write the findings to this CSV")

    def handle(self, *args, **opts):
        company = Company.objects.filter(code=opts["company"]).first()
        if company is None:
            raise CommandError(f"No company with code {opts['company']!r}")

        qs = (
            MarketplaceDispatch.objects
            .filter(company=company,
                    status=MarketplaceDispatchStatus.CONFIRMED,
                    sap_post_status=MarketplaceSapPostStatus.NOT_REQUIRED,
                    sap_delivery_note_doc_entry__isnull=True)
            .select_related("order", "dn_covered_by")
            .prefetch_related("order__lines", "scans")
            .order_by("order__order_id", "id")
        )
        if opts["channel"]:
            qs = qs.filter(channel=opts["channel"])

        db = connection.settings_dict
        self.stdout.write(f"DB      : {db['NAME']}@{db['HOST']}")
        self.stdout.write(f"Company : {company.code}")
        self.stdout.write(f"Checking {qs.count()} suppressed dispatch(es)")

        wrong, correct = [], 0
        for d in qs:
            mine = {(l.tracking_id or "").strip() for l in dispatch_lines(d)
                    if (l.tracking_id or "").strip()}
            stamped = {t for t in (d.shipped_trackings or []) if t}
            mine = mine or stamped
            if not mine:
                correct += 1        # nothing to compare — leave it alone
                continue
            siblings = list(
                MarketplaceDispatch.objects
                .filter(company=company, channel=d.channel,
                        order__order_id=d.order.order_id)
                .exclude(pk=d.pk)
                .exclude(order=d.order)
                .exclude(status=MarketplaceDispatchStatus.CANCELLED)
                .prefetch_related("scans")
            )
            gone = _shipped_by_siblings(siblings)
            if gone is None or mine <= gone:
                correct += 1
                continue
            wrong.append((d, sorted(mine - (gone or set()))))

        self.stdout.write("")
        self.stdout.write(f"  correctly suppressed : {correct}")
        self.stdout.write(self.style.WARNING(
            f"  WRONGLY suppressed   : {len(wrong)}  (goods shipped, SAP never told)"
        ))
        for d, missing in wrong[:30]:
            self.stdout.write(
                f"    dispatch {d.id:<7} {d.order.order_id:<24} "
                f"parcels no note covers: {', '.join(missing)}"
            )
        if len(wrong) > 30:
            self.stdout.write(f"    ... and {len(wrong) - 30} more")

        if opts["out"]:
            with open(opts["out"], "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["Dispatch", "Order Id", "Uncovered tracking IDs",
                            "Was pointed at DN"])
                for d, missing in wrong:
                    w.writerow([d.id, d.order.order_id, " ".join(missing),
                                (d.dn_covered_by.sap_delivery_note_num
                                 if d.dn_covered_by_id else "")])
            self.stdout.write(f"\nWrote {len(wrong)} row(s) to {opts['out']}")

        if not wrong:
            return
        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing written. Re-run with --apply to return these to "
                "the delivery-note queue (they will appear under Ready to cut)."
            ))
            return

        with transaction.atomic():
            for d, _missing in wrong:
                d.sap_post_status = MarketplaceSapPostStatus.PENDING
                d.dn_covered_by = None
                d.save(update_fields=["sap_post_status", "dn_covered_by", "updated_at"])
        self.stdout.write(self.style.SUCCESS(
            f"\nRe-queued {len(wrong)} dispatch(es). Cut their notes from "
            "SAP Delivery Notes -> Ready."
        ))
