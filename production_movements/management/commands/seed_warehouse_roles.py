"""
Seed / re-sync the per-company WarehouseRole config from constants.WAREHOUSE_ROLE_SEED.

Idempotent upsert keyed on (company, whs_code). Optionally backfills
warehouse_name from SAP OWHS (skip with --no-names if HANA is unreachable).

    python manage.py seed_warehouse_roles
    python manage.py seed_warehouse_roles --company JIVO_OIL --no-names
"""

from django.core.management.base import BaseCommand

from company.models import Company
from production_movements.constants import WAREHOUSE_ROLE_SEED
from production_movements.models import WarehouseRole


class Command(BaseCommand):
    help = "Seed/update WarehouseRole config for the production-movement flow."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Only seed this company code.")
        parser.add_argument(
            "--no-names",
            action="store_true",
            help="Do not fetch warehouse names from SAP OWHS.",
        )

    def handle(self, *args, **opts):
        only = opts.get("company")
        fetch_names = not opts.get("no_names")

        created = updated = 0
        for company_code, rows in WAREHOUSE_ROLE_SEED.items():
            if only and company_code != only:
                continue
            try:
                company = Company.objects.get(code=company_code)
            except Company.DoesNotExist:
                self.stderr.write(f"SKIP {company_code}: no Company row.")
                continue

            names = {}
            if fetch_names:
                names = self._warehouse_names(company_code)

            for row in rows:
                defaults = {
                    "role": row["role"],
                    "family": row["family"],
                    "is_grpo_target": row["is_grpo_target"],
                    "is_bom_issue_point": row["is_bom_issue_point"],
                    "feeds_whs_code": row["feeds_whs_code"],
                    "transfer_needs_request": row["transfer_needs_request"],
                    "needs_review": row["needs_review"],
                    "notes": row["notes"],
                }
                name = names.get(row["whs_code"])
                if name:
                    defaults["warehouse_name"] = name

                obj, was_created = WarehouseRole.objects.update_or_create(
                    company=company, whs_code=row["whs_code"], defaults=defaults
                )
                created += int(was_created)
                updated += int(not was_created)
                self.stdout.write(
                    f"{'+' if was_created else '~'} {company_code}:{row['whs_code']} "
                    f"-> {row['role']}"
                )

        self.stdout.write(
            self.style.SUCCESS(f"Done. created={created} updated={updated}")
        )

    def _warehouse_names(self, company_code):
        """Best-effort OWHS name lookup; empty dict if SAP is unreachable."""
        try:
            from warehouse.services.wms_hana_reader import WMSHanaReader

            reader = WMSHanaReader(company_code)
            return {w["code"]: w["name"] for w in reader.get_warehouses()}
        except Exception as exc:  # noqa: BLE001 - names are optional
            self.stderr.write(f"  (name fetch skipped for {company_code}: {exc})")
            return {}
