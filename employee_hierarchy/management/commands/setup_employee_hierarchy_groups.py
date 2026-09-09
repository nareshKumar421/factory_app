"""
Create the module's groups and give each one its permissions.

Usage::

    python manage.py setup_employee_hierarchy_groups
    python manage.py setup_employee_hierarchy_groups --list

Six groups, because the brief names six audiences, and the interesting part is
what each one does *not* get:

* **Employee (Self Service)** -- the directory and their own salary. Nothing
  about anybody else's pay, including their own manager's.
* **Manager** -- their team's salaries and the right to propose a revision, but
  not to approve one, and not their peers' figures.
* **Department Head** -- their department (and its sub-departments), plus the
  history behind each figure and the headcount reports.
* **HR** -- everybody's salary and the right to enter one, but *not* to approve
  it. Preparing a revision and signing it off are deliberately different jobs.
* **Finance** -- everybody's salary and the approval right, but no ability to
  edit employees or restructure the org.
* **HR Administrator** -- everything, for whoever owns the module.

Groups are a starting point, not a policy: a company that wants its department
heads to approve revisions grants them that permission. What the split protects
is the default -- nobody gets salary access by accident, and nobody both writes
and approves the same revision unless somebody deliberately arranged it.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

APP = "employee_hierarchy"

DIRECTORY = ["can_view_employees"]
OWN_SALARY = ["can_view_own_salary"]

EMPLOYEE_GROUPS = {
    "Employee (Self Service)": [*DIRECTORY, *OWN_SALARY],
    "Manager": [
        *DIRECTORY,
        *OWN_SALARY,
        "can_view_subordinate_salary",
        "can_update_salary",
    ],
    "Department Head": [
        *DIRECTORY,
        *OWN_SALARY,
        "can_view_subordinate_salary",
        "can_view_department_salary",
        "can_view_salary_history",
        "can_update_salary",
        "can_view_workforce_reports",
    ],
    "HR": [
        *DIRECTORY,
        *OWN_SALARY,
        "can_manage_employees",
        "can_manage_org_structure",
        "can_view_all_salaries",
        "can_view_salary_history",
        "can_create_salary",
        "can_update_salary",
        "can_view_workforce_reports",
        "can_view_employee_audit",
    ],
    "Finance (Payroll)": [
        *DIRECTORY,
        *OWN_SALARY,
        "can_view_all_salaries",
        "can_view_salary_history",
        "can_approve_salary_revision",
        "can_view_workforce_reports",
    ],
    "HR Administrator": [
        *DIRECTORY,
        *OWN_SALARY,
        "can_manage_employees",
        "can_manage_org_structure",
        "can_view_subordinate_salary",
        "can_view_department_salary",
        "can_view_all_salaries",
        "can_view_salary_history",
        "can_create_salary",
        "can_update_salary",
        "can_approve_salary_revision",
        "can_view_workforce_reports",
        "can_view_employee_audit",
    ],
}


class Command(BaseCommand):
    help = "Create / update the employee hierarchy groups and their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what each group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for group_name, codenames in EMPLOYEE_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(group_name))
                for codename in codenames:
                    self.stdout.write(f"  {APP}.{codename}")
            return

        for group_name, codenames in EMPLOYEE_GROUPS.items():
            group, created = Group.objects.get_or_create(name=group_name)
            permissions = list(
                Permission.objects.filter(
                    content_type__app_label=APP, codename__in=codenames
                )
            )
            missing = set(codenames) - {permission.codename for permission in permissions}
            if missing:
                raise SystemExit(
                    f"Missing permissions {sorted(missing)} — run migrate first."
                )
            group.permissions.set(permissions)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'Created' if created else 'Updated'} {group_name} "
                    f"({len(permissions)} permission(s))."
                )
            )
