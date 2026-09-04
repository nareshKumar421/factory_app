"""
Registers the app-authored ("local") SAP reports in the report catalogue.

    python manage.py seed_local_sap_reports
    python manage.py seed_local_sap_reports --company JIVO_BEVERAGES

Local reports run through the same screen, filters, exports and access rules
as the synced ones, but their SQL lives in ``sap_reports/local_reports.py``
because SAP has nothing to mirror -- the originals are Crystal Reports over
HANA procedures, invisible to the OUQR sync.

Safe to repeat: an unchanged report is left alone, and a changed one keeps the
friendly names, descriptions and corrected parameter labels people added in
the app. Talks only to our own database -- no SAP connection is made.
"""

from django.core.management.base import BaseCommand, CommandError

from company.models import Company
from sap_client.registry import COMPANY_SAP_REGISTRY

from sap_reports.local_reports import seed_local_reports


class Command(BaseCommand):
    help = "Seed the app-authored SAP reports (sap_reports/local_reports.py) into the catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            help="Company code to seed (default: every company with SAP configured).",
        )

    def handle(self, *args, **options):
        for company in self._companies(options.get("company")):
            summary = seed_local_reports(company)
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{company.code}"))
            for bucket, style in (
                ("created", self.style.SUCCESS),
                ("updated", self.style.WARNING),
                ("unchanged", self.style.HTTP_INFO),
            ):
                for name in summary[bucket]:
                    self.stdout.write(style(f"  {bucket}: {name}"))

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
