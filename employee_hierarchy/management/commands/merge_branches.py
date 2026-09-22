"""
Fold one branch into another and remove the one that is left empty.

Branches arrive from the SAP column, and SAP draws lines the business does not
always keep -- Construction is a handful of people the factory treats as
Common. Rather than edit the sheet, the two are merged here: everybody moves,
and the source branch goes.

The move is the whole operation. A branch with nobody on it is still offered in
every picker, so leaving the husk behind would re-fill it the first time
somebody chose it by mistake -- which is why ``--delete`` is the normal ending
and the source is retired rather than kept when it is not passed.

**Re-running the workbook import undoes this.**
``import_employee_branches`` reads the SAP column, which still says
``Construction``, so it would recreate the branch and move the seven back. The
merge is a decision about *our* labels; the segment on each record still says
what SAP thinks, and the two are allowed to disagree. Merge again after an
import, or fix the sheet.

Dry run by default::

    python manage.py merge_branches --from CONSTRUCTION --into COMMON
    python manage.py merge_branches --from CONSTRUCTION --into COMMON --delete --commit
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company

from ...constants import RecordStatus
from ...models import Branch, Employee


class Command(BaseCommand):
    help = "Move every employee from one branch to another, then drop the source."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="source", required=True, help="Branch code to empty.")
        parser.add_argument("--into", dest="target", required=True, help="Branch code to fill.")
        parser.add_argument("--company", default="JIVO_OIL")
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete the source once empty. Without it the source is retired instead.",
        )
        parser.add_argument("--commit", action="store_true", help="Actually write.")

    def _branch(self, company, code, role):
        branch = Branch.objects.filter(company=company, code__iexact=code).first()
        if branch is None:
            branch = Branch.objects.filter(company=company, name__iexact=code).first()
        if branch is None:
            known = ", ".join(
                Branch.objects.filter(company=company).values_list("code", flat=True)
            )
            raise CommandError(f"No {role} branch {code!r} in {company.code}. Have: {known}.")
        return branch

    def handle(self, *args, **options):
        company = Company.objects.filter(code=options["company"]).first()
        if company is None:
            raise CommandError(f"No company with code {options['company']!r}.")

        source = self._branch(company, options["source"], "source")
        target = self._branch(company, options["target"], "target")
        if source.pk == target.pk:
            raise CommandError("--from and --into name the same branch.")
        if source.is_default:
            # The default is what new joiners get; emptying and deleting it
            # would leave the hire form with nothing to pre-select.
            raise CommandError(
                f"{source.name} is the default branch. Make another branch the default first."
            )

        moving = Employee.objects.filter(company=company, branch=source)
        count = moving.count()
        self.stdout.write(f"Company : {company.code}")
        self.stdout.write(f"Merge   : {source.name} ({source.code}) -> {target.name} ({target.code})")
        self.stdout.write(f"Moving  : {count} employee(s)")
        for employee in moving.order_by("full_name")[:15]:
            self.stdout.write(f"   {employee.employee_code:<12} {employee.full_name}")
        if count > 15:
            self.stdout.write(f"   … +{count - 15} more")
        self.stdout.write(
            f"Then    : {source.name} will be {'DELETED' if options['delete'] else 'retired'}"
        )
        self.stdout.write(
            f"After   : {target.name} holds "
            f"{Employee.objects.filter(company=company, branch=target).count() + count}"
        )

        if not options["commit"]:
            self.stdout.write(self.style.WARNING("\nDry run. Re-run with --commit to write."))
            return

        with transaction.atomic():
            moved = moving.update(branch=target)
            if options["delete"]:
                source.delete()
                ending = "deleted"
            else:
                source.status = RecordStatus.INACTIVE
                source.save(update_fields=["status", "updated_at"])
                ending = "retired"

        self.stdout.write(
            self.style.SUCCESS(f"\n{moved} employee(s) moved; {source.name} {ending}.")
        )
