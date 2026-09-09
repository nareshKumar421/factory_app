"""
Bring approved-but-future salary revisions into force once their date arrives.

Usage (daily, from cron or the scheduler)::

    python manage.py apply_due_salary_revisions
    python manage.py apply_due_salary_revisions --company JIVO_OIL

An increment approved in March for the 1st of April is ``SCHEDULED`` until the
1st. Something has to notice the day it starts, or the cached figure the
directory filters and the reports read is last year's number. That is this
command.

Idempotent, and cheap when there is nothing to do: it only touches employees
who actually have a revision due.
"""

from django.core.management.base import BaseCommand

from company.models import Company
from employee_hierarchy import services


class Command(BaseCommand):
    help = "Apply salary revisions whose effective date has arrived."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company code. Omit for every company.")

    def handle(self, *args, **options):
        company = None
        if options["company"]:
            company = Company.objects.filter(code=options["company"]).first()
            if company is None:
                raise SystemExit(f"No company with code {options['company']}.")

        applied = services.apply_due_revisions(company)
        if applied:
            self.stdout.write(
                self.style.SUCCESS(f"Brought revisions into force for {applied} employee(s).")
            )
        else:
            self.stdout.write("Nothing was due.")
