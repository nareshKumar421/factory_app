"""Report where the app and SAP disagree about warehouse transfers.

Run it on a schedule and read it. Every finding is stock that is mis-stated
somewhere: a reservation the app thinks it released but SAP still holds, a
cross-branch move parked in an in-transit warehouse, a quantity the app recorded
differently from what SAP served.

    python manage.py report_transfer_reconciliation
    python manage.py report_transfer_reconciliation --company JIVO_OIL --json
    python manage.py report_transfer_reconciliation --all --severity critical

Exits non-zero when anything critical is found, so a scheduler can alert on it.
"""

import json
import logging

from django.core.management.base import BaseCommand

from company.models import Company
from sap_client.exceptions import SAPConnectionError, SAPDataError

from ...services.transfer_request_service import TransferRequestService

logger = logging.getLogger(__name__)

SEVERITY_STYLE = {
    "critical": "ERROR",
    "warning": "WARNING",
    "info": "SUCCESS",
}


class Command(BaseCommand):
    help = "Report drift between app transfer requests and their SAP documents."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", type=str, default="",
            help="Limit to one company code, e.g. JIVO_OIL. Default: all active.",
        )
        parser.add_argument(
            "--all", action="store_true", dest="include_settled",
            help="Also check requests that are fully posted and released.",
        )
        parser.add_argument(
            "--severity", type=str, default="",
            choices=["critical", "warning", "info"],
            help="Only show findings at this severity.",
        )
        parser.add_argument(
            "--limit", type=int, default=500,
            help="Requests to check per company (default 500).",
        )
        parser.add_argument(
            "--json", action="store_true", dest="as_json",
            help="Emit JSON instead of a table, for piping somewhere.",
        )

    def handle(self, *args, **options):
        companies = self._companies(options["company"])
        if not companies:
            self.stderr.write(self.style.WARNING("No matching company."))
            return

        results: dict[str, dict] = {}
        critical = 0

        for company in companies:
            try:
                report = TransferRequestService(company.code).reconcile(
                    include_settled=options["include_settled"],
                    limit=options["limit"],
                )
            except (SAPConnectionError, SAPDataError) as exc:
                # One unreachable company must not hide the others' findings.
                self.stderr.write(self.style.ERROR(
                    f"{company.code}: could not reach SAP — {exc}"
                ))
                logger.error("Reconciliation failed for %s: %s", company.code, exc)
                continue

            if options["severity"]:
                report["findings"] = [
                    f for f in report["findings"]
                    if f["severity"] == options["severity"]
                ]
            results[company.code] = report
            critical += sum(
                1 for f in report["findings"] if f["severity"] == "critical"
            )

        if options["as_json"]:
            self.stdout.write(json.dumps(results, indent=2, default=str))
        else:
            self._render(results)

        if critical:
            # Non-zero so a scheduled run is visibly failing, not quietly noisy.
            raise SystemExit(1)

    # ------------------------------------------------------------------

    def _companies(self, code: str):
        qs = Company.objects.all()
        if code:
            qs = qs.filter(code=code)
        return list(qs.order_by("code"))

    def _render(self, results: dict) -> None:
        total_findings = 0

        for company_code, report in results.items():
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"{company_code} — checked {report['checked']} request(s)"
            ))

            findings = report["findings"]
            if not findings:
                self.stdout.write(self.style.SUCCESS(
                    "  Nothing adrift: the app and SAP agree."
                ))
                continue

            total_findings += len(findings)
            for finding in findings:
                style = getattr(self.style, SEVERITY_STYLE.get(finding["severity"], "NOTICE"))
                self.stdout.write(style(
                    f"  [{finding['severity'].upper():8}] {finding['entry_no']:<20} "
                    f"{finding['code']}"
                ))
                self.stdout.write(f"      {finding['message']}")

            self.stdout.write("")
            self.stdout.write("  by finding:")
            for code, count in sorted(
                report["summary"].items(), key=lambda kv: -kv[1]
            ):
                self.stdout.write(f"    {count:>4}  {code}")

        self.stdout.write("")
        if total_findings:
            self.stdout.write(self.style.WARNING(
                f"{total_findings} finding(s) across {len(results)} company(ies)."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"All {len(results)} company(ies) reconcile cleanly."
            ))
