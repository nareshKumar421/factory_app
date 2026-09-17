"""
One group holding everything the Organisation module can do.

Usage::

    python manage.py setup_organisation_group
    python manage.py setup_organisation_group --list

The sidebar's "Organisation" is one module but four Django apps -- the ownership
chart (``org_chart``), Request labour (``labour_request``), Allocate labour
(``labour_gate``) and the employee screens (``employee_hierarchy``). The
per-role groups next to this file split those rights up deliberately; this one
does the opposite, for whoever owns the module end to end.

Two things are in here that are not, strictly, the module's own permissions:

* ``labour_gate.view_labourgateentry`` -- Allocate labour reads the gate's
  entries before it can split them. The gate person's own ``can_record_labour_in``
  / ``can_record_labour_out`` are NOT here: recording who walked in is the Gate
  module's job, not this one's.
* ``person_gatein.view_contractor`` -- without it the contractor dropdown on
  Allocate labour comes back empty, because the master lives under the gate.

Model-level ``add_``/``change_``/``delete_`` permissions are left out on purpose:
nothing in the app checks them, they only open rows in the Django admin.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

GROUP_NAME = "Organisation — Full Access"

ORGANISATION_PERMISSIONS = [
    # --- Ownership chart -------------------------------------------------
    "org_chart.can_view_org_chart",
    "org_chart.can_manage_org_chart",
    # --- Request labour --------------------------------------------------
    "labour_request.can_view_labour_request",
    "labour_request.can_raise_labour_request",
    "labour_request.can_decide_labour_request",
    # --- Allocate labour -------------------------------------------------
    "labour_gate.view_labourgateentry",
    "labour_gate.can_allocate_labour_department",
    "person_gatein.view_contractor",
    # --- Directory, structure, reports, audit ----------------------------
    "employee_hierarchy.can_view_employees",
    "employee_hierarchy.can_manage_employees",
    "employee_hierarchy.can_manage_org_structure",
    "employee_hierarchy.can_view_workforce_reports",
    "employee_hierarchy.can_view_employee_audit",
    # --- Permanent labour register ---------------------------------------
    "employee_hierarchy.can_view_labour_presence",
    "employee_hierarchy.can_record_labour_presence",
    # --- Compensation -----------------------------------------------------
    "employee_hierarchy.can_view_own_salary",
    "employee_hierarchy.can_view_subordinate_salary",
    "employee_hierarchy.can_view_department_salary",
    "employee_hierarchy.can_view_all_salaries",
    "employee_hierarchy.can_view_salary_history",
    "employee_hierarchy.can_create_salary",
    "employee_hierarchy.can_update_salary",
    "employee_hierarchy.can_approve_salary_revision",
]


class Command(BaseCommand):
    help = "Create / update the all-of-Organisation group and its permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what the group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            self.stdout.write(self.style.MIGRATE_HEADING(GROUP_NAME))
            for code in ORGANISATION_PERMISSIONS:
                self.stdout.write(f"  {code}")
            return

        permissions, missing = [], []
        for code in ORGANISATION_PERMISSIONS:
            app_label, codename = code.split(".", 1)
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            (permissions if permission else missing).append(permission or code)

        if missing:
            raise SystemExit(f"Missing permissions {sorted(missing)} - run migrate first.")

        group, created = Group.objects.get_or_create(name=GROUP_NAME)
        group.permissions.set(permissions)
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Updated'} {GROUP_NAME} "
                f"({len(permissions)} permission(s))."
            )
        )
