"""
Load the ownership chart the factory runs on today.

Usage::

    python manage.py seed_org_chart            # only when the chart is empty
    python manage.py seed_org_chart --replace  # wipe and reload the defaults

The chart is edited on the page once it exists, so seeding refuses to touch a
chart that already has rows unless it is told to replace it — an accidental
re-run must never quietly undo an HR correction.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from org_chart.constants import DEFAULT_CHART
from org_chart.models import OrgDepartment, OrgFunction


class Command(BaseCommand):
    help = "Seed the department ownership chart with the current defaults."

    def add_arguments(self, parser):
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Delete the existing chart first (destroys any edits made on the page).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        existing = OrgDepartment.objects.count()
        if existing and not options["replace"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Chart already has {existing} department(s) — nothing done. "
                    "Pass --replace to overwrite it."
                )
            )
            return

        if existing:
            OrgDepartment.objects.all().delete()
            self.stdout.write(f"Removed {existing} existing department(s).")

        functions = 0
        for order, (department_name, rows) in enumerate(DEFAULT_CHART):
            department = OrgDepartment.objects.create(
                name=department_name, sort_order=order
            )
            for row_order, (name, owners, level_1, level_2) in enumerate(rows):
                OrgFunction.objects.create(
                    department=department,
                    name=name,
                    owners=list(owners),
                    level_1=list(level_1),
                    level_2=list(level_2),
                    sort_order=row_order,
                )
                functions += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(DEFAULT_CHART)} department(s) and {functions} function(s)."
            )
        )
