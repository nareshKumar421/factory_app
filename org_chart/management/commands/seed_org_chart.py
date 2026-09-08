"""
Load the ownership chart a plant runs on today.

There is one chart per company, and :mod:`org_chart.constants` holds the Oil
plant's. Usage::

    python manage.py seed_org_chart                     # Jivo Oil, only if empty
    python manage.py seed_org_chart --company JIVO_MART # another company
    python manage.py seed_org_chart --replace           # wipe and reload

The chart is edited on the page once it exists, so seeding refuses to touch a
chart that already has rows unless it is told to replace it — an accidental
re-run must never quietly undo an HR correction. Only the named company's chart
is ever touched; the other two are left exactly as they are.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company
from org_chart.constants import DEFAULT_CHART, DEFAULT_PLANT_HEAD, DEFAULT_PLANT_NAME
from org_chart.models import OrgChartSettings, OrgDepartment, OrgFunction

#: Whose chart ``constants.DEFAULT_CHART`` actually describes.
DEFAULT_COMPANY_CODE = "JIVO_OIL"


class Command(BaseCommand):
    help = "Seed one company's department ownership chart with the current defaults."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            default=DEFAULT_COMPANY_CODE,
            help=f"Company code to seed (default {DEFAULT_COMPANY_CODE}).",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Delete the existing chart first (destroys any edits made on the page).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        code = options["company"]
        try:
            company = Company.objects.get(code=code)
        except Company.DoesNotExist:
            known = ", ".join(Company.objects.values_list("code", flat=True)) or "none"
            raise CommandError(f"No company with code {code!r}. Known codes: {known}.")

        chart = OrgDepartment.objects.filter(company=company)
        existing = chart.count()
        if existing and not options["replace"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{company.name}'s chart already has {existing} department(s) — "
                    "nothing done. Pass --replace to overwrite it."
                )
            )
            return

        if existing:
            chart.delete()
            self.stdout.write(
                f"Removed {existing} existing department(s) from {company.name}."
            )

        settings = OrgChartSettings.load(company)
        settings.plant_name = DEFAULT_PLANT_NAME
        settings.plant_head = DEFAULT_PLANT_HEAD
        settings.save()

        functions = 0
        for order, (department_name, head, rows) in enumerate(DEFAULT_CHART):
            department = OrgDepartment.objects.create(
                company=company, name=department_name, head=head, sort_order=order
            )
            for row_order, (name, subtitle, owners, level_1, level_2) in enumerate(rows):
                OrgFunction.objects.create(
                    department=department,
                    name=name,
                    subtitle=subtitle,
                    owners=list(owners),
                    level_1=list(level_1),
                    level_2=list(level_2),
                    sort_order=row_order,
                )
                functions += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {company.name}: {DEFAULT_PLANT_NAME} "
                f"(head: {DEFAULT_PLANT_HEAD or '—'}) with "
                f"{len(DEFAULT_CHART)} department(s) and {functions} function(s)."
            )
        )
