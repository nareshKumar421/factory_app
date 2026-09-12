from typing import List, Optional
from .context import CompanyContext
from .hana.ar_invoice_print_reader import HanaARInvoicePrintReader
from .hana.ar_invoice_reader import HanaARInvoiceReader
from .hana.approval_reader import HanaApprovalReader
from .hana.sap_user_reader import HanaSapUserReader
from .hana.transfer_approval_reader import HanaTransferApprovalReader
from .hana.transfer_draft_reader import HanaTransferDraftReader
from .hana.customer_reader import HanaCustomerReader
from .hana.dismantle_reader import HanaDismantleReader
from .hana.grpo_print_reader import HanaGRPOPrintReader
from .hana.grpo_reader import HanaGRPOReader
from .hana.po_print_reader import HanaPOPrintReader
from .hana.po_reader import HanaPOReader
from .hana.service_grpo_options_reader import HanaServiceGRPOOptionsReader
from .hana.batch_stock_reader import HanaBatchStockReader
from .hana.returns_reader import HanaReturnsReader
from .hana.series_reader import HanaSeriesReader
from .hana.stock_transfer_reader import HanaStockTransferReader
from .hana.transfer_request_reader import HanaTransferRequestReader
from .hana.warehouse_reader import HanaWarehouseReader
from .hana.vendor_reader import HanaVendorReader
from .service_layer.ap_invoice_writer import APInvoiceWriter
from .service_layer.ar_invoice_writer import ARInvoiceWriter
from .service_layer.approval_writer import ApprovalRequestWriter
from .service_layer.delivery_note_writer import DeliveryNoteWriter, GoodsIssueWriter
from .service_layer.disassembly_writer import (
    DisassemblyOrderWriter,
    ProductionIssueWriter,
    ProductionReceiptWriter,
)
from .service_layer.grpo_writer import GRPOWriter
from .service_layer.attachment_writer import AttachmentWriter
from .service_layer.itr_writer import InventoryTransferRequestWriter
from .service_layer.production_order_writer import ProductionOrderWriter
from .service_layer.stock_transfer_writer import StockTransferWriter
from .dtos import PODTO, POAdditionalExpenseDTO, WarehouseDTO, VendorDTO


class SAPClient:
    """
    Single entry point for SAP operations per company
    """

    def __init__(self, company_code: str):
        self.context = CompanyContext(company_code)

    # ---- READ ----
    def get_open_pos(self, supplier_code: str) -> List[PODTO]:
        self.po_reader = HanaPOReader(self.context)
        return self.po_reader.get_open_pos(supplier_code)

    def get_open_finished_goods_pos(self, supplier_code: str) -> List[PODTO]:
        reader = HanaPOReader(self.context)
        return reader.get_open_finished_goods_pos(supplier_code)

    def get_open_po_by_number(self, po_number: str) -> Optional[PODTO]:
        reader = HanaPOReader(self.context)
        return reader.get_open_po_by_number(po_number)

    def get_po_date_by_doc_entry(self, doc_entry: int):
        reader = HanaPOReader(self.context)
        return reader.get_po_date_by_doc_entry(doc_entry)

    def get_po_open_qtys(self, doc_entries: List[int]) -> dict:
        """Live POR1.OpenQty keyed by ``(doc_entry, line_num)``. Raises on failure."""
        reader = HanaPOReader(self.context)
        return reader.get_po_open_qtys(doc_entries)

    def get_po_additional_expenses(
        self, doc_entries: List[int]
    ) -> dict[int, List[POAdditionalExpenseDTO]]:
        """PO freight/expense lines keyed by PO DocEntry. Fail-soft (see reader)."""
        reader = HanaPOReader(self.context)
        return reader.get_po_additional_expenses(doc_entries)

    def get_return_warehouses(self) -> List[WarehouseDTO]:
        """The goods-return warehouses (``-GR``/``-GRM``/``-RG``)."""
        reader = HanaWarehouseReader(self.context)
        return reader.get_return_warehouses()

    def get_active_warehouses(self) -> List[WarehouseDTO]:
        reader = HanaWarehouseReader(self.context)
        return reader.get_active_warehouses()

    def get_warehouse_branches(self) -> dict:
        """Warehouse code -> OWHS.BPLid, for classifying a transfer route."""
        reader = HanaWarehouseReader(self.context)
        return reader.get_warehouse_branches()

    def get_warehouse_print_info(self, warehouse_codes: List[str]) -> dict:
        """Company letterhead + per-warehouse address/GST for transfer prints."""
        reader = HanaWarehouseReader(self.context)
        return reader.get_warehouse_print_info(warehouse_codes)

    def state_names(self, state_codes: List[str]) -> dict:
        """``{state code: printed name}`` (HR -> HARYANA), for document layouts."""
        reader = HanaWarehouseReader(self.context)
        return reader.get_state_names(state_codes)

    def get_warehouse_stock(self, warehouse_code: str, **kwargs) -> List[dict]:
        """Items held in one warehouse, with on-hand and available quantities."""
        reader = HanaWarehouseReader(self.context)
        return reader.get_warehouse_stock(warehouse_code, **kwargs)

    # ---- Goods return (A/R Return) prerequisites ----
    def return_variety_codes(self, item_codes) -> dict:
        """Item -> Dimension-1 Variety code SAP demands on a return line."""
        return HanaReturnsReader(self.context).variety_codes(item_codes)

    def return_costs(self, item_codes, warehouse: str) -> dict:
        """Item -> unit cost to value returned stock at (drives OINM.TransValue)."""
        return HanaReturnsReader(self.context).return_costs(item_codes, warehouse)

    def return_tax_codes(self, card_code: str, item_codes) -> dict:
        """Item -> the tax code this customer was last billed for it."""
        return HanaReturnsReader(self.context).sales_tax_codes(card_code, item_codes)

    def return_item_options(self, card_code: str = "", **kwargs) -> List[dict]:
        """Every finished good, for the return item picker.

        `card_code` only annotates the rows with what this customer was last
        billed -- it never narrows the list. Goods come back for reasons that
        have nothing to do with who was invoiced for them.
        """
        return HanaReturnsReader(self.context).finished_goods(card_code, **kwargs)

    def customer_group_code(self, card_code: str):
        """OCRD.GroupCode — 100 means an internal branch, which cannot be returned to."""
        return HanaReturnsReader(self.context).customer_group(card_code)

    def warehouse_branch_id(self, warehouse_code: str):
        """OWHS.BPLid for the branch a marketing document must be stamped with."""
        return HanaReturnsReader(self.context).warehouse_branch(warehouse_code)

    def branch_state(self, branch_id):
        """OBPL.State — one half of the GST place-of-supply comparison."""
        return HanaReturnsReader(self.context).branch_state(branch_id)

    def invoice_addresses(self, doc_entry) -> dict:
        """ShipTo/PayTo codes + resolved states from one A/R invoice."""
        return HanaReturnsReader(self.context).invoice_addresses(doc_entry)

    def customer_last_invoice_addresses(self, card_code: str) -> dict:
        """The same, from the customer's newest invoice (no source invoice case)."""
        return HanaReturnsReader(self.context).customer_last_invoice_addresses(card_code)

    def customer_address_state(self, card_code: str, address_name: str) -> str:
        """CRD1.State for one named customer address."""
        return HanaReturnsReader(self.context).address_state(card_code, address_name)

    def ar_tax_codes(self) -> dict:
        """Sales tax codes SAP accepts, by upper-cased code, with name + rate."""
        return HanaReturnsReader(self.context).ar_tax_codes()

    def find_goods_return_by_reference(self, card_code: str, num_at_card: str):
        """A live A/R Return already posted under this customer reference, or None."""
        return HanaReturnsReader(self.context).find_by_reference(card_code, num_at_card)

    def goods_return_print(self, doc_entry) -> dict:
        """One posted A/R Return as SAP's own Return layout prints it."""
        return HanaReturnsReader(self.context).return_print(doc_entry)

    # ---- Invoice approvals (SAP approval procedure on A/R invoice drafts) ----
    def list_invoice_approvals(
        self, warehouse: str, status: str | None = None, limit: int = 200
    ) -> list[dict]:
        """Approval requests on A/R invoice drafts shipping from one warehouse."""
        reader = HanaApprovalReader(self.context)
        return reader.list_approvals(warehouse, status=status, limit=limit)

    def count_pending_invoice_approvals(self, warehouse: str) -> int:
        reader = HanaApprovalReader(self.context)
        return reader.pending_count(warehouse)

    def invoice_approval_warehouses(self, wdd_code: int) -> set:
        """Warehouse codes on the invoice behind one approval request (for scoping)."""
        reader = HanaApprovalReader(self.context)
        return reader.request_warehouses(wdd_code)

    def invoice_approval_history(self, wdd_code: int) -> list[dict]:
        """The draft's full approval trail (every request + decided stage)."""
        reader = HanaApprovalReader(self.context)
        return reader.approval_history(wdd_code)

    def invoice_approval_stage(self, wdd_code: int) -> dict:
        """The stage an invoice approval waits on, and the user who must sign it."""
        return HanaApprovalReader(self.context).current_stage(wdd_code)

    def decide_invoice_approval(
        self,
        wdd_code: int,
        approve: bool,
        remarks: str = "",
        approver: str | None = None,
    ) -> dict:
        """Approve or reject one approval request, signed as ``approver``.

        Without ``approver`` this falls back to the single configured approval
        account, which SAP accepts only where that account happens to be the
        stage's authorizer.
        """
        writer = ApprovalRequestWriter(self.context)
        return writer.decide(wdd_code, approve, remarks, approver=approver)

    # ---- Transfer approvals (approval procedure on inventory-transfer drafts) ----
    def list_transfer_approvals(
        self, status: str | None = "PENDING", limit: int = 100
    ) -> list[dict]:
        """SAP approval requests on stock-transfer and transfer-request drafts."""
        reader = HanaTransferApprovalReader(self.context)
        return reader.list_approvals(status=status, limit=limit)

    def count_pending_transfer_approvals(self) -> int:
        return HanaTransferApprovalReader(self.context).pending_count()

    def list_sap_users(self, include_locked: bool = False) -> list[dict]:
        """SAP B1 user accounts, with how many active templates they authorize."""
        return HanaSapUserReader(self.context).list_users(include_locked=include_locked)

    def transfer_approval_stage(self, wdd_code: int) -> dict:
        """The stage a transfer approval waits on, and the user who must sign it."""
        return HanaTransferApprovalReader(self.context).current_stage(wdd_code)

    def decide_transfer_approval(
        self,
        wdd_code: int,
        approve: bool,
        remarks: str = "",
        approver: str | None = None,
    ) -> dict:
        """Approve or reject one transfer approval request, signed as ``approver``."""
        writer = ApprovalRequestWriter(self.context)
        return writer.decide(
            wdd_code, approve, remarks, approver=approver, subject="Transfer"
        )

    # ---- Transfer drafts (approved in SAP, but never added) ----
    def list_unposted_transfer_drafts(self, limit: int = 100) -> list[dict]:
        """Approved inventory-transfer drafts whose stock has not moved yet."""
        return HanaTransferDraftReader(self.context).list_unposted(limit=limit)

    def count_unposted_transfer_drafts(self) -> int:
        return HanaTransferDraftReader(self.context).unposted_count()

    def get_transfer_draft(self, draft_entry: int) -> dict | None:
        """One transfer draft with its lines, whether or not it can be added."""
        return HanaTransferDraftReader(self.context).get_draft(draft_entry)

    def stock_transfer_for_draft(self, draft_entry: int) -> dict | None:
        """The OWTR a draft was added as (``draftKey``), if it already was."""
        return HanaTransferDraftReader(self.context).posted_document(draft_entry)

    # ---- A/R invoices (creation + approval tracking, ObjType 13) ----
    def search_customers(self, search: str | None = None, limit: int = 50) -> list[dict]:
        """Type-ahead customer search over OCRD (active, non-frozen customers)."""
        reader = HanaCustomerReader(self.context)
        return reader.search_customers(search=search, limit=limit)

    def get_customer(self, card_code: str) -> dict | None:
        """One customer by exact code."""
        reader = HanaCustomerReader(self.context)
        return reader.get_customer(card_code)

    def customer_credit_status(self, card_code: str) -> dict | None:
        """One customer's credit limit and what is already drawn against it."""
        reader = HanaCustomerReader(self.context)
        return reader.get_credit_status(card_code)

    def ar_last_sale_defaults(self, card_code: str, item_codes: list) -> dict:
        """Item -> {price, tax_code} from the customer's latest invoice line."""
        reader = HanaARInvoiceReader(self.context)
        return reader.last_sale_defaults(card_code, list(item_codes))

    def open_so_lines_for_invoicing(
        self, card_code: str, search: str | None = None, limit: int = 300
    ) -> list[dict]:
        """One customer's open Sales Order lines (open quantity > 0)."""
        reader = HanaARInvoiceReader(self.context)
        return reader.open_so_lines(card_code, search=search, limit=limit)

    def ar_invoice_for_draft(self, draft_entry: int) -> dict | None:
        """The posted OINV invoice created from one approval draft, if any."""
        reader = HanaARInvoiceReader(self.context)
        return reader.invoice_for_draft(draft_entry)

    def ar_draft_state(self, draft_entry: int) -> dict | None:
        """Draft document status + latest approval request state for a draft."""
        reader = HanaARInvoiceReader(self.context)
        return reader.draft_state(draft_entry)

    def ar_draft_lines(self, draft_entry: int) -> list[dict]:
        """The draft's own lines — the set a batch allocation is written against."""
        reader = HanaARInvoiceReader(self.context)
        return reader.draft_lines(draft_entry)

    def ar_cash_sale_invoices(
        self,
        card_codes: list | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        search: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Posted A/R invoices of the counter/cash-sale customers, with lines."""
        reader = HanaARInvoiceReader(self.context)
        return reader.cash_sale_invoices(
            card_codes=list(card_codes or []),
            date_from=date_from,
            date_to=date_to,
            search=search,
            limit=limit,
        )

    def ar_cash_sale_state(
        self, doc_entry: int, card_codes: list | None = None
    ) -> dict | None:
        """Is this posted invoice one of the cash-sale customers', and is it live?"""
        reader = HanaARInvoiceReader(self.context)
        return reader.cash_sale_invoice_state(doc_entry, card_codes=list(card_codes or []))

    def ar_invoice_print(self, doc_entry: int) -> dict | None:
        """One posted A/R invoice shaped for SAP's own TAX INVOICE layout."""
        reader = HanaARInvoicePrintReader(self.context)
        return reader.invoice_print(doc_entry)

    def get_active_vendors(self) -> List[VendorDTO]:
        reader = HanaVendorReader(self.context)
        return reader.get_active_vendors()

    def list_stock_transfers(
        self,
        search: str | None = None,
        from_date=None,
        to_date=None,
        limit: int = 50,
    ) -> list[dict]:
        reader = HanaStockTransferReader(self.context)
        return reader.list_transfers(
            search=search,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )

    def get_stock_transfer(self, doc_entry: int) -> dict | None:
        reader = HanaStockTransferReader(self.context)
        return reader.get_transfer(doc_entry)

    def get_transfer_request(self, doc_entry: int) -> dict | None:
        reader = HanaTransferRequestReader(self.context)
        return reader.get_request(doc_entry)

    def list_open_transfer_requests(self, **filters) -> list[dict]:
        reader = HanaTransferRequestReader(self.context)
        return reader.list_open_requests(**filters)

    def get_transfer_request_open_quantities(self, doc_entry: int) -> dict:
        reader = HanaTransferRequestReader(self.context)
        return reader.open_quantities(doc_entry)

    def summarise_transfer_requests(self, doc_entries: list) -> dict:
        """Line totals for many transfer requests in one query."""
        reader = HanaTransferRequestReader(self.context)
        return reader.summarise_requests(doc_entries)

    def resolve_series(self, object_code: str, posting_date) -> dict:
        """Numbering series for a posting date — series are month-specific."""
        reader = HanaSeriesReader(self.context)
        return reader.resolve(object_code, posting_date)

    def series_name(self, series) -> str:
        """The name SAP prints for a series id already on a document (2094 -> DELG0926)."""
        reader = HanaSeriesReader(self.context)
        return reader.name_for(series)

    def batch_managed_flags(self, item_codes) -> dict[str, bool]:
        reader = HanaBatchStockReader(self.context)
        return reader.batch_managed_flags(item_codes)

    def available_batches(self, item_code: str, warehouse: str) -> list[dict]:
        reader = HanaBatchStockReader(self.context)
        return reader.available_batches(item_code, warehouse)

    def allocate_batches_fifo(self, item_code: str, warehouse: str, quantity) -> list[dict]:
        reader = HanaBatchStockReader(self.context)
        return reader.allocate_fifo(item_code, warehouse, quantity)

    def posted_batch_allocations(self, doc_entry: int, **kwargs) -> list[dict]:
        reader = HanaBatchStockReader(self.context)
        return reader.posted_allocations(doc_entry, **kwargs)

    # ---- dismantle (disassembly) reads ----
    def dismantle_bom(self, item_code: str) -> list[dict]:
        """One PIECE of ``item_code`` exploded into its production-BOM components."""
        reader = HanaDismantleReader(self.context)
        return reader.bom_components(item_code)

    def dismantle_parent_info(self, item_code: str) -> dict | None:
        """Item master + the batch size its recipe is written for."""
        reader = HanaDismantleReader(self.context)
        return reader.parent_info(item_code)

    def dismantlable_stock(self, warehouse_code: str, **kwargs) -> list[dict]:
        """Stock in a warehouse that has a production BOM, i.e. can be taken apart."""
        reader = HanaDismantleReader(self.context)
        return reader.dismantlable_stock(warehouse_code, **kwargs)

    def return_batches(self, warehouse_codes, **kwargs) -> list[dict]:
        """Goods-return batches as SAP holds them — never re-derive the number."""
        reader = HanaDismantleReader(self.context)
        return reader.return_batches(warehouse_codes, **kwargs)

    def existing_batches(self, item_codes, batch_numbers) -> set:
        """(item, batch) pairs SAP already knows — a receipt may not reuse one."""
        reader = HanaDismantleReader(self.context)
        return reader.existing_batches(item_codes, batch_numbers)

    def list_grpos(
        self,
        search: str | None = None,
        from_date=None,
        to_date=None,
        limit: int = 50,
        crude_oil_only: bool = False,
    ) -> list[dict]:
        reader = HanaGRPOReader(self.context)
        return reader.list_grpos(
            search=search,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            crude_oil_only=crude_oil_only,
        )

    def get_grpo(self, doc_entry: int, crude_oil_only: bool = False) -> dict | None:
        reader = HanaGRPOReader(self.context)
        return reader.get_grpo(doc_entry, crude_oil_only=crude_oil_only)

    def grpo_print(self, doc_entry: int) -> dict | None:
        """One posted GRPO shaped for SAP's own Goods Receipt Note layout."""
        reader = HanaGRPOPrintReader(self.context)
        return reader.grpo_print(doc_entry)

    def po_print(self, doc_entry: int) -> dict | None:
        """One purchase order shaped for SAP's own Purchase Order layout."""
        reader = HanaPOPrintReader(self.context)
        return reader.po_print(doc_entry)

    def po_doc_entry_for_number(self, po_number: str) -> int | None:
        """The SAP ``DocEntry`` behind a PO number, for printing an older receipt."""
        reader = HanaPOPrintReader(self.context)
        return reader.doc_entry_for_number(po_number)

    def get_service_grpo_options(self) -> dict:
        reader = HanaServiceGRPOOptionsReader(self.context)
        return reader.get_options()

    def get_expense_codes(self) -> List[dict]:
        """SAP additional-expense master (OEXD) for this company.

        Expense codes are company-scoped, so the material GRPO screen must read
        them per company rather than carry a hardcoded list.
        """
        reader = HanaServiceGRPOOptionsReader(self.context)
        return reader.get_expense_code_options()

    # ---- WRITE ----
    def create_production_order(self, payload: dict) -> dict:
        writer = ProductionOrderWriter(self.context)
        return writer.create(payload)

    def create_grpo(self, payload: dict):
        self.grpo_writer = GRPOWriter(self.context)
        return self.grpo_writer.create(payload)

    def create_ap_invoice(self, payload: dict):
        """Post an A/P invoice. When SAP routes it into an approval procedure
        the result is ``{"pending_approval": True, "draft_entry": N}`` instead
        of a posted document — see ``APInvoiceWriter``."""
        writer = APInvoiceWriter(self.context)
        return writer.create(payload)

    def create_ar_invoice(self, payload: dict):
        """Post an A/R invoice. When SAP routes it into an approval procedure
        the result is ``{"pending_approval": True, "draft_entry": N}`` instead
        of a posted document — see ``ARInvoiceWriter``."""
        writer = ARInvoiceWriter(self.context)
        return writer.create(payload)

    def update_ar_draft(self, draft_entry: int, payload: dict) -> None:
        """PATCH an A/R invoice draft (e.g. write line batch allocations)."""
        writer = ARInvoiceWriter(self.context)
        writer.patch_draft(draft_entry, payload)

    def save_ar_draft_to_document(self, draft_entry: int) -> None:
        """Post an approved A/R invoice draft as the real OINV document."""
        writer = ARInvoiceWriter(self.context)
        writer.save_draft_to_document(draft_entry)

    def create_delivery_note(self, payload: dict) -> dict:
        """Create an outbound Delivery Note (decrements FG stock)."""
        writer = DeliveryNoteWriter(self.context)
        return writer.create(payload)

    def create_stock_transfer(self, payload: dict) -> dict:
        """Post an inventory transfer (OWTR). A 201 means stock has moved."""
        return StockTransferWriter(self.context).create(payload)

    def add_stock_transfer_draft(self, draft_entry: int) -> None:
        """Add an approved inventory-transfer draft as the real OWTR document.

        The SAP-client **Add** button, through the Service Layer. Posts the
        draft as it stands — batch allocations included — and answers nothing,
        so read the document back with ``stock_transfer_for_draft``.
        """
        StockTransferWriter(self.context).save_draft_to_document(draft_entry)

    def cancel_stock_transfer(self, doc_entry: int) -> None:
        """Cancel a transfer. SAP writes a reversing document to undo it."""
        StockTransferWriter(self.context).cancel(doc_entry)

    def create_transfer_request(self, payload: dict) -> dict:
        """Post an inventory transfer request (OWTQ). Reserves stock, moves none."""
        return InventoryTransferRequestWriter(self.context).create(payload)

    def close_transfer_request(self, doc_entry: int) -> None:
        """Retire a request, releasing its reservation.

        The only retirement SAP allows on this entity — Cancel is rejected with
        -5006 even while the request is still open. Used for both a rejected
        request and an abandoned one; the app records which it was.
        """
        InventoryTransferRequestWriter(self.context).close(doc_entry)

    def create_goods_issue(self, payload: dict) -> dict:
        """Create an Inventory Goods Issue (consumes packing materials)."""
        writer = GoodsIssueWriter(self.context)
        return writer.create(payload)

    # ---- dismantle (disassembly) writes ----
    #
    # Three calls in a FIXED order — order, then receipt, then issue. SAP refuses
    # a disassembly's goods issue while its receipt is missing (20206), so these
    # are never to be reordered. See ``service_layer/disassembly_writer.py``.
    def create_disassembly_order(self, payload: dict) -> dict:
        """Create the disassembly production order (``OWOR."Type" = 'D'``)."""
        return DisassemblyOrderWriter(self.context).create(payload)

    def disassembly_order_lines(self, doc_entry: int) -> list[dict]:
        """The order's component lines as SAP stored them, for their LineNum."""
        return DisassemblyOrderWriter(self.context).get_lines(doc_entry)

    def create_production_receipt(self, payload: dict) -> dict:
        """Receipt from Production — on a disassembly this receives the components."""
        return ProductionReceiptWriter(self.context).create(payload)

    def create_production_issue(self, payload: dict) -> dict:
        """Issue for Production — on a disassembly this consumes the parent item."""
        return ProductionIssueWriter(self.context).create(payload)

    def close_production_order(self, doc_entry: int) -> None:
        """Close the order once both completion documents are in."""
        DisassemblyOrderWriter(self.context).close(doc_entry)

    def list_documents(self, entity: str, *, select: str = "", filter: str = "",
                       top: int = 20) -> list:
        """Generic read of a Service Layer collection (``entity``) with optional
        ``$select`` / ``$filter`` / ``$top``. Returns the ``value`` list (or [])."""
        from .service_layer.reader import list_collection
        return list_collection(self.context, entity, select=select, filter=filter, top=top)

    def upload_attachment(
        self,
        file_path: str,
        filename: str,
        *,
        allow_metadata_fallback: bool = False,
    ) -> dict:
        """Upload a file to SAP Attachments2"""
        writer = AttachmentWriter(self.context)
        return writer.upload(
            file_path,
            filename,
            allow_metadata_fallback=allow_metadata_fallback,
        )

    def get_grpo_attachment_entry(self, doc_entry: int) -> Optional[int]:
        """Get the existing AttachmentEntry from a GRPO document"""
        writer = AttachmentWriter(self.context)
        return writer.get_document_attachment_entry(doc_entry)

    def add_line_to_existing_attachment(
        self,
        absolute_entry: int,
        file_path: str,
        filename: str,
        *,
        allow_metadata_fallback: bool = False,
    ) -> dict:
        """Add a new file line to an existing Attachments2 entry"""
        writer = AttachmentWriter(self.context)
        return writer.add_line_to_existing_attachment(
            absolute_entry,
            file_path,
            filename,
            allow_metadata_fallback=allow_metadata_fallback,
        )

    def link_attachment_to_grpo(self, doc_entry: int, absolute_entry: int) -> dict:
        """Link an attachment to a GRPO document"""
        writer = AttachmentWriter(self.context)
        return writer.link_to_document(doc_entry, absolute_entry)
