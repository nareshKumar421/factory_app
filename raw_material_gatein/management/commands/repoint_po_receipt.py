"""Move a received PO onto a different open PO for the same vendor.

The command-line twin of ``POReceiptRepointAPI``, for the correction that has to
be made before the screen exists for it — a PO that ran out between gate-in and
GRPO posting, on an entry that is already completed and QC-finished.

    python manage.py repoint_po_receipt --po-receipt 1347 --to-po 220926064 \\
        --reason "220826133 consumed by GRPO 2026096695" --dry-run

Nothing is written without ``--commit``; ``--dry-run`` (the default) rolls back
after running every check, so it reports exactly what the real run would do.
"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from raw_material_gatein.models import POReceipt
from raw_material_gatein.services import RepointError, repoint_po_receipt


class Command(BaseCommand):
    help = "Move a received PO onto a different open PO, keeping its items and QC."

    def add_arguments(self, parser):
        parser.add_argument(
            "--po-receipt", type=int, required=True,
            help="POReceipt id to move (shown on the GRPO preview screen).",
        )
        parser.add_argument(
            "--to-po", required=True,
            help="Replacement PO number (SAP DocNum) with open quantity.",
        )
        parser.add_argument(
            "--reason", required=True,
            help="Why the move is needed — stored on the replacement log.",
        )
        parser.add_argument(
            "--user", default=None,
            help="Email of the user to record as having made the change.",
        )
        parser.add_argument(
            "--commit", action="store_true",
            help="Actually write the change. Without it the run is rolled back.",
        )

    def handle(self, *args, **options):
        try:
            po_receipt = POReceipt.objects.select_related("vehicle_entry").get(
                id=options["po_receipt"]
            )
        except POReceipt.DoesNotExist:
            raise CommandError(f"PO receipt {options['po_receipt']} does not exist.")

        user = None
        if options["user"]:
            from django.contrib.auth import get_user_model

            user = get_user_model().objects.filter(email=options["user"]).first()
            if user is None:
                raise CommandError(f"No user with email {options['user']}.")

        entry = po_receipt.vehicle_entry
        company_code = entry.company.code

        self.stdout.write(
            f"{entry.entry_no} / receipt {po_receipt.id}: PO {po_receipt.po_number} "
            f"(DocEntry {po_receipt.sap_doc_entry}) -> PO {options['to_po']} "
            f"in {company_code}"
        )

        try:
            with transaction.atomic():
                summary = repoint_po_receipt(
                    po_receipt,
                    new_po_number=options["to_po"],
                    reason=options["reason"],
                    company_code=company_code,
                    user=user,
                )
                if not options["commit"]:
                    raise _Rollback(summary)
        except RepointError as exc:
            raise CommandError(str(exc))
        except _Rollback as rollback:
            self.stdout.write(json.dumps(rollback.summary, indent=2, default=str))
            self.stdout.write(
                self.style.WARNING("Dry run — rolled back. Re-run with --commit.")
            )
            return

        self.stdout.write(json.dumps(summary, indent=2, default=str))
        self.stdout.write(self.style.SUCCESS("Repointed."))


class _Rollback(Exception):
    """Unwinds the dry run after every check has had its say."""

    def __init__(self, summary):
        super().__init__("dry run")
        self.summary = summary
