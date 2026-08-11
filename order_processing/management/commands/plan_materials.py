"""Explode open production requirements into materials, then procurement.

    python manage.py plan_materials
    python manage.py plan_materials --bom-depth 2

Reads SAP; creates planning records only. No SAP purchase order is created from
here (Rule 11) -- that stays a human act.
"""
from django.core.management.base import BaseCommand

from order_processing.models import MaterialRequirement, ProcurementRequirement, ProcurementStatus
from order_processing.services import material_planning


class Command(BaseCommand):
    help = "Explode BOMs for open production requirements and plan procurement."

    def add_arguments(self, parser):
        parser.add_argument("--bom-depth", type=int, default=1,
                            help="How many BOM levels to walk. Cycles are refused.")
        parser.add_argument("--show", action="store_true")

    def handle(self, *args, **options):
        summary = material_planning.plan_all(bom_depth=options["bom_depth"])
        self.stdout.write(
            f"Exploded {summary['exploded']} requirement(s); "
            f"{summary['no_bom']} had no BOM; {summary['failed']} failed."
        )
        if summary["missing_boms"]:
            # Not a footnote: these cannot be made, so their shortfall has nowhere
            # to go.
            self.stdout.write(self.style.WARNING(
                "  No BOM in SAP for: " + ", ".join(sorted(set(summary["missing_boms"]))[:12])
            ))
        self.stdout.write(
            f"Procurement: {summary['procurement_created']} new, "
            f"{summary['procurement_updated']} updated, {summary['procurement_retired']} retired."
        )

        if not options["show"]:
            return

        shorts = MaterialRequirement.objects.filter(net_required__gt=0, stock_known=True)[:15]
        if shorts:
            self.stdout.write(self.style.WARNING(f"\nShort materials ({shorts.count()}):"))
            self.stdout.write(f"  {'material':<12s} {'gross':>12s} {'on hand':>11s} "
                              f"{'incoming':>10s} {'net':>12s}  for")
            for m in shorts:
                self.stdout.write(
                    f"  {m.item_code:<12s} {m.gross_required:>12} {m.on_hand:>11} "
                    f"{m.incoming_po:>10} {m.net_required:>12}  {m.requirement.item_code}"
                )

        procs = ProcurementRequirement.objects.filter(status=ProcurementStatus.REQUIRED)[:15]
        if procs:
            self.stdout.write(self.style.ERROR(f"\nProcurement required ({procs.count()}):"))
            self.stdout.write(f"  {'material':<12s} {'buy':>12s} {'incoming':>10s} {'needed by':>12s}")
            for p in procs:
                self.stdout.write(
                    f"  {p.item_code:<12s} {p.quantity:>12} {p.incoming_po:>10} "
                    f"{str(p.needed_by or '-'):>12s}"
                )
