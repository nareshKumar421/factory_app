import logging
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from hdbcli import dbapi
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from company.models import Company
from grpo.models import GRPOStatus, SAPAttachmentStatus, ServiceGRPOPosting
from sap_client.client import SAPClient
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.hana.connection import HanaConnection

from .models import (
    TransporterAPInvoiceAttachment,
    TransporterAPInvoiceLine,
    TransporterAPInvoicePosting,
    TransporterAPInvoiceStatus,
)

logger = logging.getLogger(__name__)


class DispatchInvoiceService:
    """Coordinates open bilty service GRPOs and transporter A/P invoices."""

    AMOUNT_TOLERANCE = Decimal("1.00")

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.company = Company.objects.get(code=company_code)
        self.context = CompanyContext(company_code)
        self.connection = HanaConnection(self.context.hana)

    def get_open_bilties(self) -> List[Dict[str, Any]]:
        postings = list(
            ServiceGRPOPosting.objects.select_related(
                "dispatch_plan",
                "dispatch_plan__company",
                "dispatch_plan__transporter",
                "dispatch_plan__vehicle",
                "dispatch_plan__driver",
            )
            .prefetch_related("lines")
            .filter(
                dispatch_plan__company=self.company,
                dispatch_plan__is_active=True,
                status=GRPOStatus.POSTED,
                sap_doc_entry__isnull=False,
            )
            .exclude(
                transporter_ap_invoice_lines__transporter_ap_invoice__status__in=[
                    TransporterAPInvoiceStatus.PENDING,
                    TransporterAPInvoiceStatus.POSTED,
                    TransporterAPInvoiceStatus.FAILED,
                ]
            )
            .distinct()
            .order_by("-posted_at", "-created_at")
        )

        if not postings:
            return []

        sap_lines = self._fetch_sap_grpo_lines(
            [posting.sap_doc_entry for posting in postings if posting.sap_doc_entry]
        )

        open_bilties = []
        for posting in postings:
            rows = sap_lines.get(posting.sap_doc_entry, [])
            open_rows = [
                row
                for row in rows
                if row["canceled"] == "N" and not row["already_invoiced"]
            ]
            if not open_rows:
                continue

            first_row = open_rows[0]
            plan = posting.dispatch_plan
            vehicle_no = plan.vehicle_no or (
                plan.vehicle.vehicle_number if plan.vehicle_id else ""
            )
            open_bilties.append(
                {
                    "service_grpo_posting_id": posting.id,
                    "dispatch_plan_id": plan.id,
                    "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
                    "sap_invoice_doc_num": plan.sap_invoice_doc_num or "",
                    "dispatch_date": plan.dispatch_date,
                    "vehicle_no": vehicle_no,
                    "driver_name": plan.driver_name or "",
                    "transporter_id": plan.transporter_id,
                    "transporter_name": plan.transporter_name or posting.vendor_name,
                    "vendor_code": posting.vendor_code,
                    "vendor_name": posting.vendor_name or first_row["card_name"],
                    "branch_id": first_row["branch_id"],
                    "bilty_no": plan.bilty_no or "",
                    "bilty_date": plan.bilty_date,
                    "grpo_doc_entry": posting.sap_doc_entry,
                    "grpo_doc_num": posting.sap_doc_num,
                    "grpo_doc_total": self._decimal(
                        posting.sap_doc_total or first_row["doc_total"]
                    ),
                    "posted_at": posting.posted_at,
                    "line_count": len(open_rows),
                }
            )
        return open_bilties

    def preview_ap_invoice(
        self,
        service_grpo_posting_ids: List[int],
        vendor_code: Optional[str] = None,
        branch_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._build_preview(
            service_grpo_posting_ids=service_grpo_posting_ids,
            vendor_code=vendor_code,
            branch_id=branch_id,
        )

    def post_ap_invoice(
        self,
        service_grpo_posting_ids: List[int],
        user,
        invoice_number: str,
        invoice_amount: Decimal,
        attachments: Optional[list] = None,
        invoice_date=None,
        doc_date=None,
        doc_due_date=None,
        tax_date=None,
        vendor_code: Optional[str] = None,
        branch_id: Optional[int] = None,
        comments: str = "",
    ) -> TransporterAPInvoicePosting:
        posting = self.submit_ap_invoice(
            service_grpo_posting_ids=service_grpo_posting_ids,
            user=user,
            invoice_number=invoice_number,
            invoice_amount=invoice_amount,
            attachments=attachments,
            invoice_date=invoice_date,
            vendor_code=vendor_code,
            branch_id=branch_id,
            comments=comments,
        )
        return self.post_submitted_ap_invoice(
            posting_id=posting.id,
            user=user,
            doc_date=doc_date,
            doc_due_date=doc_due_date,
            tax_date=tax_date,
            comments=comments,
        )

    def submit_ap_invoice(
        self,
        service_grpo_posting_ids: List[int],
        user,
        invoice_number: str,
        invoice_amount: Decimal,
        attachments: Optional[list] = None,
        invoice_date=None,
        vendor_code: Optional[str] = None,
        branch_id: Optional[int] = None,
        comments: str = "",
    ) -> TransporterAPInvoicePosting:
        invoice_number = (invoice_number or "").strip()
        if not invoice_number:
            raise ValueError("Transporter invoice number is required.")

        attachments = attachments or []
        if not attachments:
            raise ValueError("At least one transporter invoice attachment is required.")

        preview = self._build_preview(
            service_grpo_posting_ids=service_grpo_posting_ids,
            vendor_code=vendor_code,
            branch_id=branch_id,
        )

        amount_difference = self._decimal(invoice_amount) - self._decimal(
            preview["selected_grpo_total"]
        )
        if abs(amount_difference) > self.AMOUNT_TOLERANCE:
            raise ValueError(
                "Transporter invoice amount does not match selected GRPO total "
                f"within INR {self.AMOUNT_TOLERANCE}. Difference: {amount_difference}."
            )

        self._guard_duplicate_invoice(
            vendor_code=preview["vendor_code"],
            invoice_number=invoice_number,
        )

        with transaction.atomic():
            posting = TransporterAPInvoicePosting.objects.create(
                company=self.company,
                vendor_code=preview["vendor_code"],
                vendor_name=preview["vendor_name"],
                invoice_number=invoice_number,
                invoice_date=invoice_date,
                invoice_amount=self._decimal(invoice_amount),
                selected_grpo_total=preview["selected_grpo_total"],
                amount_difference=amount_difference,
                branch_id=preview["branch_id"],
                status=TransporterAPInvoiceStatus.PENDING,
                comments=comments or "",
                created_by=user,
                updated_by=user,
            )
            self._create_local_lines(posting, preview["lines"])
            self._create_attachment_records(
                posting=posting,
                attachments=attachments,
                user=user,
            )

        return posting

    def post_submitted_ap_invoice(
        self,
        posting_id: int,
        user,
        doc_date=None,
        doc_due_date=None,
        tax_date=None,
        comments: str = "",
    ) -> TransporterAPInvoicePosting:
        posting = self.get_ap_invoice(posting_id)
        if posting.status not in (
            TransporterAPInvoiceStatus.PENDING,
            TransporterAPInvoiceStatus.FAILED,
        ):
            raise ValueError("Only pending or failed A/P invoices can be posted to SAP.")

        attachment_records = posting.attachments.all().order_by("id")
        if not attachment_records.exists():
            raise ValueError("At least one transporter invoice attachment is required.")

        service_grpo_posting_ids = list(
            dict.fromkeys(
                posting.lines.values_list("service_grpo_posting_id", flat=True)
            )
        )
        preview = self._build_preview(
            service_grpo_posting_ids=service_grpo_posting_ids,
            vendor_code=posting.vendor_code,
            branch_id=posting.branch_id,
            exclude_invoice_posting_id=posting.id,
        )

        amount_difference = self._decimal(posting.invoice_amount) - self._decimal(
            preview["selected_grpo_total"]
        )
        if abs(amount_difference) > self.AMOUNT_TOLERANCE:
            raise ValueError(
                "Transporter invoice amount does not match selected GRPO total "
                f"within INR {self.AMOUNT_TOLERANCE}. Difference: {amount_difference}."
            )

        self._guard_duplicate_invoice(
            vendor_code=posting.vendor_code,
            invoice_number=posting.invoice_number,
            exclude_posting_id=posting.id,
        )

        if amount_difference != posting.amount_difference:
            posting.selected_grpo_total = preview["selected_grpo_total"]
            posting.amount_difference = amount_difference
            posting.updated_by = user
            posting.save(
                update_fields=[
                    "selected_grpo_total",
                    "amount_difference",
                    "updated_by",
                    "updated_at",
                ]
            )

        sap_comments = posting.comments or ""
        if comments:
            sap_comments = " | ".join(part for part in [sap_comments, comments] if part)

        if getattr(settings, "DISPATCH_SIMULATE_AP_INVOICE_POSTING", False):
            logger.info(
                "Simulating transporter A/P invoice SAP post for %s in local test mode.",
                posting.invoice_number,
            )
            posting.sap_doc_entry = 990000000 + posting.id
            posting.sap_doc_num = 980000000 + posting.id
            posting.sap_doc_total = self._decimal(posting.invoice_amount)
            posting.status = TransporterAPInvoiceStatus.POSTED
            posting.posted_at = timezone.now()
            posting.posted_by = user
            posting.updated_by = user
            posting.error_message = None
            posting.save(
                update_fields=[
                    "sap_doc_entry",
                    "sap_doc_num",
                    "sap_doc_total",
                    "status",
                    "posted_at",
                    "posted_by",
                    "updated_by",
                    "error_message",
                    "updated_at",
                ]
            )
            attachment_records.update(
                sap_attachment_status=SAPAttachmentStatus.LINKED,
                sap_absolute_entry=posting.sap_doc_entry,
                sap_error_message=None,
            )
            return posting

        sap_client = SAPClient(company_code=self.company_code)
        try:
            attachment_entry = self._upload_attachments_to_sap(
                sap_client=sap_client,
                attachments=attachment_records,
            )
            payload = self._build_sap_payload(
                preview=preview,
                invoice_number=posting.invoice_number,
                invoice_date=posting.invoice_date,
                doc_date=doc_date,
                doc_due_date=doc_due_date,
                tax_date=tax_date,
                comments=sap_comments,
                user=user,
                attachment_entry=attachment_entry,
            )
            logger.info(
                "Transporter A/P invoice payload for %s: %s",
                posting.invoice_number,
                payload,
            )
            result = sap_client.create_ap_invoice(payload)
        except (SAPValidationError, SAPConnectionError, SAPDataError) as exc:
            self._mark_posting_failed(posting, str(exc), user=user)
            raise
        except Exception as exc:
            self._mark_posting_failed(posting, str(exc), user=user)
            raise SAPDataError(f"Unexpected error: {str(exc)}") from exc

        posting.sap_doc_entry = result.get("DocEntry")
        posting.sap_doc_num = result.get("DocNum")
        posting.sap_doc_total = self._decimal(result.get("DocTotal", 0))
        posting.status = TransporterAPInvoiceStatus.POSTED
        posting.posted_at = timezone.now()
        posting.posted_by = user
        posting.updated_by = user
        posting.error_message = None
        posting.save(
            update_fields=[
                "sap_doc_entry",
                "sap_doc_num",
                "sap_doc_total",
                "status",
                "posted_at",
                "posted_by",
                "updated_by",
                "error_message",
                "updated_at",
            ]
        )
        attachment_records.update(sap_attachment_status=SAPAttachmentStatus.LINKED)
        return posting

    def get_ap_invoice_history(self) -> Iterable[TransporterAPInvoicePosting]:
        return (
            TransporterAPInvoicePosting.objects.filter(company=self.company)
            .select_related("company", "posted_by")
            .prefetch_related("lines", "attachments")
            .order_by("-created_at")
        )

    def get_ap_invoice(self, posting_id: int) -> TransporterAPInvoicePosting:
        try:
            return (
                TransporterAPInvoicePosting.objects.filter(company=self.company)
                .select_related("company", "posted_by")
                .prefetch_related("lines", "attachments")
                .get(id=posting_id)
            )
        except TransporterAPInvoicePosting.DoesNotExist as exc:
            raise ValueError("Transporter AP invoice posting not found.") from exc

    def _build_preview(
        self,
        service_grpo_posting_ids: List[int],
        vendor_code: Optional[str] = None,
        branch_id: Optional[int] = None,
        exclude_invoice_posting_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        posting_ids = list(dict.fromkeys(service_grpo_posting_ids or []))
        if not posting_ids:
            raise ValueError("At least one posted bilty GRPO must be selected.")

        postings = list(
            ServiceGRPOPosting.objects.select_related(
                "dispatch_plan",
                "dispatch_plan__company",
            )
            .prefetch_related("lines")
            .filter(
                id__in=posting_ids,
                dispatch_plan__company=self.company,
                dispatch_plan__is_active=True,
            )
        )
        found_ids = {posting.id for posting in postings}
        missing_ids = sorted(set(posting_ids) - found_ids)
        if missing_ids:
            raise ValueError(f"Bilty GRPO posting(s) not found: {missing_ids}")

        for posting in postings:
            if posting.status != GRPOStatus.POSTED:
                raise ValueError(
                    f"Service GRPO {posting.id} is not posted to SAP yet."
                )
            if not posting.sap_doc_entry:
                raise ValueError(
                    f"Service GRPO {posting.id} has no SAP DocEntry."
                )

        local_invoiced = TransporterAPInvoiceLine.objects.filter(
            service_grpo_posting_id__in=posting_ids,
            transporter_ap_invoice__status__in=[
                TransporterAPInvoiceStatus.PENDING,
                TransporterAPInvoiceStatus.POSTED,
                TransporterAPInvoiceStatus.FAILED,
            ],
        )
        if exclude_invoice_posting_id:
            local_invoiced = local_invoiced.exclude(
                transporter_ap_invoice_id=exclude_invoice_posting_id
            )
        local_invoiced = local_invoiced.values_list(
            "service_grpo_posting_id", flat=True
        )
        if local_invoiced:
            raise ValueError(
                "One or more selected bilty GRPOs are already submitted for invoicing."
            )

        vendor_codes = {posting.vendor_code for posting in postings}
        if len(vendor_codes) != 1:
            raise ValueError("Selected bilties must belong to the same transporter/vendor.")
        selected_vendor_code = next(iter(vendor_codes))
        if vendor_code and vendor_code != selected_vendor_code:
            raise ValueError("Selected bilties do not match the requested vendor.")

        sap_lines = self._fetch_sap_grpo_lines(
            [posting.sap_doc_entry for posting in postings]
        )

        preview_lines = []
        selected_total = Decimal("0.00")
        selected_branch_ids = set()
        vendor_name = postings[0].vendor_name or ""

        for posting in postings:
            rows = sap_lines.get(posting.sap_doc_entry, [])
            if not rows:
                raise ValueError(
                    f"SAP GRPO {posting.sap_doc_entry} was not found."
                )

            first_row = rows[0]
            if first_row["doc_type"] != "S":
                raise ValueError(
                    f"SAP GRPO {posting.sap_doc_num or posting.sap_doc_entry} is not a service GRPO."
                )
            if first_row["canceled"] != "N":
                raise ValueError(
                    f"SAP GRPO {posting.sap_doc_num or posting.sap_doc_entry} is cancelled."
                )
            if first_row["card_code"] != selected_vendor_code:
                raise ValueError(
                    f"SAP GRPO {posting.sap_doc_num or posting.sap_doc_entry} vendor does not match."
                )

            vendor_name = vendor_name or first_row["card_name"]
            selected_branch_ids.add(first_row["branch_id"])
            selected_total += self._decimal(posting.sap_doc_total or first_row["doc_total"])

            line_rows = {row["line_num"]: row for row in rows}
            local_lines = list(posting.lines.all().order_by("id"))
            if not local_lines:
                local_lines = [None for _ in rows]

            for index, local_line in enumerate(local_lines):
                row = line_rows.get(index)
                if not row:
                    raise ValueError(
                        f"SAP GRPO {posting.sap_doc_entry} line {index} was not found."
                    )
                if row["already_invoiced"]:
                    raise ValueError(
                        f"SAP GRPO {posting.sap_doc_num or posting.sap_doc_entry} line {index} is already invoiced."
                    )

                line_dispatch_plan = (
                    getattr(local_line, "dispatch_plan", None)
                    if local_line is not None
                    else posting.dispatch_plan
                ) or posting.dispatch_plan
                preview_lines.append(
                    {
                        "service_grpo_posting_id": posting.id,
                        "service_grpo_line_id": getattr(local_line, "id", None),
                        "dispatch_plan_id": line_dispatch_plan.id,
                        "bilty_no": line_dispatch_plan.bilty_no or "",
                        "grpo_doc_entry": posting.sap_doc_entry,
                        "grpo_doc_num": posting.sap_doc_num,
                        "grpo_line_num": index,
                        "service_description": (
                            getattr(local_line, "service_description", "")
                            or row["description"]
                        ),
                        "line_total": self._decimal(row["line_total"]),
                        "tax_code": getattr(local_line, "tax_code", "") or row["tax_code"],
                        "gl_account": (
                            getattr(local_line, "gl_account", "")
                            or row["gl_account"]
                        ),
                    }
                )

        if len(selected_branch_ids) != 1:
            raise ValueError("Selected bilties must belong to the same SAP branch.")

        selected_branch_id = next(iter(selected_branch_ids))
        if selected_branch_id is None:
            raise ValueError("Selected bilty GRPOs do not have an SAP branch.")
        if branch_id is not None and int(branch_id) != int(selected_branch_id):
            raise ValueError("Selected bilties do not match the requested branch.")

        return {
            "vendor_code": selected_vendor_code,
            "vendor_name": vendor_name,
            "branch_id": int(selected_branch_id),
            "selected_grpo_total": selected_total.quantize(Decimal("0.01")),
            "tolerance": self.AMOUNT_TOLERANCE,
            "lines": preview_lines,
        }

    def _guard_duplicate_invoice(
        self,
        vendor_code: str,
        invoice_number: str,
        exclude_posting_id: Optional[int] = None,
    ) -> None:
        local_duplicate = TransporterAPInvoicePosting.objects.filter(
            company=self.company,
            vendor_code=vendor_code,
            invoice_number=invoice_number,
            status__in=[
                TransporterAPInvoiceStatus.PENDING,
                TransporterAPInvoiceStatus.POSTED,
                TransporterAPInvoiceStatus.FAILED,
            ],
        )
        if exclude_posting_id:
            local_duplicate = local_duplicate.exclude(id=exclude_posting_id)
        local_duplicate = local_duplicate.exists()
        if local_duplicate:
            raise ValueError(
                "This transporter invoice number has already been submitted locally."
            )

        if self._sap_invoice_exists(vendor_code, invoice_number):
            raise ValueError(
                "This transporter invoice number is already posted in SAP."
            )

    def _create_local_lines(
        self,
        posting: TransporterAPInvoicePosting,
        preview_lines: List[Dict[str, Any]],
    ) -> None:
        lines = [
            TransporterAPInvoiceLine(
                transporter_ap_invoice=posting,
                service_grpo_posting_id=line["service_grpo_posting_id"],
                service_grpo_line_id=line["service_grpo_line_id"],
                dispatch_plan_id=line["dispatch_plan_id"],
                base_entry=line["grpo_doc_entry"],
                base_line=line["grpo_line_num"],
                base_doc_num=line["grpo_doc_num"],
                bilty_no=line["bilty_no"],
                service_description=line["service_description"],
                line_total=line["line_total"],
                tax_code=line["tax_code"],
                gl_account=line["gl_account"],
            )
            for line in preview_lines
        ]
        TransporterAPInvoiceLine.objects.bulk_create(lines)

    def _create_attachment_records(
        self,
        posting: TransporterAPInvoicePosting,
        attachments: list,
        user,
    ):
        records = []
        for uploaded_file in attachments:
            records.append(
                TransporterAPInvoiceAttachment.objects.create(
                    transporter_ap_invoice=posting,
                    file=uploaded_file,
                    original_filename=getattr(uploaded_file, "name", ""),
                    sap_attachment_status=SAPAttachmentStatus.PENDING,
                    uploaded_by=user,
                )
            )
        return TransporterAPInvoiceAttachment.objects.filter(
            id__in=[record.id for record in records]
        ).order_by("id")

    def _upload_attachments_to_sap(
        self,
        sap_client: SAPClient,
        attachments,
    ) -> Optional[int]:
        sap_absolute_entry = None
        for attachment in attachments:
            try:
                if sap_absolute_entry:
                    sap_client.add_line_to_existing_attachment(
                        absolute_entry=sap_absolute_entry,
                        file_path=attachment.file.path,
                        filename=attachment.original_filename,
                    )
                    abs_entry = sap_absolute_entry
                else:
                    sap_result = sap_client.upload_attachment(
                        file_path=attachment.file.path,
                        filename=attachment.original_filename,
                    )
                    abs_entry = sap_result.get("AbsoluteEntry")

                if not abs_entry:
                    raise SAPDataError("SAP did not return attachment AbsoluteEntry.")

                sap_absolute_entry = abs_entry
                attachment.sap_absolute_entry = abs_entry
                attachment.sap_attachment_status = SAPAttachmentStatus.UPLOADED
                attachment.sap_error_message = None
                attachment.save(
                    update_fields=[
                        "sap_absolute_entry",
                        "sap_attachment_status",
                        "sap_error_message",
                    ]
                )
            except (SAPValidationError, SAPConnectionError, SAPDataError) as exc:
                attachment.sap_attachment_status = SAPAttachmentStatus.FAILED
                attachment.sap_error_message = str(exc)
                attachment.save(
                    update_fields=["sap_attachment_status", "sap_error_message"]
                )
                raise
        return sap_absolute_entry

    def _build_sap_payload(
        self,
        preview: Dict[str, Any],
        invoice_number: str,
        invoice_date,
        doc_date,
        doc_due_date,
        tax_date,
        comments: str,
        user,
        attachment_entry: Optional[int],
    ) -> Dict[str, Any]:
        payload = {
            "DocType": "dDocument_Service",
            "CardCode": preview["vendor_code"],
            "NumAtCard": invoice_number,
            "BPL_IDAssignedToInvoice": preview["branch_id"],
            "Comments": self._build_structured_comments(
                invoice_number=invoice_number,
                comments=comments,
                user=user,
            ),
            "DocumentLines": [
                {
                    "BaseType": 20,
                    "BaseEntry": line["grpo_doc_entry"],
                    "BaseLine": line["grpo_line_num"],
                }
                for line in preview["lines"]
            ],
        }
        if doc_date:
            payload["DocDate"] = str(doc_date)
        if doc_due_date:
            payload["DocDueDate"] = str(doc_due_date)
        if tax_date:
            payload["TaxDate"] = str(tax_date)
        if invoice_date and not tax_date:
            payload["TaxDate"] = str(invoice_date)
        if attachment_entry:
            payload["AttachmentEntry"] = attachment_entry
        return payload

    @staticmethod
    def _build_structured_comments(invoice_number: str, comments: str, user) -> str:
        full_name = user.get_full_name() if hasattr(user, "get_full_name") else str(user)
        username = getattr(user, "username", getattr(user, "email", str(user)))
        parts = [
            "App: FactoryApp v2",
            f"User: {full_name} ({username})",
            "Document: Transporter A/P Invoice",
            f"Transporter Invoice: {invoice_number}",
        ]
        if comments:
            parts.append(comments)
        return " | ".join(parts)

    def _mark_posting_failed(
        self,
        posting: TransporterAPInvoicePosting,
        error_message: str,
        user=None,
    ) -> None:
        posting.status = TransporterAPInvoiceStatus.FAILED
        posting.error_message = error_message
        if user:
            posting.updated_by = user
        posting.save(update_fields=["status", "error_message", "updated_by", "updated_at"])

    def _sap_invoice_exists(self, vendor_code: str, invoice_number: str) -> bool:
        if getattr(settings, "DISPATCH_USE_LOCAL_GRPO_LINES_FOR_TESTING", False):
            logger.info(
                "Skipping SAP OPCH duplicate lookup for transporter invoice %s in local GRPO test mode.",
                invoice_number,
            )
            return False

        schema = self.connection.schema
        query = f"""
            SELECT H."DocEntry"
            FROM "{schema}"."OPCH" H
            WHERE H."CardCode" = ?
              AND IFNULL(TO_NVARCHAR(H."NumAtCard"), '') = ?
              AND IFNULL(H."CANCELED", 'N') = 'N'
            LIMIT 1
        """
        rows = self._execute(query, [vendor_code, invoice_number])
        return bool(rows)

    def _fetch_sap_grpo_lines(
        self,
        doc_entries: Iterable[int],
    ) -> Dict[int, List[Dict[str, Any]]]:
        doc_entries = [entry for entry in dict.fromkeys(doc_entries) if entry]
        if not doc_entries:
            return {}
        if getattr(settings, "DISPATCH_USE_LOCAL_GRPO_LINES_FOR_TESTING", False):
            return self._fetch_local_grpo_test_lines(doc_entries)

        schema = self.connection.schema
        opdn_columns = self._table_columns("OPDN")
        pdn1_columns = self._table_columns("PDN1")

        branch_expr = self._header_raw(opdn_columns, "BPLId", "branch_id", "NULL")
        card_name_expr = self._header_string(opdn_columns, "CardName", "card_name")
        description_expr = self._line_string(
            pdn1_columns, "Dscription", "description"
        )
        gl_account_expr = self._line_string(pdn1_columns, "AcctCode", "gl_account")
        tax_column = "TaxCode" if "TaxCode" in pdn1_columns else "VatGroup"
        tax_expr = self._line_string(pdn1_columns, tax_column, "tax_code")

        placeholders = ", ".join(["?"] * len(doc_entries))
        group_by_fields = [
            'H."DocEntry"',
            'H."DocNum"',
            'H."DocType"',
            'H."CANCELED"',
            'H."CardCode"',
            'H."DocTotal"',
            'L."LineNum"',
            'L."LineTotal"',
        ]
        if "BPLId" in opdn_columns:
            group_by_fields.append('H."BPLId"')
        if "CardName" in opdn_columns:
            group_by_fields.append('H."CardName"')
        if "Dscription" in pdn1_columns:
            group_by_fields.append('L."Dscription"')
        if "AcctCode" in pdn1_columns:
            group_by_fields.append('L."AcctCode"')
        if tax_column in pdn1_columns:
            group_by_fields.append(f'L."{tax_column}"')

        query = f"""
            SELECT
                H."DocEntry" AS doc_entry,
                H."DocNum" AS doc_num,
                IFNULL(H."DocType", '') AS doc_type,
                IFNULL(H."CANCELED", 'N') AS canceled,
                {branch_expr},
                IFNULL(H."CardCode", '') AS card_code,
                {card_name_expr},
                IFNULL(H."DocTotal", 0) AS doc_total,
                L."LineNum" AS line_num,
                {description_expr},
                IFNULL(L."LineTotal", 0) AS line_total,
                {gl_account_expr},
                {tax_expr},
                MAX(
                    CASE
                        WHEN IH."DocEntry" IS NOT NULL
                         AND IFNULL(IH."CANCELED", 'N') = 'N'
                        THEN 1
                        ELSE 0
                    END
                ) AS already_invoiced
            FROM "{schema}"."OPDN" H
            INNER JOIN "{schema}"."PDN1" L
                ON L."DocEntry" = H."DocEntry"
            LEFT JOIN "{schema}"."PCH1" IL
                ON IL."BaseType" = 20
               AND IL."BaseEntry" = L."DocEntry"
               AND IL."BaseLine" = L."LineNum"
            LEFT JOIN "{schema}"."OPCH" IH
                ON IH."DocEntry" = IL."DocEntry"
            WHERE H."DocEntry" IN ({placeholders})
            GROUP BY
                {", ".join(group_by_fields)}
            ORDER BY H."DocEntry", L."LineNum"
        """
        rows = self._execute(query, doc_entries)

        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            mapped = self._map_sap_grpo_line(row)
            grouped.setdefault(mapped["doc_entry"], []).append(mapped)
        return grouped

    def _fetch_local_grpo_test_lines(
        self,
        doc_entries: Iterable[int],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Build SAP-like GRPO rows from local service GRPO records for dev UI testing."""
        postings = (
            ServiceGRPOPosting.objects.filter(
                dispatch_plan__company=self.company,
                sap_doc_entry__in=list(doc_entries),
                status=GRPOStatus.POSTED,
            )
            .select_related("dispatch_plan")
            .prefetch_related("lines")
        )
        default_branch_id = getattr(settings, "DISPATCH_LOCAL_GRPO_TEST_BRANCH_ID", 2)
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for posting in postings:
            local_lines = list(posting.lines.all().order_by("id"))
            if not local_lines:
                local_lines = [None]
            rows = []
            for index, line in enumerate(local_lines):
                line_total = self._decimal(
                    getattr(line, "amount", None) or posting.sap_doc_total or Decimal("0.00")
                )
                rows.append(
                    {
                        "doc_entry": int(posting.sap_doc_entry),
                        "doc_num": posting.sap_doc_num,
                        "doc_type": "S",
                        "canceled": "N",
                        "branch_id": int(default_branch_id) if default_branch_id else None,
                        "card_code": posting.vendor_code,
                        "card_name": posting.vendor_name,
                        "doc_total": self._decimal(posting.sap_doc_total or line_total),
                        "line_num": index,
                        "description": getattr(line, "service_description", "") or "Transport freight",
                        "line_total": line_total,
                        "gl_account": getattr(line, "gl_account", "") or "",
                        "tax_code": getattr(line, "tax_code", "") or "",
                        "already_invoiced": False,
                    }
                )
            grouped[int(posting.sap_doc_entry)] = rows
        return grouped

    def _table_columns(self, table_name: str) -> set:
        rows = self._execute(
            """
                SELECT "COLUMN_NAME"
                FROM "SYS"."TABLE_COLUMNS"
                WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = ?
            """,
            [self.connection.schema, table_name.upper()],
        )
        return {row[0] for row in rows}

    @staticmethod
    def _header_raw(columns: set, column: str, alias: str, fallback: str) -> str:
        if column not in columns:
            return f"{fallback} AS {alias}"
        return f'H."{column}" AS {alias}'

    @staticmethod
    def _header_string(columns: set, column: str, alias: str) -> str:
        if column not in columns:
            return f"'' AS {alias}"
        return f'IFNULL(TO_NVARCHAR(H."{column}"), \'\') AS {alias}'

    @staticmethod
    def _line_string(columns: set, column: str, alias: str) -> str:
        if column not in columns:
            return f"'' AS {alias}"
        return f'IFNULL(TO_NVARCHAR(L."{column}"), \'\') AS {alias}'

    @staticmethod
    def _map_sap_grpo_line(row: Sequence[Any]) -> Dict[str, Any]:
        return {
            "doc_entry": int(row[0]),
            "doc_num": int(row[1]) if row[1] is not None else None,
            "doc_type": row[2] or "",
            "canceled": row[3] or "N",
            "branch_id": int(row[4]) if row[4] is not None else None,
            "card_code": row[5] or "",
            "card_name": row[6] or "",
            "doc_total": DispatchInvoiceService._decimal(row[7]),
            "line_num": int(row[8]),
            "description": row[9] or "",
            "line_total": DispatchInvoiceService._decimal(row[10]),
            "gl_account": row[11] or "",
            "tax_code": row[12] or "",
            "already_invoiced": bool(row[13]),
        }

    def _execute(self, query: str, params: List[Any]) -> List:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.error("SAP HANA connection failed for dispatch invoices: %s", exc)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.ProgrammingError as exc:
            logger.error("SAP HANA dispatch invoice query error: %s", exc)
            raise SAPDataError(
                "Failed to retrieve transporter invoice data from SAP. Invalid query."
            ) from exc
        except dbapi.Error as exc:
            logger.error("SAP HANA dispatch invoice data error: %s", exc)
            raise SAPDataError(
                "Failed to retrieve transporter invoice data from SAP."
            ) from exc
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _decimal(value) -> Decimal:
        if value is None or value == "":
            return Decimal("0.00")
        return Decimal(str(value)).quantize(Decimal("0.01"))
