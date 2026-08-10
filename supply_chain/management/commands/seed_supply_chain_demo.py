"""Seed the supply-chain reference data and a sample plan.

Uses the exact codes from the JIVO Supply Chain Reference Template's example rows
(M-01..M-04, FG0000030, PM-CAP-26, ...) so the dashboard can be demonstrated
before any department has returned their sheet — which is what the brief's
"working dashboard built and demonstrated for review" needs.

    python manage.py seed_supply_chain_demo --company JIVO_OIL

Writes only to this app's tables plus ``SalesPlanningRequirementRow``, and
refuses to run unless ``--force`` is given when rows already exist.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from sales_planning_requirement.models import SalesPlanningRequirementRow
from supply_chain.models import (
    MachineCapacity,
    MaterialLeadTime,
    MaterialMachineMap,
    SupplyChainPolicy,
)

MACHINES = [
    # id,    name,         location,  pack type,    size range,      out/hr, shift h, shifts, days, changeover
    ("M-01", "PET Line 1", "Plant A", "PET Bottle", "500 ML – 1 LTR", 6000, 8, 2, 26, 45),
    ("M-02", "JAR Line 1", "Plant A", "5 LTR Jar",  "3 LTR – 5 LTR",  1200, 8, 2, 26, 60),
    ("M-03", "TIN Line",   "Plant B", "TIN",        "13 KG – 15 LTR",  900, 8, 1, 26, 90),
    ("M-04", "POUCH Line", "Plant B", "Pouch",      "700 GM – 1 LTR", 4500, 8, 2, 26, 30),
]

SKUS = [
    # sku,        name,                                    brand,  pack,          size,     primary, alternates, rate
    ("FG0000030", "MUSTARD KACHI GHANI 1 LTR 20 PCS",      "JIVO", "PET 26GM",    "1 LTR",  "M-01", "M-04", 5800),
    ("FG0000142", "COLD PRESS GROUNDNUT OIL 1 LTR 16 PCS", "JIVO", "PET 52 GRAM", "1 LTR",  "M-01", "",     5200),
    ("FG0000011", "MUSTARD KACCHI GHANI 5 LTR 4 PCS",      "JIVO", "5 LTR JAR",   "5 LTR",  "M-02", "",     1150),
    ("FG0000316", "SOYABEAN OIL 13 KGS (B)",               "JIVO", "TIN",         "13 KG",  "M-03", "",      850),
]

MATERIALS = [
    # code,         name,                              type,       category,  supplier,          lead, moq,   unit
    ("PM-CAP-26",   "PET Cap 26 GM (Mustard 1 LTR)",   "PACKAGING", "Cap",    "ABC Caps Pvt Ltd", 21, 50000,  "Pcs"),
    ("PM-BTL-1L",   "PET Bottle 1 LTR",                "PACKAGING", "Bottle", "XYZ Plastics",     30, 25000,  "Pcs"),
    ("PM-LBL-JIVO", "Label — JIVO Mustard 1 LTR",      "PACKAGING", "Label",  "PrintPro",         12, 100000, "Pcs"),
    ("PM-CTN-20",   "Carton 20 PCS",                   "PACKAGING", "Carton", "BoxCorp",           7, 5000,   "Pcs"),
    ("RM-OIL-MUS",  "Mustard Oil (Loose)",             "RAW",       "Bulk Oil", "Farm Co-op",     45, 20,     "Tons"),
]

# item, planned, required, min stock, on hand, open PO
#
# ``required`` is what the HANA procedure calls required_qty: the quantity still
# needed AFTER stock on hand and the minimum-stock floor have been applied. Stock
# and min stock travel alongside as context, so they are shown but not re-deducted.
# net_shortage is then required less anything already on order.
PLAN_ROWS = [
    ("FG0000030", 120000, 40000, 30000, 90000, 0),
    ("FG0000142", 40000, 12000, 10000, 30000, 0),
    ("FG0000011", 20000, 9000, 5000, 14000, 0),
    ("FG0000316", 8000, 6000, 2000, 3500, 0),
    ("PM-CAP-26", 0, 130000, 30000, 20000, 40000),   # long lead + short -> overdue
    ("PM-BTL-1L", 0, 5000, 30000, 125000, 0),        # stock nearly covers it
    ("PM-LBL-JIVO", 0, 130000, 30000, 10000, 0),     # big shortfall, short lead
    ("PM-CTN-20", 0, 6500, 1000, 7000, 0),           # shortest lead of all
    ("RM-OIL-MUS", 0, 140, 40, 60, 30),              # raw oil, longest lead
    ("PM-SHRINK", 0, 9000, 1000, 0, 0),              # deliberately has NO lead time
]


class Command(BaseCommand):
    help = "Seed supply-chain reference data and a sample plan for demonstration."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_OIL")
        parser.add_argument("--force", action="store_true",
                            help="Overwrite existing seeded rows.")
        parser.add_argument("--start-in-days", type=int, default=20,
                            help="Days until the plan period starts (drives the alarms).")

    def handle(self, *args, **options):
        company = options["company"]
        start = timezone.localdate() + timedelta(days=options["start_in_days"])
        end = start + timedelta(days=30)

        existing = MachineCapacity.objects.filter(company_code=company).exists()
        if existing and not options["force"]:
            self.stderr.write(
                f"{company} already has supply-chain reference data. Re-run with --force."
            )
            return

        SupplyChainPolicy.objects.update_or_create(company_code=company, defaults={})

        for mid, name, loc, pack, size, rate, sh, spd, days, chg in MACHINES:
            MachineCapacity.objects.update_or_create(
                company_code=company, machine_id=mid,
                defaults=dict(
                    name=name, location=loc, pack_type=pack, pack_size_range=size,
                    output_per_hour=rate, shift_hours=sh, shifts_per_day=spd,
                    working_days_per_month=days, changeover_minutes=chg, is_active=True,
                ),
            )

        for sku, name, brand, pack, size, primary, alts, rate in SKUS:
            MaterialMachineMap.objects.update_or_create(
                company_code=company, sku_code=sku,
                defaults=dict(
                    sku_name=name, brand=brand, pack_type=pack, pack_size=size,
                    primary_machine_id=primary, alternate_machine_ids=alts,
                    output_on_primary=rate, is_active=True,
                ),
            )

        for code, name, mtype, cat, supplier, lead, moq, unit in MATERIALS:
            MaterialLeadTime.objects.update_or_create(
                company_code=company, material_code=code,
                defaults=dict(
                    material_name=name, material_type=mtype, category=cat,
                    supplier_name=supplier, lead_time_days=lead, moq=moq, unit=unit,
                    is_active=True,
                ),
            )

        SalesPlanningRequirementRow.objects.filter(
            company_code=company, forecast_name="SEED DEMO"
        ).delete()
        for code, planned, required, min_stock, on_hand, open_po in PLAN_ROWS:
            shortage = max(Decimal(required) - Decimal(open_po), Decimal("0"))
            SalesPlanningRequirementRow.objects.create(
                company_code=company, source_schema="SEED", forecast_id=None,
                forecast_name="SEED DEMO", forecast_start_date=start, forecast_end_date=end,
                planning_month=f"{start:%B %Y}", item_code=code, item_name=code,
                planned_qty=planned, base_required_qty=required, min_stock=min_stock,
                stock_in_hand=on_hand, required_qty=required, open_po_qty=open_po,
                net_shortage_qty=shortage,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {company}: {len(MACHINES)} machines, {len(SKUS)} SKUs, "
            f"{len(MATERIALS)} materials, {len(PLAN_ROWS)} plan rows "
            f"(plan starts {start:%d %b %Y})."
        ))
