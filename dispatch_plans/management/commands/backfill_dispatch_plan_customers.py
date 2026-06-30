"""Backfill ``DispatchPlan.customer_name`` / ``customer_code`` from SAP.

The customer (SAP ``CardName`` / ``CardCode``) is denormalised onto a plan when it
is booked, but plans booked before that field existed have it blank — so they show
"-" in the Docking customer column. This pulls the customer for those plans from
the SAP bills in the given date range (one SAP query) and fills it in.

Dry run by default; pass --apply to persist. Requires SAP/HANA connectivity.

    manage.py backfill_dispatch_plan_customers --company JIVO_OIL \
        --date-from 2026-03-01 --date-to 2026-06-30 --apply
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from dispatch_plans.models import DispatchPlan
from dispatch_plans.services import DispatchPlansService


class Command(BaseCommand):
    help = (
        "Fill DispatchPlan.customer_name/customer_code from SAP for plans that are "
        "missing it (dry run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Company code, e.g. JIVO_OIL.")
        parser.add_argument("--date-from", required=True, help="YYYY-MM-DD")
        parser.add_argument("--date-to", required=True, help="YYYY-MM-DD")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        company_code = options["company"]
        apply_changes = options["apply"]
        self.stdout.write("APPLY" if apply_changes else "DRY RUN (nothing written)")

        service = DispatchPlansService(company_code=company_code)
        result = service.get_bills(
            {
                "date_from": options["date_from"],
                "date_to": options["date_to"],
                "booking_status": "all",
            }
        )
        customer_by_doc = {
            row["doc_entry"]: (row.get("card_code") or "", row.get("card_name") or "")
            for row in result["data"]
        }

        plans = DispatchPlan.objects.filter(
            company__code=company_code, is_active=True, customer_name=""
        )
        targets = []
        for plan in plans:
            code, name = customer_by_doc.get(plan.sap_invoice_doc_entry, ("", ""))
            if not (name or code):
                continue
            targets.append((plan, code, name))
            self.stdout.write(f"  {plan.sap_invoice_doc_num or plan.sap_invoice_doc_entry} -> {name}")

        if apply_changes and targets:
            now = timezone.now()
            with transaction.atomic():
                for plan, code, name in targets:
                    DispatchPlan.objects.filter(id=plan.id).update(
                        customer_code=code, customer_name=name, updated_at=now
                    )

        verb = "Filled" if apply_changes else "Would fill"
        self.stdout.write(self.style.SUCCESS(f"{verb} customer on {len(targets)} plan(s)."))
