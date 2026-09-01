"""Who would be locked out by per-user warehouse scoping, and out of what.

Run this BEFORE deploying the scoping, and after any change to the warehouse
groups. An unassigned user is blocked from raising or accepting anything, so a
missing row here is a person who cannot do their job — and the failure shows up
as a 403 mid-shift, not as a warning at deploy time.

    python manage.py report_warehouse_scope_gaps
    python manage.py report_warehouse_scope_gaps --company JIVO_OIL
    python manage.py report_warehouse_scope_gaps --assign-from-history   (dry run)
    python manage.py report_warehouse_scope_gaps --assign-from-history --commit

`--assign-from-history` proposes an assignment for each unconfigured user from
what they have actually done: the source warehouses of the transfer requests
they raised and the BSTs they created. It is a starting point for the admin to
confirm, not a substitute for deciding who runs which floor — a user who once
raised a request out of another site would be proposed for it too, which is why
it prints and asks rather than writing by default.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Q

from company.models import Company
from warehouse.models_bst import BSTTransfer
from warehouse.models_manager import UserWarehouse
from warehouse.models_transfer import WarehouseTransferRequest
from warehouse.services import warehouse_scope


class Command(BaseCommand):
    help = "List users who can move stock but manage no warehouse (they would be blocked)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            help="Company code to check. Omit to check every company.",
        )
        parser.add_argument(
            "--assign-from-history",
            action="store_true",
            help="Propose assignments from the warehouses each user has actually worked.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="With --assign-from-history, actually write the proposed rows.",
        )

    def handle(self, *args, **options):
        codes = (
            [options["company"]]
            if options.get("company")
            else list(Company.objects.values_list("code", flat=True))
        )

        total_gaps = 0
        for code in codes:
            if not Company.objects.filter(code=code).exists():
                self.stderr.write(self.style.ERROR(f"No such company: {code}"))
                continue

            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {code} ==="))

            assigned = (
                UserWarehouse.objects.filter(company__code=code, is_active=True)
                .select_related("user")
                .order_by("user__full_name", "warehouse_code")
            )
            by_user = defaultdict(list)
            for row in assigned:
                by_user[row.user].append(row.warehouse_code)

            if by_user:
                self.stdout.write(f"\nConfigured managers ({len(by_user)}):")
                for user, whs in sorted(
                    by_user.items(), key=lambda kv: kv[0].full_name or ""
                ):
                    self.stdout.write(
                        f"  {user.full_name or user.email:<32} {', '.join(sorted(whs))}"
                    )
            else:
                self.stdout.write(self.style.WARNING("\nNo managers configured at all."))

            gaps = warehouse_scope.users_missing_assignment(code)
            if not gaps:
                self.stdout.write(
                    self.style.SUCCESS("\nNo gaps: everyone who can move stock is assigned.")
                )
                continue

            total_gaps += len(gaps)
            self.stdout.write(
                self.style.ERROR(
                    f"\n{len(gaps)} user(s) can move stock but manage NO warehouse "
                    "— they will be refused:"
                )
            )
            proposals = {}
            for user in gaps:
                history = self._worked_warehouses(user, code)
                proposals[user] = history
                shown = ", ".join(sorted(history)) if history else "no history to go on"
                self.stdout.write(
                    f"  {user.full_name or user.email:<32} "
                    f"({user.employee_code or 'no code'})   worked: {shown}"
                )

            if options.get("assign_from_history"):
                self._apply(proposals, code, commit=options.get("commit", False))

        self.stdout.write("")
        if total_gaps:
            self.stdout.write(
                self.style.ERROR(
                    f"{total_gaps} gap(s) across {len(codes)} company(ies). "
                    "Assign them on Admin -> Warehouse Managers before this ships, "
                    "or those users will be blocked."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("No gaps anywhere."))

    @staticmethod
    def _worked_warehouses(user, company_code: str) -> set:
        """Warehouses this user has actually sent stock out of."""
        whs = set(
            WarehouseTransferRequest.objects.filter(
                requested_by=user, company__code=company_code
            )
            .exclude(from_warehouse="")
            .values_list("from_warehouse", flat=True)
        )
        bsts = BSTTransfer.objects.filter(company__code=company_code).filter(
            Q(created_by=user) | Q(scan_approved_by=user)
        )
        whs |= set(
            bsts.exclude(sap_from_warehouse="").values_list(
                "sap_from_warehouse", flat=True
            )
        )
        return {w.strip().upper() for w in whs if w and w.strip()}

    def _apply(self, proposals: dict, company_code: str, *, commit: bool) -> None:
        company = Company.objects.get(code=company_code)
        planned = [(u, whs) for u, whs in proposals.items() if whs]
        if not planned:
            self.stdout.write(
                self.style.WARNING(
                    "\nNothing to propose: none of the gap users have any history."
                )
            )
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n{'Writing' if commit else 'Would write'} "
                f"{sum(len(w) for _, w in planned)} assignment(s):"
            )
        )
        for user, whs in planned:
            for code in sorted(whs):
                self.stdout.write(f"  {user.full_name or user.email} -> {code}")
                if commit:
                    UserWarehouse.objects.get_or_create(
                        user=user, company=company, warehouse_code=code
                    )
        if not commit:
            self.stdout.write(
                self.style.WARNING("\nDry run. Re-run with --commit to write these.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nWritten."))
