"""Retire transfer requests that are still reserving stock nobody expects to move.

An open request line adds to `OITW."IsCommited"` at its source warehouse, and SAP
never expires one. Left alone that drifts: as of 2026-08-26 the three companies
between them held 566 open requests, 484 of them over 90 days old, reserving
roughly 9.7 million units against stock that in many cases is no longer there.

This command exists so the app never adds to that pile. Run it on a schedule.

    python manage.py close_stale_transfer_requests --days 30 --dry-run
    python manage.py close_stale_transfer_requests --days 30 --company JIVO_OIL

Only requests the app raised are touched — a request is matched by its stored
`sap_request_doc_entry`, so pre-existing SAP requests are left for the SAP team.
"""

import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)

from ...models_transfer import (
    TransferPostingStatus,
    TransferRequestStatus,
    WarehouseTransferRequest,
)

logger = logging.getLogger(__name__)

# Statuses where the request will never be served, so its reservation is dead
# weight. An APPROVED request is deliberately excluded — someone may still be
# about to post it.
DEAD_STATUSES = (
    TransferRequestStatus.REJECTED,
    TransferRequestStatus.CANCELLED,
)


class Command(BaseCommand):
    help = "Close app-raised SAP transfer requests that are still reserving stock."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=30,
            help="Close requests older than this many days (default 30).",
        )
        parser.add_argument(
            "--company", type=str, default="",
            help="Limit to one company code, e.g. JIVO_OIL.",
        )
        parser.add_argument(
            "--include-pending", action="store_true",
            help="Also close requests still awaiting a decision. Off by default "
                 "so a slow approver does not lose their request.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be closed without touching SAP.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 1:
            raise CommandError("--days must be at least 1.")

        cutoff = timezone.now() - timezone.timedelta(days=days)
        dry_run = options["dry_run"]

        candidates = (
            WarehouseTransferRequest.objects
            .filter(
                sap_request_doc_entry__isnull=False,
                sap_request_closed_at__isnull=True,
                created_at__lt=cutoff,
            )
            .exclude(posting_status=TransferPostingStatus.POSTED)
            .select_related("company")
            .order_by("created_at")
        )

        if options["company"]:
            candidates = candidates.filter(company__code=options["company"])

        statuses = list(DEAD_STATUSES)
        if options["include_pending"]:
            statuses.append(TransferRequestStatus.PENDING)
        candidates = candidates.filter(status__in=statuses)

        total = candidates.count()
        if not total:
            self.stdout.write(self.style.SUCCESS(
                f"Nothing to close — no app-raised request older than {days} days "
                f"is still reserving stock."
            ))
            return

        self.stdout.write(
            f"{total} request(s) older than {days} days still reserving stock"
            + (" (dry run)" if dry_run else "")
        )

        closed = 0
        failed = 0
        for request in candidates:
            age = (timezone.now() - request.created_at).days
            label = (
                f"{request.entry_no}  {request.from_warehouse}→{request.to_warehouse}  "
                f"SAP {request.sap_request_doc_num or request.sap_request_doc_entry}  "
                f"{request.status}  {age}d"
            )

            if dry_run:
                self.stdout.write(f"  would close  {label}")
                continue

            try:
                # Close, not Cancel — SAP refuses Cancel on this entity even
                # while the request is open.
                from sap_client.client import SAPClient
                SAPClient(company_code=request.company.code).close_transfer_request(
                    request.sap_request_doc_entry
                )
            except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
                failed += 1
                self.stderr.write(self.style.WARNING(f"  FAILED  {label}: {exc}"))
                logger.error(
                    "Could not close stale SAP request %s (%s): %s",
                    request.sap_request_doc_entry, request.entry_no, exc,
                )
                continue

            request.sap_request_closed_at = timezone.now()
            request.save(update_fields=["sap_request_closed_at", "updated_at"])
            closed += 1
            self.stdout.write(self.style.SUCCESS(f"  closed  {label}"))

        if dry_run:
            self.stdout.write(f"\nDry run — {total} request(s) would be closed.")
            return

        summary = f"\nClosed {closed} of {total} request(s)."
        if failed:
            self.stdout.write(self.style.WARNING(
                summary + f" {failed} failed and are still reserving stock — "
                f"they will be retried on the next run."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
