"""
Management command to create Production Execution and Production QC groups.

Usage:
    python manage.py setup_production_groups          # create/update all groups
    python manage.py setup_production_groups --list    # list groups and their permissions
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

# ---------------------------------------------------------------------------
# Production Execution groups
# ---------------------------------------------------------------------------
PRODUCTION_GROUPS = {
    "Production Operator": [
        # Production Runs
        "production_execution.can_view_production_run",
        "production_execution.can_create_production_run",
        "production_execution.can_edit_production_run",
        # Hourly Logs
        "production_execution.can_view_production_log",
        "production_execution.can_edit_production_log",
        # Breakdowns
        "production_execution.can_view_breakdown",
        "production_execution.can_create_breakdown",
        "production_execution.can_edit_breakdown",
        # Material Usage
        "production_execution.can_view_material_usage",
        "production_execution.can_create_material_usage",
        "production_execution.can_edit_material_usage",
        # Machine Runtime
        "production_execution.can_view_machine_runtime",
        "production_execution.can_create_machine_runtime",
        # Line Clearance
        "production_execution.can_view_line_clearance",
        "production_execution.can_create_line_clearance",
        # Machine Checklists
        "production_execution.can_view_machine_checklist",
        "production_execution.can_create_machine_checklist",
        # Waste
        "production_execution.can_view_waste_log",
        "production_execution.can_create_waste_log",
    ],
    "Shift Incharge": [
        # Everything Operator has  ──────────────────────────
        "production_execution.can_view_production_run",
        "production_execution.can_create_production_run",
        "production_execution.can_edit_production_run",
        "production_execution.can_complete_production_run",
        "production_execution.can_view_production_log",
        "production_execution.can_edit_production_log",
        "production_execution.can_view_breakdown",
        "production_execution.can_create_breakdown",
        "production_execution.can_edit_breakdown",
        "production_execution.can_view_material_usage",
        "production_execution.can_create_material_usage",
        "production_execution.can_edit_material_usage",
        "production_execution.can_view_machine_runtime",
        "production_execution.can_create_machine_runtime",
        "production_execution.can_view_line_clearance",
        "production_execution.can_create_line_clearance",
        "production_execution.can_view_machine_checklist",
        "production_execution.can_create_machine_checklist",
        "production_execution.can_view_waste_log",
        "production_execution.can_create_waste_log",
        # Extra  ────────────────────────────────────────────
        "production_execution.can_view_manpower",
        "production_execution.can_create_manpower",
        "production_execution.can_view_reports",
    ],
    "Production Engineer": [
        # Everything Shift Incharge has  ────────────────────
        "production_execution.can_view_production_run",
        "production_execution.can_create_production_run",
        "production_execution.can_edit_production_run",
        "production_execution.can_complete_production_run",
        "production_execution.can_view_production_log",
        "production_execution.can_edit_production_log",
        "production_execution.can_view_breakdown",
        "production_execution.can_create_breakdown",
        "production_execution.can_edit_breakdown",
        "production_execution.can_view_material_usage",
        "production_execution.can_create_material_usage",
        "production_execution.can_edit_material_usage",
        "production_execution.can_view_machine_runtime",
        "production_execution.can_create_machine_runtime",
        "production_execution.can_view_line_clearance",
        "production_execution.can_create_line_clearance",
        "production_execution.can_view_machine_checklist",
        "production_execution.can_create_machine_checklist",
        "production_execution.can_view_waste_log",
        "production_execution.can_create_waste_log",
        "production_execution.can_view_manpower",
        "production_execution.can_create_manpower",
        "production_execution.can_view_reports",
        # Extra  ────────────────────────────────────────────
        "production_execution.can_manage_machines",
        "production_execution.can_manage_checklist_templates",
        "production_execution.can_approve_waste_engineer",
    ],
    "Production HOD": [
        # Everything Engineer has  ──────────────────────────
        "production_execution.can_view_production_run",
        "production_execution.can_create_production_run",
        "production_execution.can_edit_production_run",
        "production_execution.can_complete_production_run",
        "production_execution.can_view_production_log",
        "production_execution.can_edit_production_log",
        "production_execution.can_view_breakdown",
        "production_execution.can_create_breakdown",
        "production_execution.can_edit_breakdown",
        "production_execution.can_view_material_usage",
        "production_execution.can_create_material_usage",
        "production_execution.can_edit_material_usage",
        "production_execution.can_view_machine_runtime",
        "production_execution.can_create_machine_runtime",
        "production_execution.can_view_line_clearance",
        "production_execution.can_create_line_clearance",
        "production_execution.can_view_machine_checklist",
        "production_execution.can_create_machine_checklist",
        "production_execution.can_view_waste_log",
        "production_execution.can_create_waste_log",
        "production_execution.can_view_manpower",
        "production_execution.can_create_manpower",
        "production_execution.can_view_reports",
        "production_execution.can_manage_machines",
        "production_execution.can_manage_checklist_templates",
        "production_execution.can_approve_waste_engineer",
        # Extra  ────────────────────────────────────────────
        "production_execution.can_manage_production_lines",
        "production_execution.can_approve_waste_hod",
    ],
    "QA Officer": [
        "production_execution.can_view_production_run",
        "production_execution.can_view_line_clearance",
        "production_execution.can_approve_line_clearance_qa",
    ],
    "Store Incharge": [
        "production_execution.can_view_production_run",
        "production_execution.can_view_material_usage",
        "production_execution.can_view_waste_log",
        "production_execution.can_approve_waste_store",
    ],
    "Area Manager": [
        "production_execution.can_view_production_run",
        "production_execution.can_view_waste_log",
        "production_execution.can_approve_waste_am",
    ],
}

# ---------------------------------------------------------------------------
# Production QC groups
# ---------------------------------------------------------------------------
QC_GROUPS = {
    "QC Store": [
        # Arrival slips
        "quality_control.add_materialarrivalslip",
        "quality_control.view_materialarrivalslip",
        "quality_control.change_materialarrivalslip",
        "quality_control.can_submit_arrival_slip",
        # Read-only inspection access
        "quality_control.view_rawmaterialinspection",
    ],
    "QC Chemist": [
        # Arrival slip – view only
        "quality_control.view_materialarrivalslip",
        # Inspections
        "quality_control.add_rawmaterialinspection",
        "quality_control.view_rawmaterialinspection",
        "quality_control.change_rawmaterialinspection",
        "quality_control.can_submit_inspection",
        "quality_control.can_approve_as_chemist",
        # Production QC
        "quality_control.can_view_production_qc",
        "quality_control.can_create_production_qc",
        "quality_control.can_submit_production_qc",
    ],
    "QC Manager": [
        # Arrival slips – full
        "quality_control.add_materialarrivalslip",
        "quality_control.view_materialarrivalslip",
        "quality_control.change_materialarrivalslip",
        "quality_control.delete_materialarrivalslip",
        "quality_control.can_submit_arrival_slip",
        "quality_control.can_send_back_arrival_slip",
        # Inspections – full
        "quality_control.add_rawmaterialinspection",
        "quality_control.view_rawmaterialinspection",
        "quality_control.change_rawmaterialinspection",
        "quality_control.delete_rawmaterialinspection",
        "quality_control.can_submit_inspection",
        "quality_control.can_approve_as_chemist",
        "quality_control.can_approve_as_qam",
        "quality_control.can_reject_inspection",
        # Master data
        "quality_control.can_manage_material_types",
        "quality_control.can_manage_qc_parameters",
        # Production QC
        "quality_control.can_view_production_qc",
        "quality_control.can_create_production_qc",
        "quality_control.can_submit_production_qc",
    ],
}

ALL_GROUPS = {**PRODUCTION_GROUPS, **QC_GROUPS}


class Command(BaseCommand):
    help = "Create or update Production Execution and Production QC permission groups"

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="List all groups and their permissions without making changes",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self._list_groups()
            return

        self._create_groups()

    # ------------------------------------------------------------------
    def _list_groups(self):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write("  Groups & Permissions (read-only)")
        self.stdout.write("=" * 60)

        for group_name, perms in ALL_GROUPS.items():
            self.stdout.write(f"\n  {group_name}  ({len(perms)} permissions)")
            self.stdout.write("  " + "-" * 40)
            for p in perms:
                self.stdout.write(f"    {p}")

        self.stdout.write(f"\n  Total: {len(ALL_GROUPS)} groups\n")

    # ------------------------------------------------------------------
    def _create_groups(self):
        created_count = 0
        updated_count = 0
        warnings = []

        for group_name, perm_strings in ALL_GROUPS.items():
            group, created = Group.objects.get_or_create(name=group_name)

            perm_objects = []
            for perm_str in perm_strings:
                app_label, codename = perm_str.split(".")
                try:
                    perm = Permission.objects.get(
                        content_type__app_label=app_label,
                        codename=codename,
                    )
                    perm_objects.append(perm)
                except Permission.DoesNotExist:
                    warnings.append(f"  {group_name}: permission not found — {perm_str}")

            group.permissions.set(perm_objects)

            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  Created group: {group_name} ({len(perm_objects)} permissions)"
                ))
            else:
                updated_count += 1
                self.stdout.write(self.style.WARNING(
                    f"  Updated group: {group_name} ({len(perm_objects)} permissions)"
                ))

        # Summary
        self.stdout.write("")
        if warnings:
            self.stdout.write(self.style.ERROR("Warnings:"))
            for w in warnings:
                self.stdout.write(self.style.ERROR(w))
            self.stdout.write("")

        self.stdout.write(self.style.SUCCESS(
            f"Done. Created: {created_count}, Updated: {updated_count}, "
            f"Total: {len(ALL_GROUPS)} groups."
        ))
