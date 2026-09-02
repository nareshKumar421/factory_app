"""
Mirrors SAP's saved queries into the report catalogue.

    python manage.py sync_sap_reports
    python manage.py sync_sap_reports --company JIVO_OIL --category "GST R1"
    python manage.py sync_sap_reports --dry-run

By default every report category is mirrored; SAP's internal machinery
categories (System, dashboards, approval templates, ...) are skipped unless
named explicitly with --category.

Run it after a report is added or edited in SAP's Query Manager. Safe to repeat:
nothing this app owns (friendly names, descriptions, corrected parameter labels)
is overwritten, and a query missing from SAP is flagged rather than deleted.
"""

from django.core.management.base import BaseCommand, CommandError

from company.models import Company
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.registry import COMPANY_SAP_REGISTRY

from sap_reports.services.catalog import SapReportCatalogService


class Command(BaseCommand):
    help = "Sync SAP Query Manager reports (OUQR) into the SAP Reports catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            help="Company code to sync (default: every company with SAP configured).",
        )
        parser.add_argument(
            "--category",
            help=(
                "One SAP query category to mirror (default: every report "
                "category; SAP's internal machinery categories are skipped)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing anything.",
        )

    def handle(self, *args, **options):
        companies = self._companies(options.get("company"))
        category = options.get("category") or None
        dry_run = options["dry_run"]

        failures = 0
        for company in companies:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{company.code}"))
            try:
                summary = SapReportCatalogService(company).sync(
                    category_name=category,
                    dry_run=dry_run,
                )
            except (SAPConnectionError, SAPDataError, SAPValidationError) as error:
                failures += 1
                self.stderr.write(self.style.ERROR(f"  SAP error: {error}"))
                continue

            self._print(summary)

        if failures:
            raise CommandError(f"{failures} company/companies could not be synced.")

    def _companies(self, company_code):
        if company_code:
            company = Company.objects.filter(code=company_code).first()
            if company is None:
                raise CommandError(f"No company with code '{company_code}'.")
            if company_code not in COMPANY_SAP_REGISTRY:
                raise CommandError(f"'{company_code}' has no SAP configuration.")
            return [company]

        companies = list(Company.objects.filter(code__in=COMPANY_SAP_REGISTRY.keys()))
        if not companies:
            raise CommandError("No companies with SAP configuration were found.")
        return companies

    def _print(self, summary):
        prefix = "  would " if summary["dry_run"] else "  "
        self.stdout.write(
            f"  category: {summary['category']}  "
            f"found in SAP: {summary['found_in_sap']}"
        )
        for bucket, style in (
            ("created", self.style.SUCCESS),
            ("updated", self.style.WARNING),
            ("unchanged", self.style.HTTP_INFO),
        ):
            names = summary.get(bucket) or []
            if not names:
                continue
            self.stdout.write(style(f"{prefix}{bucket}: {len(names)}"))
            for name in names:
                self.stdout.write(f"      - {name}")

        for name in summary.get("missing_in_sap") or []:
            self.stdout.write(self.style.WARNING(f"    no longer in SAP: {name}"))

        for note in summary.get("not_runnable") or []:
            self.stdout.write(self.style.ERROR(f"    not runnable: {note}"))
