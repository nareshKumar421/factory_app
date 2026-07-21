from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from company.models import Company
from grpo.models import GRPOStatus, ServiceGRPOLinePosting, ServiceGRPOPosting

from ...models import (
    DispatchPlan,
    DispatchPlanStatus,
    TransporterAPInvoiceAttachment,
    TransporterAPInvoiceLine,
    TransporterAPInvoicePosting,
    TransporterAPInvoiceStatus,
)


class Command(BaseCommand):
    help = (
        "Seed local dispatch Service GRPO and transporter A/P invoice data "
        "for UI testing without posting to SAP."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company-code",
            default="JIVO_OIL",
            help="Company code to seed data for. Defaults to JIVO_OIL.",
        )
        parser.add_argument(
            "--branch-id",
            type=int,
            default=2,
            help="Branch/BPL id shown in the local A/P invoice test flow.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        company_code = options["company_code"]
        branch_id = options["branch_id"]
        try:
            company = Company.objects.get(code=company_code)
        except Company.DoesNotExist as exc:
            raise CommandError(f"Company {company_code!r} was not found.") from exc

        user = get_user_model().objects.order_by("id").first()
        now = timezone.now()

        self._clear_existing_dummy_data(company)

        open_posting = self._seed_service_grpo(
            company=company,
            user=user,
            sap_invoice_doc_entry=990001,
            sap_invoice_doc_num="990001",
            sap_grpo_doc_entry=880001,
            sap_grpo_doc_num=880001,
            bilty_no="TEST-BILTY-OPEN",
            vehicle_no="TEST-TRUCK-01",
            amount=Decimal("1234.00"),
            now=now,
        )
        second_open_posting = self._seed_service_grpo(
            company=company,
            user=user,
            sap_invoice_doc_entry=990003,
            sap_invoice_doc_num="990003",
            sap_grpo_doc_entry=880003,
            sap_grpo_doc_num=880003,
            bilty_no="TEST-BILTY-OPEN-2",
            vehicle_no="TEST-TRUCK-03",
            amount=Decimal("1750.50"),
            now=now,
        )
        pending_posting = self._seed_service_grpo(
            company=company,
            user=user,
            sap_invoice_doc_entry=990002,
            sap_invoice_doc_num="990002",
            sap_grpo_doc_entry=880002,
            sap_grpo_doc_num=880002,
            bilty_no="TEST-BILTY-AP",
            vehicle_no="TEST-TRUCK-02",
            amount=Decimal("2500.00"),
            now=now,
        )
        ap_invoice = self._seed_pending_ap_invoice(
            company=company,
            user=user,
            branch_id=branch_id,
            service_grpo=pending_posting,
        )

        self.stdout.write(self.style.SUCCESS("Seeded dispatch invoice UI data."))
        self.stdout.write(f"Open bilty Service GRPO id: {open_posting.id}")
        self.stdout.write(f"Second open bilty Service GRPO id: {second_open_posting.id}")
        self.stdout.write(f"Pending A/P Invoice id: {ap_invoice.id}")
        self.stdout.write(
            "Use /dispatch/open-bilties for the open test bilty and "
            "/dispatch/transporter-invoices/pending for the pending A/P invoice."
        )

    def _clear_existing_dummy_data(self, company):
        dummy_grpo_doc_entries = [880001, 880002, 880003]
        dummy_grpos = ServiceGRPOPosting.objects.filter(
            dispatch_plan__company=company,
            sap_doc_entry__in=dummy_grpo_doc_entries,
        )
        TransporterAPInvoicePosting.objects.filter(
            company=company,
            lines__service_grpo_posting__in=dummy_grpos,
        ).distinct().delete()
        TransporterAPInvoicePosting.objects.filter(
            company=company,
            invoice_number__startswith="TEST-AP-INV",
        ).delete()

    def _seed_service_grpo(
        self,
        *,
        company,
        user,
        sap_invoice_doc_entry: int,
        sap_invoice_doc_num: str,
        sap_grpo_doc_entry: int,
        sap_grpo_doc_num: int,
        bilty_no: str,
        vehicle_no: str,
        amount: Decimal,
        now,
    ) -> ServiceGRPOPosting:
        plan, _ = DispatchPlan.objects.update_or_create(
            company=company,
            sap_invoice_doc_entry=sap_invoice_doc_entry,
            defaults={
                "sap_invoice_doc_num": sap_invoice_doc_num,
                "invoice_number": sap_invoice_doc_num,
                "invoice_amount": amount,
                "invoice_weight": Decimal("1250.000"),
                "place_of_supply": "HR",
                "product_variety": "Oil",
                "total_litres": Decimal("0.000"),
                "effective_month": date.today().replace(day=1),
                "budget_delivery_point": "TEST-DELIVERY",
                "service_location_code": 2,
                "service_location_name": "HARYANA",
                "sac_entry": 40,
                "sac_code": "9965",
                "booking_status": DispatchPlanStatus.BOOKED,
                "dispatch_date": date.today(),
                "bilty_no": bilty_no,
                "bilty_date": date.today(),
                "freight": amount,
                "total_freight": amount,
                "is_active": True,
            },
        )
        posting, _ = ServiceGRPOPosting.objects.update_or_create(
            dispatch_plan=plan,
            sap_doc_entry=sap_grpo_doc_entry,
            defaults={
                "vendor_code": "VENDA_TEST_TRANSPORT",
                "vendor_name": "Test Transporter",
                "sap_doc_num": sap_grpo_doc_num,
                "sap_doc_total": amount,
                "place_of_supply": "HR",
                "effective_month": plan.effective_month,
                "budget_delivery_point": plan.budget_delivery_point,
                "location_code": plan.service_location_code,
                "location_name": plan.service_location_name,
                "sac_entry": plan.sac_entry,
                "sac_code": plan.sac_code,
                "product_variety": plan.product_variety,
                "total_litres": plan.total_litres,
                "status": GRPOStatus.POSTED,
                "error_message": "",
                "posted_at": now,
                "posted_by": user,
            },
        )
        ServiceGRPOLinePosting.objects.update_or_create(
            service_grpo_posting=posting,
            service_description=f"Test transport freight for {bilty_no}",
            defaults={
                "amount": amount,
                "unit_price": amount,
                "tax_code": "GST05R",
                "gl_account": "5670002",
                "sac_entry": plan.sac_entry,
                "sac_code": plan.sac_code,
                "location_code": plan.service_location_code,
                "location_name": plan.service_location_name,
                "project_code": plan.budget_delivery_point,
                "product_variety": plan.product_variety,
                "total_litres": plan.total_litres,
            },
        )
        return posting

    def _seed_pending_ap_invoice(
        self,
        *,
        company,
        user,
        branch_id: int,
        service_grpo: ServiceGRPOPosting,
    ) -> TransporterAPInvoicePosting:
        invoice_number = "TEST-AP-INV-001"
        TransporterAPInvoicePosting.objects.filter(
            company=company,
            invoice_number=invoice_number,
        ).delete()
        amount = service_grpo.sap_doc_total or Decimal("0.00")
        posting = TransporterAPInvoicePosting.objects.create(
            company=company,
            vendor_code=service_grpo.vendor_code,
            vendor_name=service_grpo.vendor_name,
            invoice_number=invoice_number,
            invoice_date=date.today(),
            invoice_amount=amount,
            selected_grpo_total=amount,
            amount_difference=Decimal("0.00"),
            branch_id=branch_id,
            status=TransporterAPInvoiceStatus.PENDING,
            comments="Seeded local test invoice",
            created_by=user,
            updated_by=user,
        )
        service_line = service_grpo.lines.order_by("id").first()
        TransporterAPInvoiceLine.objects.create(
            transporter_ap_invoice=posting,
            service_grpo_posting=service_grpo,
            service_grpo_line=service_line,
            dispatch_plan=service_grpo.dispatch_plan,
            base_entry=service_grpo.sap_doc_entry,
            base_line=0,
            base_doc_num=service_grpo.sap_doc_num,
            bilty_no=service_grpo.dispatch_plan.bilty_no,
            service_description=(
                service_line.service_description if service_line else "Test transport freight"
            ),
            line_total=amount,
            tax_code=service_line.tax_code if service_line else "GST05R",
            gl_account=service_line.gl_account if service_line else "5670002",
        )
        attachment = TransporterAPInvoiceAttachment(
            transporter_ap_invoice=posting,
            original_filename="dummy-transporter-invoice.txt",
            sap_attachment_status="PENDING",
            uploaded_by=user,
        )
        attachment.file.save(
            "dummy-transporter-invoice.txt",
            ContentFile(b"Dummy transporter invoice for local UI testing.\n"),
            save=True,
        )
        return posting
