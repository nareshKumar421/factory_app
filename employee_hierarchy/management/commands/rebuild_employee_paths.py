"""
Recompute every employee's hierarchy path and level from their manager link.

Usage::

    python manage.py rebuild_employee_paths
    python manage.py rebuild_employee_paths --company JIVO_OIL

A repair tool. The materialised path is a cache of the ``reporting_manager``
links, maintained by :mod:`employee_hierarchy.services`; anything cached
deserves a way to be rebuilt. Run it after a bulk import, or after somebody has
edited a manager in the Django admin, which is the one door that bypasses the
services.

Safe to run at any time: it derives everything from the manager links and
writes the answer, so running it twice changes nothing the second time.
"""

from django.core.management.base import BaseCommand

from company.models import Company
from employee_hierarchy import hierarchy


class Command(BaseCommand):
    help = "Rebuild employee hierarchy paths and levels from their manager links."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company code. Omit to rebuild every company.")

    def handle(self, *args, **options):
        company = None
        if options["company"]:
            company = Company.objects.filter(code=options["company"]).first()
            if company is None:
                raise SystemExit(f"No company with code {options['company']}.")

        rebuilt = hierarchy.rebuild_paths(company)
        where = company.code if company else "all companies"
        self.stdout.write(
            self.style.SUCCESS(f"Rebuilt {rebuilt} employee path(s) for {where}.")
        )
