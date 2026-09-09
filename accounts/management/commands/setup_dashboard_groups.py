"""
Create one permission group per dashboard in the Dashboards module.

Usage:
    python manage.py setup_dashboard_groups             # create / update all
    python manage.py setup_dashboard_groups --dry-run   # show, write nothing
    python manage.py setup_dashboard_groups --list      # what exists right now

Lives in ``accounts`` rather than in a dashboards app because there is no such
app: the Dashboards module is a frontend grouping over a dozen backends
(stock_dashboard, non_moving_rm, sales_planning_requirement, production_execution,
pm_demand, dispatch_plans, gate_core, wms, blowing, factory_expense,
budget_approvals). ``accounts`` owns users and rights, so a command that spans all
of them belongs here.

Two rules shape the lists below.

1. VIEW RIGHTS ONLY. A dashboard group must never hand out an operational write
   right just because a panel is gated on one. The Warehouse Control board gates
   its pallet-space panel on the WMS *write* permissions (so that operators who
   hold no `view_*` are not locked out) and its linking panel on
   `can_link_dispatch_vehicle`. Granting those here would turn "let them see the
   board" into "let them move stock and link trucks". Those panels simply stay
   hidden for a pure dashboard viewer, which is the correct outcome.

2. ONE GROUP PER DISTINCT RIGHT SET, not per screen. Production, Production
   Movement and PM Demand all key on the same right, so they are one group. Three
   identically-permissioned groups would be a trap: removing somebody from one
   would not remove their access, because the other two still grant it.

Adding a group here never grants anybody anything on its own — no user is touched.
Access only changes when an admin puts someone in one of these groups.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction

PREFIX = "Dashboards — "  # em dash, matching the "Maint — X" groups

# --------------------------------------------------------------------------- #
# One entry per dashboard (or per shared right set). Values are the rights the
# group grants; the frontend route gates on ANY of them, the API on each.
# --------------------------------------------------------------------------- #
DASHBOARD_GROUPS: dict[str, list[str]] = {
    # --- single-board, single-right ---------------------------------------- #
    "Stock Benchmark": ["stock_dashboard.can_view_stock_dashboard"],
    "Non-Moving": ["non_moving_rm.can_view_non_moving_rm"],
    "Sales Plan vs Requirement": [
        "sales_planning_requirement.can_view_sales_planning_requirement",
    ],
    "Blowing": ["blowing.can_view_blowing_reports"],
    "Budget Approvals": ["budget_approvals.can_view_budget_approvals"],
    "Dispatch Pipeline": ["dispatch_plans.can_view_dispatch_pipeline"],
    "Dispatch Fulfilment": ["dispatch_plans.can_view_dispatch_plans"],
    "Dispatch Tracking": ["gate_core.can_view_dispatch_tracking"],
    "Factory Expense": ["factory_expense.can_view_factory_expense"],
    # Production, Production Movement and PM Demand share one right. PM Demand
    # carries its own right too: the frontend gates it on the production one,
    # while pm_demand's API enforces the dedicated one, so a group that grants
    # only the first would pass the route guard and then be refused by the API.
    "Production Reports": [
        "production_execution.can_view_reports",
        "pm_demand.can_view_pm_demand",
    ],
    # --- multi-right boards ------------------------------------------------- #
    "Gate": [
        "person_gatein.can_view_dashboard",
        "gate_core.can_view_gate_entry",
        "person_gatein.view_entrylog",
        "gate_core.can_view_sales_dispatch_out",
        "raw_material_gatein.view_poreceipt",
    ],
    # The pallet-space and vehicle-linking panels need write rights this group
    # deliberately withholds; they stay hidden rather than being unlocked here.
    "Warehouse Control": [
        "non_moving_rm.can_view_non_moving_rm",
        "dispatch_plans.can_view_dispatch_plans",
        "wms.view_warehouse",
        "wms.view_pallet",
        "wms.view_inventory",
        "wms.view_movement",
    ],
    # The wall board: bills, the docking register behind its vendor/company/
    # vehicle panels, and the late-on-road count.
    "Dispatch Wall": [
        "dispatch_plans.can_view_dispatch_plans",
        "dispatch_plans.can_view_dispatch_pipeline",
        "gate_core.can_view_sales_dispatch_out",
        "gate_core.can_view_dispatch_tracking",
    ],
    "Command Centre": [
        "stock_dashboard.can_view_stock_dashboard",
        "non_moving_rm.can_view_non_moving_rm",
        "sales_planning_requirement.can_view_sales_planning_requirement",
        "production_execution.can_view_reports",
        "dispatch_plans.can_view_dispatch_plans",
        "dispatch_plans.can_view_dispatch_pipeline",
    ],
}

# --------------------------------------------------------------------------- #
# Rights that DO something rather than show something. Kept out of the view
# groups and out of "All", so that "can see every board" and "can change what a
# board counts" stay separate decisions.
# --------------------------------------------------------------------------- #
ACTION_GROUPS: dict[str, list[str]] = {
    "Factory Expense Config": [
        "factory_expense.can_view_factory_expense",
        "factory_expense.can_configure_factory_expense",
    ],
    "Sales Plan Refresh": [
        "sales_planning_requirement.can_view_sales_planning_requirement",
        "sales_planning_requirement.can_refresh_sales_planning_requirement",
    ],
}


def build_groups() -> dict[str, list[str]]:
    """Every group, with "All" derived so it cannot drift from the boards."""
    groups = {f"{PREFIX}{name}": list(codes) for name, codes in DASHBOARD_GROUPS.items()}
    groups.update({f"{PREFIX}{name}": list(codes) for name, codes in ACTION_GROUPS.items()})
    every_view = sorted({code for codes in DASHBOARD_GROUPS.values() for code in codes})
    groups[f"{PREFIX}All"] = every_view
    return groups


class Command(BaseCommand):
    help = "Create a permission group per Dashboards-module board."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show the groups as they exist now."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        groups = build_groups()

        if options["list"]:
            for name in groups:
                group = Group.objects.filter(name=name).first()
                if not group:
                    self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                    continue
                held = sorted(
                    f"{p.content_type.app_label}.{p.codename}"
                    for p in group.permissions.select_related("content_type")
                )
                self.stdout.write(self.style.SUCCESS(f"{name}: {len(held)} permission(s)"))
                for code in held:
                    self.stdout.write(f"    - {code}")
            return

        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - nothing will be written\n"))

        missing_any: set[str] = set()
        created = updated = unchanged = 0

        with transaction.atomic():
            for name, codes in groups.items():
                wanted, missing = self._resolve(codes)
                missing_any |= missing

                group = Group.objects.filter(name=name).first()
                existing = set(group.permissions.all()) if group else set()
                target = set(wanted)

                if group is None:
                    verb, style = "create", self.style.SUCCESS
                    created += 1
                elif existing != target:
                    verb, style = "update", self.style.WARNING
                    updated += 1
                else:
                    unchanged += 1
                    self.stdout.write(f"  unchanged  {name} ({len(target)})")
                    continue

                # An existing group is REPLACED, not merged: these lists are the
                # definition of the group, so a right added by hand is dropped on
                # the next run. Name what goes, rather than losing it silently.
                dropped = sorted(
                    f"{p.content_type.app_label}.{p.codename}" for p in existing - target
                )
                self.stdout.write(style(f"  {verb:9}  {name} ({len(target)})"))
                for code in dropped:
                    self.stdout.write(self.style.ERROR(f"      removes {code}"))

                if not dry_run:
                    group, _ = Group.objects.get_or_create(name=name)
                    group.permissions.set(wanted)

            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write("")
        for code in sorted(missing_any):
            self.stdout.write(self.style.ERROR(f"Missing permission (skipped): {code}"))
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(groups)} group(s): {created} to create, {updated} to update, "
                f"{unchanged} unchanged."
            )
        )
        if not dry_run:
            self.stdout.write(
                "No user was touched - access changes only when someone is added to a group."
            )

    @staticmethod
    def _resolve(codes: list[str]) -> tuple[list[Permission], set[str]]:
        """Permissions for these codes, plus the codes that do not exist."""
        found, missing = [], set()
        for code in codes:
            app_label, codename = code.split(".", 1)
            perm = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if perm is None:
                missing.add(code)
            else:
                found.append(perm)
        return found, missing
