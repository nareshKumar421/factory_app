"""Account for every confirmed dispatch: why is it, or is it not, on the cut screen?

"We confirmed 276 orders but Ready to cut shows 211" is asked often enough, and the
screen cannot answer it: dispatches suppressed as NOT_REQUIRED appear in no tab at
all, and the Posted tile counts NOTES rather than the dispatches they carry. This
reconciles the two, bucket by bucket, so the missing ones have a name.

Strictly read-only — it opens no transaction and posts nothing.

    python manage.py mp_dn_reconcile --company JIVO_MART
    python manage.py mp_dn_reconcile --company JIVO_MART --sheet 2026-09-03
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from company.models import Company
from marketplace.models import (
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceSapPostStatus,
    OrderImportBatch,
)


class Command(BaseCommand):
    help = "Reconcile confirmed dispatches against the delivery-note cut screen."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--channel", default="FLIPKART")
        parser.add_argument("--sheet", default="",
                            help="Substring of the sheet filename to scope to")

    def handle(self, *args, **opts):
        company = Company.objects.filter(code=opts["company"]).first()
        if company is None:
            raise CommandError(f"No company with code {opts['company']!r}")
        channel = opts["channel"]

        db = connection.settings_dict
        self.stdout.write(f"DB      : {db['NAME']}@{db['HOST']}")
        self.stdout.write(f"Company : {company.code} · {channel}")

        batches = OrderImportBatch.objects.filter(company=company)
        if opts["sheet"]:
            batches = batches.filter(filename__icontains=opts["sheet"])
            names = list(batches.values_list("id", "filename"))
            if not names:
                raise CommandError(f"No sheet matching {opts['sheet']!r}")
            for bid, fn in names:
                self.stdout.write(f"Sheet   : [{bid}] {fn}")

        qs = (MarketplaceDispatch.objects
              .filter(company=company, channel=channel,
                      status=MarketplaceDispatchStatus.CONFIRMED)
              .select_related("order"))
        if opts["sheet"]:
            qs = qs.filter(order__import_batch__in=batches)

        total = qs.count()
        self.stdout.write(f"\nCONFIRMED dispatches: {total}")

        by_status = Counter(qs.values_list("sap_post_status", flat=True))
        self.stdout.write("\nby SAP post status:")
        for st, n in by_status.most_common():
            self.stdout.write(f"   {n:>5}  {st}")

        # What the cut screen would actually show for these.
        from marketplace.services.delivery_note_service import _collect, awaiting_dispatches

        awaiting_ids = set(awaiting_dispatches(company, channel)
                           .values_list("id", flat=True))
        if opts["sheet"]:
            awaiting_ids &= set(qs.values_list("id", flat=True))

        includable, blocked = _collect(company, channel)
        inc_ids = {i["dispatch"].id for i in includable}
        blk = [b for b in blocked if b["dispatch_id"] in awaiting_ids]
        if opts["sheet"]:
            inc_ids &= set(qs.values_list("id", flat=True))

        posted = qs.filter(sap_delivery_note_doc_entry__isnull=False).count()
        notes = qs.filter(sap_delivery_note_doc_entry__isnull=False).values_list(
            "sap_delivery_note_doc_entry", flat=True).distinct().count()

        not_required = by_status.get(MarketplaceSapPostStatus.NOT_REQUIRED, 0)
        awaiting_approval = by_status.get(MarketplaceSapPostStatus.AWAITING_APPROVAL, 0)

        self.stdout.write("\nwhere they are:")
        self.stdout.write(f"   {len(inc_ids):>5}  READY to cut (what the screen shows)")
        self.stdout.write(f"   {len(blk):>5}  BLOCKED (awaiting, but cannot post)")
        self.stdout.write(f"   {posted:>5}  already POSTED, on {notes} note(s)")
        self.stdout.write(f"   {awaiting_approval:>5}  AWAITING_APPROVAL in SAP")

        # These four are where a dispatch IS. NOT_REQUIRED is no longer one of them:
        # since awaiting_dispatches stopped filtering that status, a row carrying it
        # sits in READY like any other and is already counted there. Adding it in as
        # its own bucket double-counted, which read as "1521 of 1511, -10 UNEXPLAINED"
        # — the tool inventing a discrepancy in the very report meant to close one.
        named = len(inc_ids) + len(blk) + posted + awaiting_approval
        self.stdout.write(f"\n   {named} of {total} accounted for"
                          + ("" if named == total else
                             f"  — {total - named} UNEXPLAINED, look at the status table above"))
        if not_required:
            self.stdout.write(
                f"   ({not_required} of them still carry NOT_REQUIRED from before the "
                "suppression was removed — counted above, not a separate bucket)")

        # For the suppressed ones: does the note they point at actually EXIST? A
        # sibling only has to be CONFIRMED to suppress, so if its own note was never
        # cut the goods have no note anywhere and the stock never left SAP.
        supp = qs.filter(sap_post_status=MarketplaceSapPostStatus.NOT_REQUIRED)
        with_note = without = 0
        orphans = []
        for d in supp.select_related("dn_covered_by", "order"):
            cover = d.dn_covered_by
            if cover is not None and cover.sap_delivery_note_doc_entry is not None:
                with_note += 1
            else:
                without += 1
                orphans.append(d)
        if supp.exists():
            self.stdout.write("\nof the NOT_REQUIRED, the note they point at:")
            self.stdout.write(f"   {with_note:>5}  EXISTS in SAP — genuinely already shipped")
            self.stdout.write(
                f"   {without:>5}  has NO delivery note — goods have no note anywhere")
            for d in orphans[:20]:
                cover = d.dn_covered_by
                self.stdout.write(
                    f"      {d.order.order_id}  points at dispatch "
                    f"{cover.id if cover else '—'} "
                    f"(status {cover.sap_post_status if cover else '—'})")

        if blk:
            self.stdout.write("\nblocked reasons:")
            for reason, n in Counter(b["reason"] for b in blk).most_common():
                self.stdout.write(f"   {n:>5}  {reason}")
            for b in blk[:15]:
                self.stdout.write(f"      {b['order_id']}  {b['reason']}")
