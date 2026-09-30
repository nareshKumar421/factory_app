"""
Read the price sheet and save it as today's oil prices and Jivo rates.

Usage:
    python manage.py sync_oil_prices                  # JIVO_OIL, today
    python manage.py sync_oil_prices --company JIVO_OIL --dry-run

Meant for cron, nightly (EXIM ran its own at 23:00). A second run the same day
replaces that day's figures; earlier days are never touched. Prints what it
saved and exits non-zero if the sheet could not be read, so a cron log shows a
missed night.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from exim import price_sheet, services_price
from exim.licence_import import LicenceImportProblem, resolve_company


class Command(BaseCommand):
    help = "Save today's oil prices and Jivo rates from the price sheet."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_OIL", help="The company they are kept under (default JIVO_OIL).")
        parser.add_argument("--dry-run", action="store_true", help="Read the sheet and say what it holds; save nothing.")

    def handle(self, *args, **options):
        try:
            company = resolve_company(options["company"])
        except LicenceImportProblem as exc:
            raise CommandError(str(exc)) from exc
        try:
            sheet = price_sheet.read_sheet()
        except price_sheet.PriceSheetError as exc:
            raise CommandError(f"{timezone.localtime():%Y-%m-%d %H:%M} {exc}") from exc

        day = timezone.localdate()
        with transaction.atomic():
            prices = services_price.save_prices(company, sheet["prices"], day=day)
            rates = services_price.save_rates(company, sheet["rates"], day=day)
            if options["dry_run"]:
                transaction.set_rollback(True)
        self.stdout.write(
            f"{timezone.localtime():%Y-%m-%d %H:%M} {company.code} {day}: "
            f"{len(sheet['prices'])} commodity prices ({prices['created']} new, {prices['updated']} re-read), "
            f"{len(sheet['rates'])} pack rates ({rates['created']} new, {rates['updated']} re-read)"
            + (" - DRY RUN, nothing saved" if options["dry_run"] else "")
        )
