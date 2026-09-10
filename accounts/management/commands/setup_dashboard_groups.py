"""
Create one permission group per page in the Dashboards module.

Usage:
    python manage.py setup_dashboard_groups             # create / update all
    python manage.py setup_dashboard_groups --dry-run   # show, write nothing
    python manage.py setup_dashboard_groups --list      # what exists right now
    python manage.py setup_dashboard_groups --audit     # who holds these rights already

Lives in ``accounts`` rather than in a dashboards app because there is no such
app: the Dashboards module is a frontend grouping over a dozen backends
(stock_dashboard, non_moving_rm, sales_planning_requirement, production_execution,
packing_material, dispatch_plans, gate_core, wms, blowing, factory_expense,
budget_approvals, sap_reports, labour_gate). ``accounts`` owns users and rights,
so a command that spans all of them belongs here.

ONE GROUP PER PAGE. Every entry under the Dashboards menu gets its own group, so
a page can be granted without granting its neighbours. Note the consequence where
several pages share one right: Production, Production Movement, Packing Material,
PM Requirement and Production Control all key on
``production_execution.can_view_reports``, so those groups overlap. Taking
somebody out of "Production" does NOT close the Production board if they are
still in any of the others. Where that matters, use ``--audit`` to see every
group that grants a right before removing anybody from one.

VIEW RIGHTS ONLY. A dashboard group must never hand out an operational write
right just because a panel is gated on one. The Warehouse Control board gates its
pallet-space panel on the WMS *write* permissions (so operators holding no
``view_*`` are not locked out) and its linking panel on
``can_link_dispatch_vehicle``; SAP Reports reveals its menu to holders of
``can_manage_sap_reports``. Granting those here would turn "let them see the
board" into "let them move stock, link trucks and rewrite report SQL". Those
panels stay hidden for a pure viewer, which is the correct outcome — the two
rights that DO something have their own groups at the bottom.

Adding a group here never grants anybody anything on its own — no user is touched.
Access only changes when an admin puts someone in one of these groups.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction

PREFIX = "Dashboards — "  # em dash, matching the "Maint — X" groups

# --------------------------------------------------------------------------- #
# One entry per page under the Dashboards menu, in menu order. Values are the
# rights that page needs; the route gates on ANY of them, each API on its own.
# --------------------------------------------------------------------------- #
PAGE_GROUPS: dict[str, list[str]] = {
    # /dashboards/production-control — the lines, the floor they fill, standing
    # stock and the gate's labour tally. Mints no right of its own: it is those
    # four reports on one screen, so holding all four IS being allowed to read it.
    "Production Control": [
        "production_execution.can_view_reports",
        "stock_dashboard.can_view_stock_dashboard",
        "non_moving_rm.can_view_non_moving_rm",
        "labour_gate.view_labourgateentry",
    ],
    # /dashboards/warehouse-control — the write-gated panels are withheld, see above.
    "Warehouse Control": [
        "non_moving_rm.can_view_non_moving_rm",
        "dispatch_plans.can_view_dispatch_plans",
        "wms.view_warehouse",
        "wms.view_pallet",
        "wms.view_inventory",
        "wms.view_movement",
    ],
    # /dashboards/logistics-control — warehouse, dispatch, transport, workforce
    # and space on one wall. Two rights the board gates cards on are withheld
    # here for the reason in the header: ``dispatch_plans.can_link_dispatch_vehicle``
    # links trucks and the WMS add/change/delete rights move stock. The freight
    # card and the BH-BT card simply stay hidden for a pure viewer.
    "Logistics Control": [
        "stock_dashboard.can_view_stock_dashboard",
        "non_moving_rm.can_view_non_moving_rm",
        "dispatch_plans.can_view_dispatch_plans",
        "factory_expense.can_view_factory_expense",
        "wms.view_warehouse",
        "wms.view_pallet",
        "wms.view_inventory",
        "wms.view_movement",
    ],
    # /dashboards/overview — an aggregate of the boards below it.
    "Command Centre": [
        "stock_dashboard.can_view_stock_dashboard",
        "non_moving_rm.can_view_non_moving_rm",
        "sales_planning_requirement.can_view_sales_planning_requirement",
        "production_execution.can_view_reports",
        "dispatch_plans.can_view_dispatch_plans",
        "dispatch_plans.can_view_dispatch_pipeline",
    ],
    # /dashboards/gate
    "Gate": [
        "person_gatein.can_view_dashboard",
        "gate_core.can_view_gate_entry",
        "person_gatein.view_entrylog",
        "gate_core.can_view_sales_dispatch_out",
        "raw_material_gatein.view_poreceipt",
    ],
    # /dashboards/production
    "Production": ["production_execution.can_view_reports"],
    # /dashboards/blowing
    "Blowing": ["blowing.can_view_blowing_reports"],
    # /dashboards/stock-levels
    "Stock Benchmark": ["stock_dashboard.can_view_stock_dashboard"],
    # /dashboards/non-moving
    "Non-Moving": ["non_moving_rm.can_view_non_moving_rm"],
    # /dashboards/sales-planning-requirement
    "Sales Plan vs Requirement": [
        "sales_planning_requirement.can_view_sales_planning_requirement",
    ],
    # /dashboards/production-movement — same right as Production, see the header.
    "Production Movement": ["production_execution.can_view_reports"],
    # /dashboards/packing-material — same right again. The API also accepts the
    # dedicated ``packing_material.can_view_packing_material``, which does not
    # exist until ``manage.py sync_packing_material_permission`` has been run;
    # granting it instead of this needs that command first.
    "Packing Material": ["production_execution.can_view_reports"],
    # /dashboards/pm-requirement — the buyer's view of the same material: the
    # month's plan exploded through its BOMs against issues, stores and open
    # orders. Its own group rather than a share of Packing Material's, because
    # the two pages are read by different people; same right today, so the
    # groups overlap exactly as Production and Production Movement do.
    "PM Requirement": ["production_execution.can_view_reports"],
    # /dashboards/dispatch — the wall board: bills, the docking register behind
    # its vendor/company/vehicle panels, and the late-on-road count.
    "Dispatch Wall": [
        "dispatch_plans.can_view_dispatch_plans",
        "dispatch_plans.can_view_dispatch_pipeline",
        "gate_core.can_view_sales_dispatch_out",
        "gate_core.can_view_dispatch_tracking",
    ],
    # /dashboards/factory-expense
    "Factory Expense": ["factory_expense.can_view_factory_expense"],
    # /dashboards/budget-approvals
    "Budget Approvals": ["budget_approvals.can_view_budget_approvals"],
    # /dashboards/dispatch-pipeline
    "Dispatch Pipeline": ["dispatch_plans.can_view_dispatch_pipeline"],
    # /dashboards/dispatch-fulfilment
    "Dispatch Fulfilment": ["dispatch_plans.can_view_dispatch_plans"],
    # /dashboards/dispatch-tracking
    "Dispatch Tracking": ["gate_core.can_view_dispatch_tracking"],
    # /dashboards/sap-reports — running published reports, not administering them.
    "SAP Reports": ["sap_reports.can_view_sap_reports"],
    # /dashboards/dispatch-plans — a redirect onto the Dispatch module's plans
    # page, but it carries its own route guard, so it gets its own group.
    "Dispatch Plans": ["dispatch_plans.can_view_dispatch_plans"],
}

# --------------------------------------------------------------------------- #
# Rights that DO something rather than show something. Kept out of the page
# groups and out of "All", so that "can see every board" and "can change what a
# board counts" stay separate decisions.
# --------------------------------------------------------------------------- #
ACTION_GROUPS: dict[str, list[str]] = {
    # /dashboards/factory-expense/config
    "Factory Expense Config": [
        "factory_expense.can_view_factory_expense",
        "factory_expense.can_configure_factory_expense",
    ],
    "Sales Plan Refresh": [
        "sales_planning_requirement.can_view_sales_planning_requirement",
        "sales_planning_requirement.can_refresh_sales_planning_requirement",
    ],
    "SAP Reports Admin": [
        "sap_reports.can_view_sap_reports",
        "sap_reports.can_manage_sap_reports",
    ],
}


def build_groups() -> dict[str, list[str]]:
    """Every group, with "All" derived so it cannot drift from the pages."""
    groups = {f"{PREFIX}{name}": list(codes) for name, codes in PAGE_GROUPS.items()}
    groups.update({f"{PREFIX}{name}": list(codes) for name, codes in ACTION_GROUPS.items()})
    every_view = sorted({code for codes in PAGE_GROUPS.values() for code in codes})
    groups[f"{PREFIX}All"] = every_view
    return groups


class Command(BaseCommand):
    help = "Create a permission group per page in the Dashboards module."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show the groups as they exist now."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--audit",
            action="store_true",
            help="For each right, list every group that already grants it.",
        )

    def handle(self, *args, **options):
        groups = build_groups()

        if options["audit"]:
            return self._audit(groups)
        if options["list"]:
            return self._list(groups)

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

    # ---------------------------------------------------------------- helpers #
    def _list(self, groups):
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

    def _audit(self, groups):
        """Every group already granting each right, ours and pre-existing.

        Two things this is for: seeing which of the older hand-made groups
        (`dispatch`, `logistics`, `factory head`...) overlap these, and seeing
        which of OUR groups share a right, since removing a user from one of
        those does not close the board.
        """
        codes = sorted({code for perms in groups.values() for code in perms})
        for code in codes:
            app_label, codename = code.split(".", 1)
            perm = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if perm is None:
                self.stdout.write(self.style.ERROR(f"{code}: PERMISSION DOES NOT EXIST"))
                continue
            holders = sorted(perm.group_set.values_list("name", flat=True))
            users = perm.user_set.count()
            self.stdout.write(self.style.SUCCESS(f"{code}"))
            self.stdout.write(f"    groups: {', '.join(holders) if holders else '(none)'}")
            if users:
                self.stdout.write(
                    self.style.WARNING(f"    {users} user(s) hold it directly, outside any group")
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
