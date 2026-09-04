"""Create/refresh per-page role groups for the Maintenance and Fire modules.

Each group bundles the *existing* ``maintenance.*`` permission codenames for one
page/function, so admins can assign a single role instead of hand-picking
permissions. Idempotent — safe to run on any environment; re-running sets each
group's permission set to exactly the list below.

    python manage.py ensure_role_groups

On a live database prefer seeding one page's roles at a time, and keep whatever
an admin granted by hand:

    python manage.py ensure_role_groups --groups Electricity --add-only

``--dry-run`` reports the same work and rolls back.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# All codenames below live in the `maintenance` Django app.
STORE_SPARES = [
    "can_view_spare", "can_manage_spare",
    "add_maintenancespare", "change_maintenancespare", "view_maintenancespare",
    "add_sparerequest", "change_sparerequest", "view_sparerequest",
    "view_sparemovement", "add_sparecategory", "change_sparecategory", "view_sparecategory",
    "view_maintenancesparereceipt",
]

ROLE_GROUPS: dict[str, list[str]] = {
    # ---- Fire module (access is via the section view permissions) ----
    "Fire — Work Permit Requester": [
        "can_view_work_permit", "can_manage_work_permit", "can_issue_work_permit",
    ],
    "Fire — Work Permit Approver": [
        "can_view_work_permit", "can_approve_work_permit",
    ],
    "Fire — Safety Fine Manager": [
        "can_view_safety_fine", "can_manage_safety_fine",
    ],
    "Fire — Safety Fine Viewer": [
        "can_view_safety_fine",
    ],
    "Fire — Reports Officer": [
        "can_view_fire_report", "can_manage_fire_report", "can_review_fire_report",
    ],
    "Fire — Store Keeper": [
        "can_view_fire", "can_manage_fire",
        "add_maintenancefire", "change_maintenancefire", "view_maintenancefire",
        "add_firerequest", "change_firerequest", "view_firerequest",
        "view_firemovement", "add_firecategory", "change_firecategory", "view_firecategory",
    ],
    "Fire — Equipment Issue": [
        "can_view_fire_issue", "can_manage_fire_issue",
        "add_fireequipmentissue", "change_fireequipmentissue", "view_fireequipmentissue",
        "add_fireequipmentissueitem", "change_fireequipmentissueitem", "view_fireequipmentissueitem",
    ],
    # ---- Maintenance module (needs the module-view permission) ----
    "Maint — Material Indent Requester": [
        "can_view_maintenance_module", "can_view_material_indent", "can_manage_material_indent",
        "add_materialindent", "change_materialindent", "view_materialindent",
        "add_materialindentitem", "change_materialindentitem", "view_materialindentitem",
    ],
    # Draft-only data entry: fills the indent in and stops. Deliberately holds
    # can_draft_material_indent and NOT can_manage_material_indent, so the
    # "Send for Approval" button is closed to them.
    "Maint — Material Indent Draft Only": [
        "can_view_maintenance_module", "can_view_material_indent", "can_draft_material_indent",
        "add_materialindent", "change_materialindent", "view_materialindent",
        "add_materialindentitem", "change_materialindentitem", "view_materialindentitem",
        "add_materialindentattachment", "view_materialindentattachment",
        "delete_materialindentattachment",
    ],
    # The other half of the split: reads the drafts somebody else parked and
    # sends them for admin approval. Cannot raise or edit an indent.
    "Maint — Material Indent Sender": [
        "can_view_maintenance_module", "can_view_material_indent", "can_submit_material_indent",
        "view_materialindent", "view_materialindentitem", "view_materialindentattachment",
    ],
    "Maint — Material Indent Store Review": [
        "can_view_maintenance_module", "can_view_material_indent", "can_review_material_indent",
    ],
    "Maint — Material Indent Approver": [
        "can_view_maintenance_module", "can_view_material_indent", "can_approve_material_indent",
    ],
    "Maint — Material Indent Purchaser": [
        "can_view_maintenance_module", "can_view_material_indent", "can_purchase_material_indent",
    ],
    "Maint — Material Indent Gate-In": [
        "can_view_maintenance_module", "can_view_material_indent", "can_gatein_material_indent",
        "add_materialindentattachment", "view_materialindentattachment",
    ],
    "Maint — Store Receiver": [
        "can_view_maintenance_module", "can_view_material_indent", "can_receive_material_indent",
        *STORE_SPARES,
    ],
    "Maint — Work Order Creator": [
        "can_view_maintenance_module", "can_view_work_order", "can_create_work_order",
        "add_maintenanceworkorder", "view_maintenanceworkorder", "view_asset",
    ],
    "Maint — Work Order Manager": [
        "can_view_maintenance_module", "can_view_work_order", "can_manage_work_order",
        "can_assign_work_order", "can_start_work_order", "can_complete_work_order",
        "can_approve_work_order", "can_close_work_order",
        "add_maintenanceworkorder", "change_maintenanceworkorder", "view_maintenanceworkorder",
        "view_asset",
    ],
    "Maint — Asset Manager": [
        "can_view_maintenance_module", "view_asset", "add_asset", "change_asset",
        "view_assetcategory", "view_assetlocation", "view_assetdepartment",
    ],
    "Maint — Store/Spares Manager": [
        "can_view_maintenance_module", *STORE_SPARES,
    ],
    # Electricity register, one group per operation. The legacy "Manager" group
    # keeps can_manage_daily_electricity (the superset) so nothing it already
    # granted is lost, and also lists the granular rights for readability.
    "Maint — Daily Electricity Manager": [
        "can_view_maintenance_module", "can_view_daily_electricity", "can_manage_daily_electricity",
        "can_view_electricity_meter", "can_manage_electricity_meter",
        "can_add_daily_electricity", "can_edit_daily_electricity", "can_delete_daily_electricity",
    ],
    "Maint — Daily Electricity Viewer": [
        "can_view_maintenance_module", "can_view_daily_electricity",
        "can_view_electricity_meter",
    ],
    # Meter master keeper: adds/edits meters and their rates, does not enter readings.
    "Maint — Electricity Meter Manager": [
        "can_view_maintenance_module", "can_view_daily_electricity",
        "can_view_electricity_meter", "can_manage_electricity_meter",
    ],
    # Shop-floor data entry: records the day's readings, cannot rewrite history.
    "Maint — Electricity Reading Operator": [
        "can_view_maintenance_module", "can_view_daily_electricity",
        "can_view_electricity_meter", "can_add_daily_electricity",
    ],
    # Supervisor over the readings: enters and corrects, still no meter master.
    "Maint — Electricity Reading Supervisor": [
        "can_view_maintenance_module", "can_view_daily_electricity",
        "can_view_electricity_meter",
        "can_add_daily_electricity", "can_edit_daily_electricity", "can_delete_daily_electricity",
    ],
    "Maint — Daily Wastage Manager": [
        "can_view_maintenance_module", "can_view_daily_wastage", "can_manage_daily_wastage",
    ],
    "Maint — Daily Wastage Viewer": [
        "can_view_maintenance_module", "can_view_daily_wastage",
    ],
}


class Command(BaseCommand):
    help = "Create/refresh per-page role groups for Maintenance and Fire."

    def add_arguments(self, parser):
        parser.add_argument(
            "--groups",
            default="",
            help=(
                "Only touch groups whose name contains this text (case-insensitive), "
                "e.g. --groups Electricity. Seeds one page's roles on a live "
                "database without rewriting every other role group."
            ),
        )
        parser.add_argument(
            "--add-only",
            action="store_true",
            help=(
                "Add the listed permissions instead of replacing the group's set. "
                "Keeps permissions someone granted by hand in Django admin."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and roll back.",
        )

    def handle(self, *args, **options):
        name_filter = (options["groups"] or "").strip().lower()
        add_only = options["add_only"]
        dry_run = options["dry_run"]

        selected = {
            name: codenames
            for name, codenames in ROLE_GROUPS.items()
            if not name_filter or name_filter in name.lower()
        }
        if not selected:
            raise CommandError(
                f"No role group matches --groups {options['groups']!r}. "
                f"Known groups: {', '.join(sorted(ROLE_GROUPS))}"
            )

        with transaction.atomic():
            self._ensure(selected, add_only)
            if dry_run:
                self.stdout.write(self.style.WARNING("Dry run — rolled back."))
                transaction.set_rollback(True)

    def _ensure(self, selected, add_only):
        available = {
            p.codename: p
            for p in Permission.objects.filter(content_type__app_label="maintenance")
        }
        for group_name, codenames in selected.items():
            group, created = Group.objects.get_or_create(name=group_name)
            # Every page's department/asset dropdowns hit /maintenance/options/ and
            # /maintenance/assets/, both gated by view_asset — so all roles need it.
            codenames = [*codenames, "view_asset"]
            perms, missing = [], []
            for code in codenames:
                perm = available.get(code)
                (perms if perm else missing).append(perm or code)
            resolved = [p for p in perms if p]
            if add_only:
                group.permissions.add(*resolved)
            else:
                group.permissions.set(resolved)
            note = self.style.SUCCESS("created" if created else "updated")
            self.stdout.write(f"{note} '{group_name}' — {len(resolved)} perms" + (
                self.style.WARNING(f" | MISSING: {', '.join(missing)}") if missing else ""
            ))
        self.stdout.write(self.style.SUCCESS(f"Done. {len(selected)} role groups ensured."))
