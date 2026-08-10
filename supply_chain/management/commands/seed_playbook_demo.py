"""Seed the playbook's own trial SKU with its own numbers.

The playbook ("What To Do Every Day", 10 August 2026) works one example all the
way through, using figures read from live SAP B1 / HANA:

    FG0000030 — Mustard Kachi Ghani 1 LTR 20 PCS
    582,653 bottles left to make over 18 working days  = 32,370/day
    695,819 caps on hand, 239,744 committed            = 456,075 free
    456,075 / 32,370                                   = 14 days of cover
    39-day supplier lead time from 44 past deliveries  -> RED

Seeding exactly that means the engine can be checked against a worked example
that people at the plant already recognise, rather than against numbers invented
to make the code look right.

    python manage.py seed_playbook_demo --company JIVO_OIL
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from supply_chain.models import (
    MaterialLeadTime,
    MaterialStock,
    MonitoredSku,
    OperatingParameters,
    SkuComponent,
    SupplierDelivery,
)

SKU = ("FG0000030", "MUSTARD KACHI GHANI 1 LTR 20 PCS", Decimal("582653"), 18)

# code, name, type, per bottle, unit, on hand, committed, lead days, samples, supplier
MATERIALS = [
    ("PM0000235", "Caps 1 LTR", "PACKAGING", Decimal("1"), "Pcs",
     Decimal("695819"), Decimal("239744"), 39, 44, "Cap supplier"),
    ("PM0000851", "PET Bottle 1 LTR", "PACKAGING", Decimal("1"), "Pcs",
     Decimal("0"), Decimal("0"), 26, 31, "Bottle supplier"),
    ("RM0000003", "Mustard loose oil", "RAW", Decimal("1"), "Ltr",
     Decimal("0"), Decimal("0"), 42, 22, "Oil supplier"),
    ("PM0000019", "Label — back", "PACKAGING", Decimal("1"), "Pcs",
     Decimal("291330"), Decimal("0"), 16, 18, "PrintPro"),
    ("PM0000020", "Label — front", "PACKAGING", Decimal("1"), "Pcs",
     Decimal("291330"), Decimal("0"), 16, 18, "PrintPro"),
    ("PM0000411", "Carton 20 PCS", "PACKAGING", Decimal("0.05"), "Pcs",
     Decimal("19422"), Decimal("0"), 40, 12, "BoxCorp"),
    # Over 600 days of cover — the playbook's example of a material needing no action.
    ("PM0000075", "Tape logo printed", "PACKAGING", Decimal("0.0025"), "Pcs",
     Decimal("51500"), Decimal("0"), 8, 9, "Tape supplier"),
]


class Command(BaseCommand):
    help = "Seed the playbook's trial SKU (FG0000030) with its published figures."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="JIVO_OIL")
        parser.add_argument("--samples", type=int, default=0,
                            help="Override how many delivery samples to synthesise.")

    def handle(self, *args, **options):
        company = options["company"]
        today = timezone.localdate()

        OperatingParameters.objects.update_or_create(company_code=company, defaults={})

        code, name, plan, days = SKU
        sku, _ = MonitoredSku.objects.update_or_create(
            company_code=company, sku_code=code,
            defaults={"sku_name": name, "plan_quantity": plan,
                      "working_days_left": days, "is_active": True},
        )

        SupplierDelivery.objects.filter(company_code=company).delete()
        for (mcode, mname, mtype, per_unit, unit, on_hand, committed,
             lead, samples, supplier) in MATERIALS:
            SkuComponent.objects.update_or_create(
                sku=sku, material_code=mcode,
                defaults={"material_name": mname, "material_type": mtype,
                          "quantity_per_unit": per_unit, "unit": unit},
            )
            MaterialStock.objects.update_or_create(
                company_code=company, material_code=mcode,
                defaults={"on_hand": on_hand, "committed": committed,
                          "warehouses": "Plant godowns", "source": "PLAYBOOK"},
            )
            # A typed-in fallback, so the engine still judges a material whose
            # delivery history has not been loaded.
            MaterialLeadTime.objects.update_or_create(
                company_code=company, material_code=mcode,
                defaults={"material_name": mname, "material_type": mtype,
                          "supplier_name": supplier, "lead_time_days": lead,
                          "unit": unit, "is_active": True},
            )

            # Synthesise a delivery history whose Nth percentile IS the published
            # lead time, so the measured path is exercised rather than the fallback.
            n = options["samples"] or samples
            for i in range(n):
                # Most deliveries faster than the headline figure, a tail slower —
                # which is what an 80th percentile is meant to capture.
                offset = -6 + (i % 7) if i < int(n * 0.8) else 0 + (i % 3)
                took = max(1, lead + offset)
                received = today - timedelta(days=10 + i * 3)
                SupplierDelivery.objects.create(
                    company_code=company, material_code=mcode, supplier_name=supplier,
                    ordered_on=received - timedelta(days=took), received_on=received,
                    quantity=1000, reference=f"PO-{mcode}-{i:03d}",
                )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {company}: {code}, {len(MATERIALS)} materials, "
            f"{SupplierDelivery.objects.filter(company_code=company).count()} delivery samples."
        ))
