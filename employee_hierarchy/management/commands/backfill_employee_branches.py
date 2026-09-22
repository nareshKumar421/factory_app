"""
Put the default branch on employees who predate the branch field.

The branch master arrived after the directory did, so everybody already in it
has ``branch = NULL`` and reads "Unassigned" on their profile. New joiners get
the default from :func:`employee_hierarchy.services.create_employee`; this is
the one-off that catches up the people hired before that existed.

Deliberately a command and **not** part of the migration that added the field.
A migration runs itself on every deploy, against every environment, with nobody
watching -- and writing a branch onto two hundred people is an assertion about
where they work, not a schema change. This way somebody names the branch, sees
the count first, and can point a subset at a different branch when the factory
turns out not to be one site after all.

Dry run by default, like the other repairs in this module::

    # look first -- writes nothing
    python manage.py backfill_employee_branches

    # do it
    python manage.py backfill_employee_branches --commit

    # a branch other than the default, or only one company
    python manage.py backfill_employee_branches --branch MART --company JIVO_OIL --commit

Only employees with **no** branch are touched, so it is safe to re-run and it
never overwrites a choice somebody has already made.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company

from ...constants import RecordStatus
from ...models import Branch, Employee
from ...services import default_branch


class Command(BaseCommand):
    help = "Give employees with no branch the company's default branch."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually write. Without it the command only reports.",
        )
        parser.add_argument(
            "--branch",
            help="Branch code to use instead of the company's default.",
        )
        parser.add_argument(
            "--company",
            help="Limit to one company code, e.g. JIVO_OIL. Default: every company.",
        )

    def handle(self, *args, **options):
        companies = Company.objects.all()
        if options["company"]:
            companies = companies.filter(code=options["company"])
            if not companies.exists():
                raise CommandError(f"No company with code {options['company']!r}.")

        total = 0
        for company in companies:
            unbranched = Employee.objects.filter(company=company, branch__isnull=True)
            count = unbranched.count()
            if not count:
                self.stdout.write(f"{company.code}: nothing to do.")
                continue

            if options["branch"]:
                branch = Branch.objects.filter(
                    company=company, code=options["branch"], status=RecordStatus.ACTIVE
                ).first()
                if branch is None:
                    raise CommandError(
                        f"{company.code} has no active branch with code "
                        f"{options['branch']!r}."
                    )
            else:
                branch = default_branch(company)
                if branch is None:
                    # Refused rather than invented: which branch these people
                    # belong to is exactly the thing this command must not guess.
                    self.stderr.write(
                        f"{company.code}: no default branch set, so {count} "
                        "employee(s) were left alone. Set one first."
                    )
                    continue

            self.stdout.write(f"{company.code}: {count} employee(s) -> {branch.name}")
            if options["commit"]:
                with transaction.atomic():
                    unbranched.update(branch=branch)
            total += count

        if not options["commit"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {total} employee(s) would be updated. "
                    "Re-run with --commit to write."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"{total} employee(s) updated."))
