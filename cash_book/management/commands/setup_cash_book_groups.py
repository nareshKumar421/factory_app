"""
Create the cash book's role groups and assign their permissions.

Usage::

    python manage.py setup_cash_book_groups           # create / update
    python manage.py setup_cash_book_groups --list    # show what each holds

Five roles. Four are the cash box's own; the fifth is HR's, and it holds
nothing but the salary advance screen:

* **Cash Book Viewer**    -- anybody who has to read the book: accounts, audit,
  a plant head checking what a department is spending.
* **Cash Book Custodian** -- the person holding the cash. Records receipts and
  payments, corrects and cancels, and sends bunches of vouchers for approval.
* **Cash Book Approver**  -- decides on the payments somebody else recorded.
  Deliberately a separate group from the custodian: one person recording cash
  and agreeing to their own spending is the control this module has, and
  granting both should be something somebody chose to do.
* **Cash Book Administrator** -- keeps the book *and* configures the branch
  list behind it (Settings -> Cash Book Branches).
* **Salary Advance HR** -- decides whether an advance accounts handed over
  comes back off a wage, and ticks it off once it has. The one group here that
  is not given the view right: agreeing to dock somebody's pay is a payroll
  decision, and it does not need the petty cash register to make it.

Each group lists the view right explicitly as well as its own. It is implied at
the endpoints, but a group that lists both reads correctly off the admin screen.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEW = "cash_book.can_view_cash_book"
MANAGE = "cash_book.can_manage_cash_book"
APPROVE = "cash_book.can_approve_cash_entries"
BRANCHES = "cash_book.can_manage_cash_branches"
SALARY_ADVANCES = "cash_book.can_approve_salary_advances"

CASH_BOOK_GROUPS = {
    "Cash Book Viewer": [VIEW],
    "Cash Book Custodian": [VIEW, MANAGE],
    "Cash Book Approver": [VIEW, APPROVE],
    # Configures the branch list every entry is filed under. Separate
    # because renaming or retiring a branch reaches back through the
    # whole register -- a settings decision, not a day's cash handling.
    "Cash Book Administrator": [VIEW, MANAGE, BRANCHES],
    # HR, and only the one screen. No view right: they decide whether an
    # advance comes off a wage, which is a payroll decision, and nothing about
    # it needs them reading the factory's petty cash register. The Advance
    # Salary endpoints admit this right by name.
    "Salary Advance HR": [SALARY_ADVANCES],
}


class Command(BaseCommand):
    help = "Create the cash book's role groups and the Salary Advance HR group."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="List the groups and their permissions without changing anything.",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self._list()
            return

        for name, codes in CASH_BOOK_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            permissions, missing = self._resolve(codes)

            if missing:
                self.stdout.write(
                    self.style.ERROR(
                        f"{name}: permission(s) not found: {', '.join(missing)}. "
                        f"Run `migrate` first -- the permissions are created with "
                        f"the cash book tables."
                    )
                )
                continue

            group.permissions.set(permissions)
            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} {name} ({len(permissions)} permission(s))")
            )

    def _resolve(self, codes):
        permissions, missing = [], []
        for code in codes:
            app_label, codename = code.split(".", 1)
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if permission is None:
                missing.append(code)
            else:
                permissions.append(permission)
        return permissions, missing

    def _list(self):
        for name in CASH_BOOK_GROUPS:
            group = Group.objects.filter(name=name).first()
            if group is None:
                self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                continue
            self.stdout.write(self.style.SUCCESS(f"{name}:"))
            for codename in group.permissions.values_list("codename", flat=True):
                self.stdout.write(f"    - {codename}")
