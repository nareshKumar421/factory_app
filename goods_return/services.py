"""Company-scoped orchestration for the Goods Return module.

Business errors raise ``ValueError`` (views translate to HTTP 400); cross-company
access violations raise DRF ``PermissionDenied`` (403). Invoice details are read on
demand from SAP via ``DispatchPlansService`` -- never stored beyond the doc-entry
reference and a per-line snapshot needed for display / over-return validation.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from vehicle_management.models import Vehicle

from .models import (
    GoodsReturn,
    GoodsReturnApprovalStatus,
    GoodsReturnAttachment,
    GoodsReturnBasis,
    GoodsReturnInvoiceRef,
    GoodsReturnItem,
    GoodsReturnStatus,
)

logger = logging.getLogger(__name__)

# A return is put in front of the gate as soon as its first page is saved, so it
# spends most of its filling-in life in AWAITING_ARRIVAL -- and the truck can pull
# up while the clerk is still on the items page, which is why ARRIVED is editable
# too. Everything from receipt onwards is closed: RECEIVED / POSTED have stock
# (and possibly a SAP document) behind them, and CANCELLED is over.
EDITABLE_STATUSES = (
    GoodsReturnStatus.DRAFT,
    GoodsReturnStatus.AWAITING_ARRIVAL,
    GoodsReturnStatus.ARRIVED,
)

# The clerk's own "I am done filling this in" stamp. It does not gate the gate --
# by this point the truck may already be in.
SUBMITTABLE_STATUSES = (
    GoodsReturnStatus.DRAFT,
    GoodsReturnStatus.AWAITING_ARRIVAL,
    GoodsReturnStatus.ARRIVED,
)

# Receiving posts the SAP documents. PARTIALLY_POSTED is receivable again on
# purpose: a run where SAP took one invoice's return and refused another's leaves
# the refused ones to post, and receiving again picks up exactly those.
RECEIVABLE_STATUSES = (
    GoodsReturnStatus.ARRIVED,
    GoodsReturnStatus.PARTIALLY_POSTED,
)


def _generate_vehicle_entry_no() -> str:
    today = timezone.now()
    prefix = f"GRV-{today.strftime('%Y%m%d')}"
    last = (
        VehicleEntry.objects.filter(entry_no__startswith=prefix)
        .order_by("-entry_no")
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(last.entry_no.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    return f"{prefix}-{seq:04d}"


class GoodsReturnService:
    """Orchestration bound to the active-header company for create/invoice lookup.

    Edit/read methods resolve the company from the *record* and enforce that it is
    one of the caller's companies (``allowed_company_ids``) -- the cross-company
    security boundary.
    """

    def __init__(self, company: Company | None = None):
        self.company = company

    # -- helpers ---------------------------------------------------------------

    def _dispatch_service(self, company: Company):
        # Imported lazily: dispatch_plans pulls in the SAP/HANA reader stack.
        from dispatch_plans.services import DispatchPlansService

        return DispatchPlansService(company.code)

    def _lookup_bill(self, company: Company, invoice_number: str) -> dict:
        try:
            bill = self._dispatch_service(company).get_bill_by_number(invoice_number)
        except Exception as exc:  # SAP/HANA read failures surface as a clean 400
            logger.warning("Goods return invoice lookup failed for %s: %s", invoice_number, exc)
            raise ValueError(f"Could not look up invoice {invoice_number}.")
        if not bill:
            raise ValueError(f"No SAP invoice found for {invoice_number}.")
        return bill

    def _get_scoped(self, pk, allowed_company_ids) -> GoodsReturn:
        gr = (
            GoodsReturn.objects.filter(pk=pk, is_active=True)
            .select_related("company", "vehicle", "driver", "vehicle_entry")
            .prefetch_related("invoice_refs", "lines", "attachments")
            .first()
        )
        if gr is None:
            raise ValueError("Goods return not found.")
        if gr.company_id not in allowed_company_ids:
            raise PermissionDenied("This record belongs to a company you cannot access.")
        return gr

    def _assert_editable(self, gr: GoodsReturn):
        if gr.status not in EDITABLE_STATUSES:
            raise ValueError(f"A {gr.get_status_display()} return can no longer be edited.")

    @staticmethod
    def _resolve_vehicle(vehicle_id) -> Vehicle:
        vehicle = Vehicle.objects.filter(pk=vehicle_id).first()
        if vehicle is None:
            raise ValueError("Selected vehicle not found.")
        return vehicle

    @staticmethod
    def _resolve_driver(driver_id) -> Driver:
        driver = Driver.objects.filter(pk=driver_id).first()
        if driver is None:
            raise ValueError("Selected driver not found.")
        return driver

    # -- reads -----------------------------------------------------------------

    def list_returns(self, company_ids, *, status=None, basis=None, search=None, approval=None):
        qs = (
            GoodsReturn.objects.filter(is_active=True, company_id__in=company_ids)
            .select_related("company", "vehicle", "driver")
            .prefetch_related("lines", "invoice_refs")
        )
        if status:
            qs = qs.filter(status=status)
        if basis:
            qs = qs.filter(basis=basis)
        if approval == "PENDING":
            qs = qs.filter(
                requires_approval=True, approval_status=GoodsReturnApprovalStatus.PENDING
            )
        elif approval:
            qs = qs.filter(approval_status=approval)
        if search:
            # The invoice number is searchable because it is the thing people have
            # in front of them: a customer rings about bill 1500, not about a GR
            # number. `distinct` because that match joins the invoice rows.
            qs = qs.filter(
                Q(entry_no__icontains=search)
                | Q(customer_name__icontains=search)
                | Q(customer_ref_no__icontains=search)
                | Q(invoice_refs__sap_invoice_doc_num__icontains=search)
            ).distinct()
        return qs

    def get_return(self, pk, allowed_company_ids) -> GoodsReturn:
        return self._get_scoped(pk, allowed_company_ids)

    def get_invoice_preview(self, gr: GoodsReturn) -> list[dict]:
        """Live invoice header/lines for the return's referenced bills (on demand)."""
        previews = []
        for ref in gr.active_invoice_refs:
            try:
                bill = self._dispatch_service(gr.company).get_bill_by_number(
                    ref.sap_invoice_doc_num or str(ref.sap_invoice_doc_entry)
                )
            except Exception:
                bill = None
            if bill:
                previews.append(
                    {
                        "invoice_ref_id": ref.id,
                        "doc_entry": bill.get("doc_entry"),
                        "doc_num": bill.get("doc_num"),
                        "card_code": bill.get("card_code"),
                        "card_name": bill.get("card_name"),
                        "items": bill.get("items", []),
                    }
                )
        return previews

    # -- create ----------------------------------------------------------------

    @transaction.atomic
    def create_return(self, data, user) -> GoodsReturn:
        """Save the return's first page -- and hand it to the gate straight away.

        The vehicle and driver are the first thing the clerk fills in, and the
        return is born AWAITING_ARRIVAL rather than DRAFT: the truck is usually
        already on its way while the items are still being keyed in, and the gate
        cannot mark in what it cannot see. The rest of the booking (items, review)
        carries on against a return that is already in the gate's queue.
        """
        if self.company is None:
            raise ValueError("A company context is required to create a return.")

        basis = data["basis"]
        requires_approval = bool(data.get("requires_approval"))
        if not data.get("vehicle_id") or not data.get("driver_id"):
            raise ValueError("Pick the vehicle and driver bringing the goods back.")
        vehicle = self._resolve_vehicle(data["vehicle_id"])
        driver = self._resolve_driver(data["driver_id"])
        gr = GoodsReturn(
            company=self.company,
            entry_no=GoodsReturn.generate_entry_no(),
            basis=basis,
            status=GoodsReturnStatus.AWAITING_ARRIVAL,
            customer_code=(data.get("customer_code") or "").strip(),
            customer_name=(data.get("customer_name") or "").strip(),
            customer_ref_no=(data.get("customer_ref_no") or "").strip(),
            vehicle=vehicle,
            driver=driver,
            expected_arrival_at=data.get("expected_arrival_at"),
            remarks=(data.get("remarks") or "").strip(),
            requires_approval=requires_approval,
            approval_status=(
                GoodsReturnApprovalStatus.PENDING
                if requires_approval
                else GoodsReturnApprovalStatus.NOT_REQUIRED
            ),
            created_by=user,
        )

        if basis == GoodsReturnBasis.INVOICE:
            invoice_numbers = data.get("invoice_numbers") or []
            if not invoice_numbers:
                raise ValueError("Select at least one invoice for an invoice-based return.")
            gr.save()
            for number in invoice_numbers:
                self._attach_invoice(gr, str(number).strip())
        else:
            if not gr.customer_name:
                raise ValueError("Enter the customer name.")
            # The SAP business-partner code, not just the name. Everything the
            # return does later is keyed on it: the returning-items picker reads
            # this customer's invoice history, and the posted A/R Return carries
            # it as CardCode. A return booked with a name alone silently offers
            # an empty item list and can never post, so it is asked for here --
            # picked from SAP on the form -- rather than discovered at step 2.
            if not gr.customer_code:
                raise ValueError("Pick the customer from SAP — a name alone is not enough.")
            gr.save()

        return gr

    def _attach_invoice(self, gr: GoodsReturn, invoice_number: str) -> GoodsReturnInvoiceRef:
        bill = self._lookup_bill(gr.company, invoice_number)
        doc_entry = bill["doc_entry"]

        if gr.invoice_refs.filter(sap_invoice_doc_entry=doc_entry, is_active=True).exists():
            raise ValueError(f"Invoice {bill.get('doc_num') or invoice_number} is already added.")

        card_code = (bill.get("card_code") or "").strip()
        card_name = (bill.get("card_name") or "").strip()
        # A return is for one customer; block mixing bills across customers.
        if gr.customer_code and card_code and card_code != gr.customer_code:
            raise ValueError("All invoices on a return must be for the same customer.")
        if not gr.customer_code:
            gr.customer_code = card_code
            gr.customer_name = card_name
            gr.save(update_fields=["customer_code", "customer_name", "updated_at"])

        ref = GoodsReturnInvoiceRef.objects.create(
            goods_return=gr,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=bill.get("doc_num") or "",
        )
        # Snapshot invoice lines as candidate return items (return_quantity=0 until
        # the operator fills Step 2).
        for line in bill.get("items", []):
            GoodsReturnItem.objects.create(
                goods_return=gr,
                invoice_ref=ref,
                source_line_num=line.get("line_num"),
                item_code=line.get("item_code") or "",
                item_name=line.get("item_name") or "",
                uom=line.get("uom") or "",
                invoice_quantity=line.get("quantity") or 0,
                unit_price=line.get("rate") or 0,
                tax_code=line.get("tax_code") or "",
                return_quantity=0,
            )
        return ref

    # -- header / invoice-ref edits -------------------------------------------

    def update_header(self, pk, data, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)
        # The customer code can be corrected but never cleared: the item picker
        # and the posted A/R Return both key on it, and a blank one takes the
        # return back to offering nothing to return (see ``create``).
        if "customer_code" in data and gr.customer_code:
            if not (data.get("customer_code") or "").strip():
                raise ValueError("A return needs its SAP customer — pick one, don't clear it.")
        for field in ("customer_code", "customer_name", "customer_ref_no", "remarks"):
            if field in data:
                setattr(gr, field, (data.get(field) or "").strip())
        if "requires_approval" in data and gr.approval_status in (
            GoodsReturnApprovalStatus.NOT_REQUIRED,
            GoodsReturnApprovalStatus.PENDING,
        ):
            requires = bool(data.get("requires_approval"))
            gr.requires_approval = requires
            gr.approval_status = (
                GoodsReturnApprovalStatus.PENDING
                if requires
                else GoodsReturnApprovalStatus.NOT_REQUIRED
            )
        gr.updated_by = user
        gr.save()
        return gr

    def add_invoice_ref(self, pk, invoice_number, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)
        if gr.basis != GoodsReturnBasis.INVOICE:
            raise ValueError("Invoices can only be added to an invoice-based return.")
        with transaction.atomic():
            self._attach_invoice(gr, str(invoice_number).strip())
            gr.updated_by = user
            gr.save(update_fields=["updated_by", "updated_at"])
        return gr

    def remove_invoice_ref(self, pk, ref_id, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)
        ref = gr.invoice_refs.filter(pk=ref_id, is_active=True).first()
        if ref is None:
            raise ValueError("Invoice reference not found.")
        with transaction.atomic():
            # Hard-remove the ref and its snapshotted lines (draft only).
            gr.lines.filter(invoice_ref=ref).delete()
            ref.delete()
            gr.updated_by = user
            gr.save(update_fields=["updated_by", "updated_at"])
        return gr

    # -- items (Step 2) --------------------------------------------------------

    @transaction.atomic
    def save_items(self, pk, lines, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)

        ref_by_id = {ref.id: ref for ref in gr.active_invoice_refs}
        cleaned = []
        for raw in lines:
            qty = raw.get("return_quantity") or 0
            if qty <= 0:
                continue  # a line not being returned
            invoice_qty = raw.get("invoice_quantity") or 0
            if invoice_qty and qty > invoice_qty:
                raise ValueError(
                    f"Return quantity ({qty}) cannot exceed invoice quantity ({invoice_qty}) "
                    f"for {raw.get('item_code') or 'an item'}."
                )
            ref_id = raw.get("invoice_ref_id")
            if ref_id is not None and ref_id not in ref_by_id:
                raise ValueError("Unknown invoice reference on a return line.")
            cleaned.append((raw, ref_id))

        if not cleaned:
            raise ValueError("Add at least one item with a return quantity.")

        # Preserve the invoice-line price/tax snapshot across the replace-set (the
        # editable grid doesn't carry them), keyed by (invoice_ref, source_line_num).
        price_tax_by_key = {
            (line.invoice_ref_id, line.source_line_num): (line.unit_price, line.tax_code)
            for line in gr.lines.all()
        }

        gr.lines.all().delete()
        for raw, ref_id in cleaned:
            unit_price, tax_code = price_tax_by_key.get(
                (ref_id, raw.get("source_line_num")), (0, "")
            )
            GoodsReturnItem.objects.create(
                goods_return=gr,
                invoice_ref=ref_by_id.get(ref_id) if ref_id else None,
                source_line_num=raw.get("source_line_num"),
                item_code=(raw.get("item_code") or "").strip(),
                item_name=(raw.get("item_name") or "").strip(),
                uom=(raw.get("uom") or "").strip(),
                invoice_quantity=raw.get("invoice_quantity") or 0,
                unit_price=unit_price,
                tax_code=tax_code,
                return_quantity=raw.get("return_quantity"),
                reason=(raw.get("reason") or "").strip(),
                condition=raw.get("condition") or "DAMAGED",
                remarks=(raw.get("remarks") or "").strip(),
            )
        gr.updated_by = user
        gr.save(update_fields=["updated_by", "updated_at"])
        return gr

    # -- vehicle (corrections after creation) ----------------------------------

    def set_vehicle(self, pk, data, user, allowed_company_ids) -> GoodsReturn:
        """Correct the truck on a return that is already in the gate's queue.

        The vehicle and driver are captured when the return is created, so this
        only ever changes them -- it cannot blank them, because the gate is
        already waiting on this arrival. A key left out is not touched;
        ``expected_arrival_at: null`` still clears the date.
        """
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)
        if gr.vehicle_entry_id:
            raise ValueError("The vehicle is already marked in at the gate.")

        update_fields = []

        if "vehicle_id" in data:
            if data["vehicle_id"] is None:
                raise ValueError("Pick the vehicle bringing the goods back.")
            gr.vehicle = self._resolve_vehicle(data["vehicle_id"])
            update_fields.append("vehicle")

        if "driver_id" in data:
            if data["driver_id"] is None:
                raise ValueError("Pick the driver bringing the goods back.")
            gr.driver = self._resolve_driver(data["driver_id"])
            update_fields.append("driver")

        if "expected_arrival_at" in data:
            gr.expected_arrival_at = data["expected_arrival_at"]
            update_fields.append("expected_arrival_at")

        gr.updated_by = user
        gr.save(update_fields=[*update_fields, "updated_by", "updated_at"])
        return gr

    # -- submit ----------------------------------------------------------------

    @transaction.atomic
    def submit(self, pk, user, allowed_company_ids) -> GoodsReturn:
        """The clerk finishes the booking.

        The gate has been able to see this return since its first page was saved,
        so submitting no longer decides whether the truck can come in -- it
        records that the paperwork is complete, and only moves the status for the
        legacy drafts that were created before that changed. A return whose truck
        is already inside stays ARRIVED.
        """
        gr = self._get_scoped(pk, allowed_company_ids)
        if gr.status not in SUBMITTABLE_STATUSES:
            raise ValueError(f"A {gr.get_status_display()} return can no longer be submitted.")
        if not gr.active_lines:
            raise ValueError("Add at least one returning item before submitting.")
        if not gr.attachments.exists():
            raise ValueError("Attach at least one supporting document before submitting.")

        update_fields = ["submitted_by", "submitted_at", "updated_by", "updated_at"]
        if gr.status == GoodsReturnStatus.DRAFT:
            gr.status = GoodsReturnStatus.AWAITING_ARRIVAL
            update_fields.append("status")
        gr.submitted_by = user
        gr.submitted_at = timezone.now()
        gr.updated_by = user
        gr.save(update_fields=update_fields)
        return gr

    # -- receive + SAP A/R Returns posting ------------------------------------

    def returnable_items(self, pk, allowed_company_ids, *, search="", limit=100):
        """The finished goods that can go on a return line.

        The whole FG range, independent of the customer. Goods come back for
        reasons that have nothing to do with who was billed for them -- a
        replacement sent on a letter pad, stock moved between distributors, a
        debit note against a shipment invoiced to somebody else -- and a picker
        that offered only this customer's purchase history refused all of them.

        The customer code is still passed down, but only to annotate the rows
        it recognises with the last price, tax code and invoice, and to float
        them to the top as the likeliest returns. A return with no customer on
        it yet still gets the full list.
        """
        from sap_client.client import SAPClient

        gr = self._get_scoped(pk, allowed_company_ids)
        client = SAPClient(company_code=gr.company.code)
        return client.return_item_options(
            gr.customer_code, search=search or "", limit=limit
        )

    def search_customers(self, search="", limit=50):
        """SAP customers for the header picker on a debit-note / letter-pad return.

        An invoice-basis return gets its customer from the invoice; these two
        bases have nothing to read it off, so the operator picks it. From SAP
        rather than typed, because a code that is merely plausible looks
        identical on this screen and then returns an empty item list at step 2.
        """
        from sap_client.client import SAPClient

        if self.company is None:
            raise ValueError("A company context is required to search customers.")
        client = SAPClient(company_code=self.company.code)
        return client.search_customers(search=(search or "").strip() or None, limit=limit)

    def list_return_warehouses(self, company_code):
        """Goods-return warehouses (from SAP) the creator picks at receipt."""
        from sap_client.context import CompanyContext
        from sap_client.hana.warehouse_reader import HanaWarehouseReader

        reader = HanaWarehouseReader(CompanyContext(company_code))
        return reader.get_return_warehouses()

    @transaction.atomic
    def receive(self, pk, user, warehouse_code, allowed_company_ids) -> GoodsReturn:
        """The GR creator confirms the goods physically arrived (after gate-in).

        Posts **one standalone SAP A/R Return per source invoice** — a return
        booked against two invoices lands two documents, not one combined (see
        ``_post_sap_returns`` for why). Each has to be standalone: SAP refuses a
        return based on an invoice outright ("'13' is not a valid value for
        property 'BaseType'"), and 94% of invoices have no delivery behind them
        to base on either.

        Because they are standalone, the app must supply what a copied line would
        have inherited: the Variety, the tax code and the return cost. All three
        are read from SAP; none is defaulted. Company resolved from the record.

        Receiving again is a retry, not a second posting: the invoices SAP already
        accepted are skipped and only the ones it refused are attempted. Those
        refusals come back on the returned record rather than as an exception,
        because raising would roll back the documents SAP *did* accept — and a
        posted return cannot be withdrawn.
        """
        gr = (
            GoodsReturn.objects.select_for_update(of=("self",))
            .select_related("company")
            .prefetch_related("invoice_refs", "lines")
            .filter(pk=pk, is_active=True)
            .first()
        )
        if gr is None:
            raise ValueError("Goods return not found.")
        if gr.company_id not in allowed_company_ids:
            raise PermissionDenied("This record belongs to a company you cannot access.")
        if gr.status not in RECEIVABLE_STATUSES:
            raise ValueError("Only a gated-in (arrived) return can be received.")
        if gr.requires_approval and gr.approval_status != GoodsReturnApprovalStatus.APPROVED:
            if gr.approval_status == GoodsReturnApprovalStatus.REJECTED:
                raise ValueError("This return's approval was rejected; it cannot be received.")
            raise ValueError("This return is awaiting admin approval before it can be received.")

        lines = gr.active_lines
        if not lines:
            raise ValueError("This return has no items to receive.")

        warehouse_code = (warehouse_code or "").strip()
        if not warehouse_code:
            raise ValueError("Select the goods-return warehouse.")
        # A retry keeps the warehouse the first run used: the stock already in SAP
        # went there, and one return split across two warehouses would leave nobody
        # able to say where the goods are.
        if gr.sap_return_warehouse and gr.sap_return_warehouse != warehouse_code:
            raise ValueError(
                f"This return has already posted into {gr.sap_return_warehouse}, so "
                f"the invoices still to post must go into the same warehouse."
            )

        posted, failures = self._post_sap_returns(gr, lines, warehouse_code, user)
        if not posted:
            # Nothing reached SAP, so there is nothing to preserve: raise and let
            # the transaction roll back, which is what a single-document return
            # has always done.
            raise ValueError(self._posting_failure_message(failures))

        gr.sap_return_warehouse = warehouse_code
        gr.status = (
            GoodsReturnStatus.PARTIALLY_POSTED if failures else GoodsReturnStatus.POSTED
        )
        gr.received_by = gr.received_by or user
        gr.received_at = gr.received_at or timezone.now()
        gr.updated_by = user
        gr.save(
            update_fields=[
                "status",
                "received_by",
                "received_at",
                "sap_gr_doc_entry",
                "sap_gr_doc_num",
                "sap_return_warehouse",
                "updated_by",
                "updated_at",
            ]
        )
        # Read by the view, so a half-posted run is reported as the failure it is
        # while the documents SAP accepted stay recorded.
        gr.posting_failures = failures
        return gr

    @staticmethod
    def _posting_failure_message(failures) -> str:
        """The refusals as one line, each named by the invoice it belongs to."""
        if not failures:
            return "SAP posted nothing for this return."
        return "; ".join(
            f"invoice {label}: {error}" if label else str(error)
            for label, error in failures
        )

    def _group_lines_by_invoice(self, gr: GoodsReturn, lines) -> list:
        """The return's lines split into the documents they will be posted as.

        One group per source invoice, in the order the invoices were added. Lines
        with no invoice behind them — every line of a debit-note or letter-pad
        return, and any item keyed in by hand — cannot be attributed to one, so
        they ride on the first document instead of becoming a document of their
        own: they belong to the return, and a second return note against no
        invoice at all is not something the customer can be shown.
        """
        by_ref: dict = {}
        unattributed = []
        for line in lines:
            if line.invoice_ref_id:
                by_ref.setdefault(line.invoice_ref_id, []).append(line)
            else:
                unattributed.append(line)

        groups = [
            (ref, by_ref[ref.id])
            for ref in gr.active_invoice_refs
            if by_ref.get(ref.id)
        ]
        if unattributed:
            if groups:
                groups[0][1].extend(unattributed)
            else:
                groups.append((None, unattributed))
        return groups

    def _post_sap_returns(self, gr: GoodsReturn, lines, warehouse_code, user):
        """One standalone A/R Return per source invoice, not one combined document.

        A SAP Return is the counterpart of a sale, and two sales cannot share one.
        The credit note that follows is raised against the invoice; the place of
        supply and the tax flavour that follows from it are the invoice's own (a
        customer with depots in two states is billed to two, and one document can
        only carry one); and SAP refuses duplicate item lines outright (160020),
        so an item that came back off both invoices has nowhere to sit on a
        combined return but a merged quantity matching neither bill.

        Everything SAP is *asked* is done first, for every document, before
        anything is *written*: a return that fails a guard on its second invoice
        has to fail before the first one is in SAP, because SAP will not let the
        app cancel a return it posted (160002/160010, and a live `Cancel` came
        back `-1116`). Only a refusal by SAP itself can leave a run half-done, and
        the documents it accepted are then kept rather than rolled back.

        Returns `(posted, failures)`, `failures` being `[(invoice label, error)]`.
        """
        from sap_client.client import SAPClient
        from sap_client.context import CompanyContext
        from sap_client.service_layer.returns_writer import ReturnsWriter

        from . import guards

        client = SAPClient(company_code=gr.company.code)

        guards.check_posting_date(timezone.localdate())
        guards.check_customer(gr.customer_code, client.customer_group_code(gr.customer_code))
        branch_id = client.warehouse_branch_id(warehouse_code)
        guards.check_warehouse(warehouse_code, branch_id)

        # Read once for the whole return rather than per document: none of these
        # answers varies by invoice, and a return carrying four bills would
        # otherwise make four round-trips for the same ones.
        item_codes = [line.item_code for line in lines]
        variety_codes = client.return_variety_codes(item_codes)
        return_costs = client.return_costs(item_codes, warehouse_code)
        branch_state = client.branch_state(branch_id)
        ar_tax_codes = None

        prepared = []
        for ref, group in self._group_lines_by_invoice(gr, lines):
            if ref is not None and ref.is_posted:
                continue  # SAP already has this invoice's return; never post twice

            # An invoice-basis line already snapshotted the tax code it was billed
            # under; only ask SAP for the ones we do not have.
            tax_codes = {line.item_code: line.tax_code for line in group if line.tax_code}
            unknown = [line.item_code for line in group if not line.tax_code]
            if unknown:
                tax_codes.update(client.return_tax_codes(gr.customer_code, unknown))

            # The place of supply is this invoice's own, not the return's: it
            # decides the tax flavour, and SAP refuses the whole document when the
            # flavour is wrong (254000293).
            addresses = self._place_of_supply(gr, client, ref)
            interstate = guards.is_interstate(branch_state, addresses.get("ship_state", ""))
            if interstate is not None:
                if ar_tax_codes is None:
                    ar_tax_codes = client.ar_tax_codes()
                tax_codes = {
                    item: guards.align_tax_code(
                        code, interstate=interstate, available=ar_tax_codes, item_code=item
                    )
                    for item, code in tax_codes.items()
                }

            guards.check_lines(
                [
                    {"item_code": line.item_code, "quantity": line.return_quantity}
                    for line in group
                ],
                variety_codes=variety_codes,
                tax_codes=tax_codes,
                return_costs=return_costs,
            )

            prepared.append(
                (
                    ref,
                    self._sap_payload(
                        gr,
                        ref,
                        group,
                        warehouse_code,
                        branch_id,
                        addresses,
                        variety_codes=variety_codes,
                        tax_codes=tax_codes,
                        return_costs=return_costs,
                    ),
                )
            )

        if not prepared:
            raise ValueError(
                "Every invoice on this return is already posted to SAP."
                if gr.active_invoice_refs
                else "This return has no items to post."
            )

        writer = ReturnsWriter(CompanyContext(gr.company.code))
        posted, failures = [], []
        for ref, payload in prepared:
            label = ""
            if ref is not None:
                label = ref.sap_invoice_doc_num or str(ref.sap_invoice_doc_entry)
            # Asked before every post, not only after a crash: the reference is
            # unique to (return, invoice), so a document already carrying it *is*
            # this one, and a second copy of a return nobody can cancel is the one
            # mistake worth a round-trip to avoid.
            existing = client.find_goods_return_by_reference(
                gr.customer_code, payload["NumAtCard"]
            )
            if existing:
                logger.warning(
                    "A/R Return %s already exists in SAP for %s (%s); not posting again.",
                    existing.get("doc_num"),
                    gr.entry_no,
                    payload["NumAtCard"],
                )
                result = {"DocEntry": existing["doc_entry"], "DocNum": existing["doc_num"]}
            else:
                try:
                    result = writer.create(payload)
                except Exception as exc:
                    logger.error(
                        "SAP A/R Returns post failed for %s (invoice %s): %s",
                        gr.entry_no,
                        label or "-",
                        exc,
                    )
                    failures.append((label, f"SAP rejected the return: {exc}"))
                    if ref is not None:
                        self._record_posting_error(ref, user, exc)
                    continue

            self._record_posted(gr, ref, result, warehouse_code, user)
            posted.append(ref)

        return posted, failures

    @staticmethod
    def _record_posted(gr: GoodsReturn, ref, result, warehouse_code, user) -> None:
        doc_entry = result.get("DocEntry")
        doc_num = str(result.get("DocNum") or "")
        if ref is not None:
            ref.sap_gr_doc_entry = doc_entry
            ref.sap_gr_doc_num = doc_num
            ref.sap_return_warehouse = warehouse_code
            ref.posted_at = timezone.now()
            ref.sap_post_error = ""
            ref.updated_by = user
            ref.save(
                update_fields=[
                    "sap_gr_doc_entry",
                    "sap_gr_doc_num",
                    "sap_return_warehouse",
                    "posted_at",
                    "sap_post_error",
                    "updated_by",
                    "updated_at",
                ]
            )
        # The header keeps the first document of the set as its own handle -- what
        # a single-invoice return has always meant, and what the returns booked
        # before the split still carry.
        if gr.sap_gr_doc_entry is None:
            gr.sap_gr_doc_entry = doc_entry
            gr.sap_gr_doc_num = doc_num

    @staticmethod
    def _record_posting_error(ref, user, error) -> None:
        ref.sap_post_error = str(error)[:2000]
        ref.updated_by = user
        ref.save(update_fields=["sap_post_error", "updated_by", "updated_at"])

    def _sap_payload(
        self,
        gr: GoodsReturn,
        ref,
        lines,
        warehouse_code,
        branch_id,
        addresses,
        *,
        variety_codes,
        tax_codes,
        return_costs,
    ) -> dict:
        """One document's payload, with everything SAP demands supplied.

        The payload shape was established by posting into the sandbox rather than
        read from documentation; see `guards.py` for the rules behind each field.
        Note `BPL_IDAssignedToInvoice` — a marketing document spells the branch
        differently from a stock transfer, and omitting it fails with -5002.
        """
        from . import guards

        payload = {
            "CardCode": gr.customer_code,
            "BPL_IDAssignedToInvoice": branch_id,
            "NumAtCard": guards.check_reference(guards.reference_for(gr.entry_no, gr.basis, ref)),
            "Comments": self._sap_comment(gr, ref),
            "DocumentLines": [
                self._sap_line(
                    gr, line, warehouse_code,
                    variety=variety_codes[line.item_code],
                    tax_code=tax_codes[line.item_code],
                    return_cost=return_costs[line.item_code],
                )
                for line in lines
            ],
        }
        # Without these SAP resolves the place of supply from the customer's
        # *default* address, which for a multi-state distributor is rarely the
        # one that was sold to -- and the mismatch is fatal (254000293).
        if addresses.get("ship_to_code"):
            payload["ShipToCode"] = addresses["ship_to_code"]
        if addresses.get("pay_to_code") or addresses.get("ship_to_code"):
            payload["PayToCode"] = (
                addresses.get("pay_to_code") or addresses["ship_to_code"]
            )
        return payload

    # -- the printed Return Note ----------------------------------------------

    def print_payload(self, pk, allowed_company_ids, *, doc_entry=None) -> dict:
        """SAP's own Return sheet for one posted document, as data.

        A read, so the view permission is enough -- printing a return the
        warehouse already posted is not a second chance to post one. The sheet is
        read from SAP every time rather than snapshotted at posting: the document
        can still be amended in SAP afterwards, and a sheet printed from a stale
        copy is the kind of error nobody notices until the customer does.

        A return booked against several invoices has a document per invoice, so
        `doc_entry` names which one to print; without it the first is printed,
        which is the whole set for the single-invoice returns that are the norm.
        Only a document belonging to *this* return can be asked for -- otherwise
        the endpoint would print any return in the company by doc entry.
        """
        from sap_client.client import SAPClient

        gr = self._get_scoped(pk, allowed_company_ids)
        wanted = self._printable_doc_entry(gr, doc_entry)

        payload = SAPClient(company_code=gr.company.code).goods_return_print(wanted)
        if not payload:
            raise ValueError(
                f"SAP has no return {self._doc_num_for(gr, wanted) or wanted} "
                f"for {gr.company.code}."
            )
        payload["goods_return_id"] = gr.id
        payload["entry_no"] = gr.entry_no
        return payload

    @staticmethod
    def _doc_num_for(gr: GoodsReturn, doc_entry) -> str:
        """The document number behind a doc entry, for a message a person reads."""
        for ref in gr.active_invoice_refs:
            if ref.sap_gr_doc_entry == doc_entry:
                return ref.sap_gr_doc_num
        return gr.sap_gr_doc_num if gr.sap_gr_doc_entry == doc_entry else ""

    @staticmethod
    def _printable_doc_entry(gr: GoodsReturn, doc_entry=None) -> int:
        """Which of the return's documents to print, refusing anything else."""
        own = {
            ref.sap_gr_doc_entry
            for ref in gr.active_invoice_refs
            if ref.sap_gr_doc_entry is not None
        }
        if gr.sap_gr_doc_entry:
            own.add(gr.sap_gr_doc_entry)
        if not own:
            raise ValueError(
                "This return has not been posted to SAP yet, so there is no "
                "Return Note to print."
            )
        if doc_entry in (None, ""):
            # The header's own document, which for a single-invoice return is the
            # only one there is.
            return gr.sap_gr_doc_entry or sorted(own)[0]
        try:
            wanted = int(doc_entry)
        except (TypeError, ValueError):
            raise ValueError(f"{doc_entry} is not a SAP document entry.")
        if wanted not in own:
            raise ValueError(
                f"Return {wanted} does not belong to {gr.entry_no}."
            )
        return wanted

    @staticmethod
    def _place_of_supply(gr: GoodsReturn, client, ref=None) -> dict:
        """The ship-to / bill-to one document must carry, and its GST state.

        Taken from the invoice that document is returning (or, for a debit-note or
        letter-pad return, the customer's most recent invoice). Per invoice rather
        than per return, because a customer with depots in two states is billed to
        two: the return of each bill has to go back to the address that bill was
        sold to.

        This is not cosmetic: leave the addresses off and SAP resolves the place of
        supply from `OCRD.ShipToDef`, which for a distributor holding stock in
        several states is usually a different state from the one actually billed.
        The return then reads as inter-state while carrying the invoice's CGST+SGST
        code, and SAP refuses the document outright -- `254000293 For interstate
        transactions (line 1) you must choose IGST`.
        """
        addresses: dict = {}
        refs = [ref] if ref is not None else gr.active_invoice_refs
        for candidate in refs:
            addresses = client.invoice_addresses(candidate.sap_invoice_doc_entry) or {}
            if addresses.get("ship_to_code"):
                break
        if not addresses.get("ship_to_code"):
            addresses = client.customer_last_invoice_addresses(gr.customer_code) or {}

        # `INV12` can be missing on an old document; the address itself still
        # knows its state.
        if addresses.get("ship_to_code") and not addresses.get("ship_state"):
            addresses["ship_state"] = client.customer_address_state(
                gr.customer_code, addresses["ship_to_code"]
            )
        return addresses

    @staticmethod
    def _sap_comment(gr: GoodsReturn, ref=None) -> str:
        """What this document was booked against, in SAP's own Comments field.

        One invoice, not the return's whole list: each document answers for its
        own bill, so naming the others on it would make every one of them look
        like the return of all of them.
        """
        if gr.basis == GoodsReturnBasis.INVOICE:
            doc_num = ref.sap_invoice_doc_num if ref is not None else ""
            against = f"invoice {doc_num}" if doc_num else "invoice"
        elif gr.basis == GoodsReturnBasis.DEBIT_NOTE:
            against = "customer debit note"
        else:
            against = "customer letter pad"
        # The customer's own number rides in Comments, not NumAtCard: that field
        # is the app's handle on a document it has already posted and has to stay
        # unique per (return, invoice), and two returns may quote one debit note.
        if gr.customer_ref_no and gr.basis != GoodsReturnBasis.INVOICE:
            against = f"{against} {gr.customer_ref_no}"
        return f"Goods return {gr.entry_no} against {against}"[:254]

    @staticmethod
    def _sap_line(
        gr, line, warehouse_code, *, variety, tax_code, return_cost
    ) -> dict:
        from . import guards

        # The customer's own batch cannot be reused — SAP refuses a return into an
        # existing batch — so a fresh one is minted and the real batch is recorded
        # as line text, which is the only place it survives.
        notes = []
        if line.original_batch_number:
            notes.append(f"Returned batch {line.original_batch_number}")
        if line.condition:
            notes.append(line.get_condition_display())

        return {
            "ItemCode": line.item_code,
            "Quantity": float(line.return_quantity),
            "WarehouseCode": warehouse_code,
            # Zero price: the stock comes back but the customer is not credited
            # here. The credit note is a separate finance step.
            "UnitPrice": 0,
            "TaxCode": tax_code,
            "CostingCode": variety,
            # ReturnCost x Quantity becomes OINM.TransValue, i.e. this is what the
            # returned stock is worth. Mandatory for batch-managed items (160021).
            "EnableReturnCost": "tYES",
            "ReturnCost": float(return_cost),
            "BatchNumbers": [
                {
                    # The line's own id, not its position: a return posts one
                    # document per invoice, and two documents both numbering from
                    # zero would mint the same batch twice for an item that came
                    # back off both bills -- which SAP refuses (10001226).
                    "BatchNumber": guards.batch_number_for(gr.entry_no, line.pk),
                    "Quantity": float(line.return_quantity),
                }
            ],
            "FreeText": " / ".join(notes)[:100],
        }

    # -- approval (admin) ------------------------------------------------------

    def _decide_approval(self, pk, user, remarks, allowed_company_ids, decision):
        gr = self._get_scoped(pk, allowed_company_ids)
        if not gr.requires_approval:
            raise ValueError("This return does not require approval.")
        if gr.status in (
            GoodsReturnStatus.RECEIVED,
            GoodsReturnStatus.PARTIALLY_POSTED,
            GoodsReturnStatus.POSTED,
            GoodsReturnStatus.CANCELLED,
        ):
            raise ValueError("This return can no longer be approved or rejected.")
        gr.approval_status = decision
        gr.approved_by = user
        gr.approved_at = timezone.now()
        gr.approval_remarks = (remarks or "").strip()
        gr.updated_by = user
        gr.save(
            update_fields=[
                "approval_status",
                "approved_by",
                "approved_at",
                "approval_remarks",
                "updated_by",
                "updated_at",
            ]
        )
        return gr

    def approve(self, pk, user, remarks, allowed_company_ids) -> GoodsReturn:
        return self._decide_approval(
            pk, user, remarks, allowed_company_ids, GoodsReturnApprovalStatus.APPROVED
        )

    def reject(self, pk, user, remarks, allowed_company_ids) -> GoodsReturn:
        return self._decide_approval(
            pk, user, remarks, allowed_company_ids, GoodsReturnApprovalStatus.REJECTED
        )

    def cancel(self, pk, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        if gr.status in (
            GoodsReturnStatus.ARRIVED,
            GoodsReturnStatus.RECEIVED,
            GoodsReturnStatus.PARTIALLY_POSTED,
            GoodsReturnStatus.POSTED,
        ):
            raise ValueError("A return already received at the gate cannot be cancelled.")
        gr.status = GoodsReturnStatus.CANCELLED
        gr.updated_by = user
        gr.save(update_fields=["status", "updated_by", "updated_at"])
        return gr

    # -- attachments -----------------------------------------------------------

    def list_attachments(self, pk, allowed_company_ids):
        gr = self._get_scoped(pk, allowed_company_ids)
        return gr.attachments.all()

    def upload_attachment(self, pk, file, attachment_type, notes, user, allowed_company_ids):
        gr = self._get_scoped(pk, allowed_company_ids)
        return GoodsReturnAttachment.objects.create(
            goods_return=gr,
            attachment_type=attachment_type or "OTHER",
            file=file,
            original_filename=getattr(file, "name", "")[:255],
            notes=(notes or "").strip(),
            uploaded_by=user,
        )

    def delete_attachment(self, pk, attachment_id, allowed_company_ids):
        gr = self._get_scoped(pk, allowed_company_ids)
        attachment = gr.attachments.filter(pk=attachment_id).first()
        if attachment is None:
            raise ValueError("Attachment not found.")
        attachment.delete()


# ===========================================================================
# Gate side -- cross-company (expected list + mark-in)
# ===========================================================================

def list_expected_returns(company_ids):
    """Returns awaiting a gate arrival for the caller's companies.

    A return lands here the moment its first page is saved -- the clerk may still
    be keying in the items, and the gate does not have to wait for that to let the
    truck in.
    """
    return (
        GoodsReturn.objects.filter(
            status=GoodsReturnStatus.AWAITING_ARRIVAL,
            vehicle_entry__isnull=True,
            is_active=True,
            company_id__in=company_ids,
        )
        .select_related("company", "vehicle", "driver")
        .prefetch_related("lines", "invoice_refs")
        .order_by("expected_arrival_at", "id")
    )


# How far back the gate history looks when the page does not ask for a window.
# The gate's question is "what did we let in today / yesterday", not "show me
# every return we ever took" -- and the table only grows.
GATE_HISTORY_DEFAULT_DAYS = 7


def list_gate_history(company_ids, *, from_date=None, to_date=None, search=None):
    """Returns the gate has already marked in, newest first.

    The queue drops a return the moment it is marked in, so without this the gate
    has no way to check what it let in an hour ago -- the rest of the return's
    life is on the Returns module, which a gate-only user cannot open. Windowed on
    the gate-in date, defaulting to the last week.
    """
    if to_date is None:
        to_date = timezone.localdate()
    if from_date is None:
        from_date = to_date - timedelta(days=GATE_HISTORY_DEFAULT_DAYS - 1)

    qs = (
        GoodsReturn.objects.filter(
            gated_in_at__isnull=False,
            gated_in_at__date__gte=from_date,
            gated_in_at__date__lte=to_date,
            is_active=True,
            company_id__in=company_ids,
        )
        .select_related("company", "vehicle", "driver", "gated_in_by")
        .prefetch_related("lines", "invoice_refs")
        .order_by("-gated_in_at")
    )
    search = (search or "").strip()
    if search:
        qs = qs.filter(
            Q(entry_no__icontains=search)
            | Q(customer_name__icontains=search)
            | Q(customer_code__icontains=search)
            | Q(vehicle__vehicle_number__icontains=search)
        )
    return qs


@transaction.atomic
def mark_return_in(pk, user, data, company_ids) -> GoodsReturn:
    """Gate marks the return's vehicle in: create a shared VehicleEntry (the truck
    reads as inside) and flip the return to ARRIVED. Company resolved from the record."""
    gr = (
        GoodsReturn.objects.select_for_update(of=("self",))
        .filter(pk=pk, is_active=True)
        .select_related("company", "vehicle", "driver")
        .first()
    )
    if gr is None:
        raise ValueError("Goods return not found.")
    if gr.company_id not in company_ids:
        raise PermissionDenied("This record belongs to a company you cannot access.")
    if gr.status != GoodsReturnStatus.AWAITING_ARRIVAL or gr.vehicle_entry_id:
        raise ValueError("This return is not awaiting a gate arrival.")

    # The clerk books the truck on the return's first page, so it is normally
    # already here. Returns booked before that was so can still arrive without
    # one, and then whatever the gate supplies is written back onto the return --
    # the ledger row it creates cannot exist without a vehicle/driver.
    if not gr.vehicle_id and data.get("vehicle_id"):
        vehicle = Vehicle.objects.filter(pk=data["vehicle_id"]).first()
        if vehicle is None:
            raise ValueError("Selected vehicle not found.")
        gr.vehicle = vehicle
    if not gr.driver_id and data.get("driver_id"):
        driver = Driver.objects.filter(pk=data["driver_id"]).first()
        if driver is None:
            raise ValueError("Selected driver not found.")
        gr.driver = driver
    if not gr.vehicle_id or not gr.driver_id:
        raise ValueError("Pick the vehicle and driver arriving with this return.")

    vehicle_entry = VehicleEntry.objects.create(
        entry_no=_generate_vehicle_entry_no(),
        company=gr.company,
        vehicle=gr.vehicle,
        driver=gr.driver,
        entry_type="GOODS_RETURN",
        status=GateEntryStatus.IN_PROGRESS,  # truck is now inside
        created_by=user,
        remarks=(data.get("remarks") or "").strip(),
    )
    gr.vehicle_entry = vehicle_entry
    gr.status = GoodsReturnStatus.ARRIVED
    gr.gated_in_by = user
    gr.gated_in_at = timezone.now()
    gr.updated_by = user
    gr.save(
        update_fields=[
            "vehicle",
            "driver",
            "vehicle_entry",
            "status",
            "gated_in_by",
            "gated_in_at",
            "updated_by",
            "updated_at",
        ]
    )
    return gr
