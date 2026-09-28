from typing import List, Optional
from .context import CompanyContext
from .exceptions import SAPDataError, SAPValidationError
from .hana.ar_invoice_print_reader import HanaARInvoicePrintReader
from .hana.ar_invoice_reader import HanaARInvoiceReader
from .hana.approval_reader import HanaApprovalReader
from .hana.credit_note_approval_reader import HanaCreditNoteApprovalReader
from .hana.credit_note_print_reader import HanaCreditNotePrintReader
from .hana.sap_user_reader import HanaSapUserReader
from .hana.transfer_approval_reader import HanaTransferApprovalReader
from .hana.transfer_draft_reader import HanaTransferDraftReader
from .hana.customer_reader import HanaCustomerReader
from .hana.fg_stock_reader import HanaFGStockReader
from .hana.finance_reader import HanaFinanceReader
from .hana.lookup_reader import HanaLookupReader
from .hana.grpo_print_reader import HanaGRPOPrintReader
from .hana.grpo_reader import HanaGRPOReader
from .hana.po_print_reader import HanaPOPrintReader
from .hana.po_reader import HanaPOReader
from .hana.production_order_reader import HanaProductionOrderReader
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
from .service_layer.budget_writer import BudgetWriter
from .service_layer.business_partner_writer import BusinessPartnerWriter
from .service_layer.entity_client import ServiceLayerEntityClient
from .service_layer.file_service_client import SapFileServiceClient
from .service_layer.product_tree_writer import ProductTreeWriter
from .service_layer.delivery_note_writer import DeliveryNoteWriter, GoodsIssueWriter
from .service_layer.grpo_writer import GRPOWriter
from .service_layer.attachment_writer import AttachmentWriter
from .service_layer.itr_writer import InventoryTransferRequestWriter
from .service_layer.production_movement_writer import (
    IssueForProductionWriter,
    ReceiptFromProductionWriter,
)
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

    def get_fg_warehouse_stock(
        self,
        item_codes: Optional[List[str]] = None,
        warehouse_code: Optional[str] = None,
    ) -> List[dict]:
        """On-hand stock of FG items in one warehouse, for the invoice approver."""
        reader = HanaFGStockReader(self.context)
        return reader.get_fg_warehouse_stock(item_codes, warehouse_code)

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

    def sap_user_codes_by_id(self, user_ids) -> dict[int, dict]:
        """``{OUSR.USERID: {user_code, user_name}}`` — translates SAP Portal's numeric ids."""
        return HanaSapUserReader(self.context).user_codes_by_id(user_ids)

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

    # ---- Credit-note approvals (approval procedure on credit-note drafts) ----
    def list_credit_note_approvals(
        self,
        status: str | None = "PENDING",
        family: str | None = None,
        limit: int = 100,
        **filters,
    ) -> list[dict]:
        """SAP approval requests on A/R and A/P credit-note drafts.

        ``family`` narrows to one side: ``'AR'`` customer credit notes,
        ``'AP'`` vendor ones, ``'ALL'`` (the default) both. ``filters`` are the
        reader's search and paging keywords (party, doc_num, code, date_from,
        date_to, offset).
        """
        reader = HanaCreditNoteApprovalReader(self.context)
        return reader.list_approvals(status=status, family=family, limit=limit, **filters)

    def count_pending_credit_note_approvals(self, family: str | None = None) -> int:
        return HanaCreditNoteApprovalReader(self.context).pending_count(family=family)

    def credit_note_approval_stage(self, wdd_code: int) -> dict:
        """The stage a credit-note approval waits on, and who must sign it."""
        return HanaCreditNoteApprovalReader(self.context).current_stage(wdd_code)

    def decide_credit_note_approval(
        self,
        wdd_code: int,
        approve: bool,
        remarks: str = "",
        approver: str | None = None,
        password: str | None = None,
    ) -> dict:
        """Approve or reject one credit-note approval, signed as ``approver``.

        ``password`` is the approver's own SAP password when they typed it;
        without it the stored ``SAP_APPROVER_CREDENTIALS`` entry is used.
        """
        writer = ApprovalRequestWriter(self.context)
        return writer.decide(
            wdd_code, approve, remarks, approver=approver, subject="Credit note", password=password
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
        return reader.document_print(doc_entry)

    def credit_note_print(self, doc_entry: int) -> dict | None:
        """One posted A/R credit note, on the same sheet as the invoice.

        Raises ``SAPValidationError`` for a credit note that exists but has no
        sheet — cancelled, or service rather than item.
        """
        reader = HanaCreditNotePrintReader(self.context)
        return reader.document_print(doc_entry)

    def get_active_vendors(self) -> List[VendorDTO]:
        reader = HanaVendorReader(self.context)
        return reader.get_active_vendors()

    def list_stock_transfers(
        self,
        search: str | None = None,
        from_date=None,
        to_date=None,
        limit: int = 50,
        include_cancelled: bool = False,
    ) -> list[dict]:
        reader = HanaStockTransferReader(self.context)
        return reader.list_transfers(
            search=search,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            include_cancelled=include_cancelled,
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

    # ---- Master-data lookups (pickers ported from SAP Portal) ----
    def lookup_items(self, search: str, limit: int = 20) -> list[dict]:
        return HanaLookupReader(self.context).search_items(search, limit=limit)

    def lookup_sac_codes(self, search: str = "", limit: int = 50) -> list[dict]:
        return HanaLookupReader(self.context).sac_codes(search, limit=limit)

    def lookup_locations(self, search: str = "", limit: int = 40) -> list[dict]:
        return HanaLookupReader(self.context).locations(search, limit=limit)

    def lookup_tax_codes(self) -> list[dict]:
        return HanaLookupReader(self.context).tax_codes()

    def lookup_costing_codes(self, dimension: int, search: str = "", limit: int = 100) -> list[dict]:
        return HanaLookupReader(self.context).costing_codes(dimension, search, limit=limit)

    def lookup_branches(self) -> list[dict]:
        return HanaLookupReader(self.context).branches()

    def lookup_resources(self, search: str = "", limit: int = 50) -> list[dict]:
        return HanaLookupReader(self.context).resources(search, limit=limit)

    def lookup_gl_accounts(self, search: str = "", limit: int = 30) -> list[dict]:
        return HanaLookupReader(self.context).gl_accounts(search, limit=limit)

    def lookup_ar_accounts(self) -> list[dict]:
        return HanaLookupReader(self.context).ar_accounts()

    def lookup_ap_accounts(self) -> list[dict]:
        return HanaLookupReader(self.context).ap_accounts()

    def lookup_business_partners(
        self, search: str = "", card_type: str | None = None, limit: int = 30
    ) -> list[dict]:
        return HanaLookupReader(self.context).search_business_partners(
            search, card_type=card_type, limit=limit
        )

    def lookup_bp_groups(self, card_type: str) -> list[dict]:
        return HanaLookupReader(self.context).bp_groups(card_type)

    def lookup_sales_employees(self) -> list[dict]:
        return HanaLookupReader(self.context).sales_employees()

    def lookup_payment_terms(self) -> list[dict]:
        return HanaLookupReader(self.context).payment_terms()

    def lookup_states(self, country: str = "IN") -> list[dict]:
        return HanaLookupReader(self.context).states(country)

    def lookup_user_table(self, key: str) -> tuple[list[dict], str | None]:
        """Rows of ``@MAIN_GROUP`` / ``@CHAIN``, plus a warning if the table is missing."""
        return HanaLookupReader(self.context).user_table_values(key)

    def lookup_banks(self, country: str = "IN") -> list[dict]:
        """Banks of one country from the Service Layer ``Banks`` collection."""
        from .service_layer.entity_client import odata_string

        rows = ServiceLayerEntityClient(self.context).get_all(
            "Banks",
            select="BankCode,BankName,SwiftNo,CountryCode",
            filter=f"CountryCode eq {odata_string((country or 'IN').strip().upper())}",
            orderby="BankName",
        )
        return [
            {
                "code": row.get("BankCode") or "",
                "name": row.get("BankName") or "",
                "swift": row.get("SwiftNo") or "",
                "country": row.get("CountryCode") or "",
            }
            for row in rows
        ]

    # ---- Business partners (registration ported from SAP Portal) ----
    def business_partner(self, card_code: str) -> dict | None:
        """One partner by exact code, whatever its state. Raises if SAP can't be read."""
        return HanaLookupReader(self.context).business_partner(card_code)

    def partners_with_tax_ids(self, card_type: str, gstin: str = "", pan: str = "") -> list[dict]:
        """Existing partners of one type registered under this GSTIN or PAN."""
        return HanaLookupReader(self.context).partners_with_tax_ids(card_type, gstin=gstin, pan=pan)

    def next_card_code(self, prefix: str, card_type: str) -> str:
        """Highest existing card code under ``prefix``, plus one."""
        return HanaLookupReader(self.context).next_card_code(prefix, card_type)

    def create_business_partner(self, payload: dict) -> dict:
        """POST a customer or vendor. The caller locks its row and asks SAP first."""
        return BusinessPartnerWriter(self.context).create(payload)

    # ---- Bills of materials (BOM change requests ported from SAP Portal) ----
    def create_product_tree(self, payload: dict) -> dict:
        return ProductTreeWriter(self.context).create(payload)

    def replace_product_tree(self, tree_code: str, payload: dict) -> dict:
        return ProductTreeWriter(self.context).replace(tree_code, payload)

    # ---- Finance reads (journal entries, chart of accounts, ledgers) ----
    def journal_entries(self, **filters) -> list[dict]:
        """Newest journal entries matching the filters, each with its lines."""
        return HanaFinanceReader(self.context).journal_entries(**filters)

    def chart_of_accounts(self, search: str = "", drawer=None) -> dict:
        """The OACT tree, title accounts rolled up."""
        return HanaFinanceReader(self.context).chart_of_accounts(search=search, drawer=drawer)

    def general_ledger(self, account: str, date_from=None, date_to=None, limit: int = 200) -> dict:
        """Postings to one G/L account or partner, with a running balance."""
        return HanaFinanceReader(self.context).general_ledger(
            account, date_from=date_from, date_to=date_to, limit=limit
        )

    def ledger_account_search(self, search: str, limit: int = 20) -> list[dict]:
        """G/L accounts and partners matching ``search``, for the ledger picker."""
        return HanaFinanceReader(self.context).ledger_account_search(search, limit=limit)

    # ---- Budget UDO (ported from SAP Portal) ----
    def list_budgets(self) -> list[dict]:
        return BudgetWriter(self.context).list()

    def get_budget(self, doc_entry: int) -> dict | None:
        return BudgetWriter(self.context).get(doc_entry)

    def create_budget(self, payload: dict) -> dict:
        return BudgetWriter(self.context).create(payload)

    def update_budget(self, doc_entry: int, payload: dict) -> None:
        BudgetWriter(self.context).update(doc_entry, payload)

    def delete_budget(self, doc_entry: int) -> None:
        BudgetWriter(self.context).delete(doc_entry)

    # ---- Production-order status (ported from SAP Portal) ----
    def release_production_order(self, doc_entry: int) -> None:
        ProductionOrderWriter(self.context).release(doc_entry)

    def close_production_order(self, doc_entry: int) -> None:
        ProductionOrderWriter(self.context).close(doc_entry)

    # ---- Any approval request (the general inbox ported from SAP Portal) ----
    def decide_approval_request(
        self,
        wdd_code: int,
        approve: bool,
        remarks: str = "",
        approver: str | None = None,
        password: str | None = None,
        subject: str = "Document",
    ) -> dict:
        """Approve or reject one request, signed as ``approver``.

        ``password`` is the approver's own SAP password when they typed it;
        without it the stored ``SAP_APPROVER_CREDENTIALS`` entry is used.
        """
        return ApprovalRequestWriter(self.context).decide(
            wdd_code, approve, remarks, approver=approver, subject=subject, password=password
        )

    def withdraw_approval_request(
        self,
        wdd_code: int,
        originator: str,
        password: str | None = None,
        subject: str = "Approval request",
    ) -> dict:
        """Cancel a still-pending request, signed as the person who raised it."""
        return ApprovalRequestWriter(self.context).cancel(
            wdd_code, originator, password=password, subject=subject
        )

    # ---- Attachments (download; ported from SAP Portal) ----
    def download_attachment(self, abs_entry: int, line: int, file_name: str = "") -> dict:
        """One ATC1 attachment file by entry and line, falling back to its name."""
        client = SapFileServiceClient(self.context.company_code)
        try:
            return client.fetch_by_entry(abs_entry, line, file_name)
        except (SAPValidationError, SAPDataError):
            # An older file server without the by-entry route answers 404; the
            # name lookup is the portal's own fallback for that case.
            if not file_name:
                raise
            return client.fetch_by_name(file_name)

    # ---- SAP production orders (ported from SAP Portal) ----
    def list_sap_production_orders(
        self, status: str | None = None, search: str = "", limit: int = 50, offset: int = 0
    ) -> dict:
        """Orders of every status with issued/received totals: ``{count, results}``."""
        return HanaProductionOrderReader(self.context).list_orders(
            status=status, search=search, limit=limit, offset=offset
        )

    def sap_production_order(self, doc_entry: int) -> dict | None:
        """One order with its component lines, issues and receipts."""
        return HanaProductionOrderReader(self.context).order_detail(doc_entry)

    def issue_for_production(self, payload: dict) -> dict:
        """POST InventoryGenExits whose lines consume a production order's components."""
        return IssueForProductionWriter(self.context).create(payload)

    def receipt_from_production(self, payload: dict) -> dict:
        """POST InventoryGenEntries receiving a production order's finished product."""
        return ReceiptFromProductionWriter(self.context).create(payload)

    # ---- SAP documents (ported from SAP Portal) ----
    # The document browser: lists and detail through the Service Layer (the
    # property lists SAP Portal proved live), names and journals from HANA on
    # one connection per screen (sap_client/hana/document_reader.py).
    def list_sap_documents(
        self, entity: str, *, select: str, filter: str = "", orderby: str = "", top: int = 20, skip: int = 0
    ) -> list[dict]:
        """Up to ``top`` rows of a document collection, from row ``skip``.

        The Service Layer pages at 20 rows whatever ``$top`` says, so this asks
        page by page until it has ``top`` rows or SAP runs out.
        """
        client = ServiceLayerEntityClient(self.context)
        top, skip = max(1, int(top)), max(0, int(skip))
        rows: list[dict] = []
        for _ in range(10):
            page, _next = client.get_page(
                entity, select=select, filter=filter, orderby=orderby, top=top - len(rows), skip=skip + len(rows)
            )
            rows.extend(page)
            if not page or len(rows) >= top:
                break
        return rows[:top]

    def get_sap_document(self, entity: str, key: int) -> dict | None:
        """One document by its key (DocEntry; JdtNum for journal entries), or None."""
        return ServiceLayerEntityClient(self.context).get(f"{entity}({int(key)})", not_found_ok=True)

    def sap_document_lookups(self, **request) -> dict:
        """Names, settlement, base documents and journals for one document screen."""
        from .hana.document_reader import HanaDocumentReader

        return HanaDocumentReader(self.context).document_lookups(**request)

    def sap_attachment_lines(self, abs_entry: int) -> list[dict]:
        """The files (ATC1 lines) of one attachment entry."""
        from .hana.document_reader import HanaDocumentReader

        return HanaDocumentReader(self.context).attachment_lines(abs_entry)

    def sap_payment_draft(self, doc_entry: int) -> dict | None:
        """One outgoing-payment draft (OPDF) assembled from HANA, or None."""
        from .hana.document_reader import HanaDocumentReader

        return HanaDocumentReader(self.context).payment_draft(doc_entry)

    # ---- SAP approvals inbox (ported from SAP Portal) ----
    # Imported per method so this port stays one block beside its siblings.
    def list_approval_inbox(self, sap_user_code: str, **filters) -> list[dict]:
        """Approval requests of every type that involve ``sap_user_code``.

        Filters: ``scope`` (waiting_on_me / raised_by_me / all), ``status``,
        ``object_type``, ``date_from``, ``date_to``, ``search``, ``limit``,
        ``offset``.
        """
        from .hana.approval_inbox_reader import HanaApprovalInboxReader

        return HanaApprovalInboxReader(self.context).list_requests(sap_user_code, **filters)

    def count_approval_inbox_waiting(self, sap_user_code: str) -> int:
        """Requests pending at a stage of ``sap_user_code`` (the sidebar badge)."""
        from .hana.approval_inbox_reader import HanaApprovalInboxReader

        return HanaApprovalInboxReader(self.context).waiting_count(sap_user_code)

    def approval_inbox_detail(self, wdd_code: int, sap_user_code: str | None) -> dict | None:
        """One request with its stages and the draft's lines; None if SAP has none."""
        from .hana.approval_inbox_reader import HanaApprovalInboxReader

        return HanaApprovalInboxReader(self.context).detail(wdd_code, sap_user_code)

    def approval_inbox_stage(
        self, wdd_code: int, *, with_duplicates: bool = False, with_item_lines: bool = False
    ) -> dict | None:
        """Any request as a decision or withdraw must judge it, read fresh."""
        from .hana.approval_inbox_reader import HanaApprovalInboxReader

        return HanaApprovalInboxReader(self.context).current_stage(
            wdd_code, with_duplicates=with_duplicates, with_item_lines=with_item_lines
        )

    def verify_approval_signer(
        self, wdd_code: int, approver: str, password: str | None = None
    ) -> str:
        """Log in as ``approver`` and confirm the request is pending; changes nothing."""
        from .service_layer.approval_signer import ApprovalSignerCheck

        return ApprovalSignerCheck(self.context).verify(wdd_code, approver, password=password)

    def set_draft_lines_without_qty_posting(
        self, draft_entry: int, line_nums, without_qty: bool
    ) -> None:
        """SAP's Without Qty Posting on the named lines of a draft."""
        from .service_layer.draft_line_writer import DraftLineWriter

        DraftLineWriter(self.context).set_without_qty_posting(draft_entry, line_nums, without_qty)

    # ---- BOM changes (ported from SAP Portal) ----
    # Reads of OITT/ITT1 for bom_changes; the writes are create_product_tree /
    # replace_product_tree above. Imported here so this block stays one hunk.
    def search_product_trees(self, search: str = "", limit: int = 50) -> list[dict]:
        """Trees whose code or name contains ``search``, with line counts."""
        from .hana.bom_reader import HanaBOMReader

        return HanaBOMReader(self.context).search_trees(search, limit=limit)

    def get_product_tree(self, tree_code: str) -> dict | None:
        """One tree with all its lines, or ``None``. Raises if SAP cannot be read."""
        from .hana.bom_reader import HanaBOMReader

        return HanaBOMReader(self.context).get_tree(tree_code)

    def product_tree_exists(self, tree_code: str) -> bool:
        """Whether SAP holds a tree for ``tree_code``. Raises if it cannot tell."""
        from .hana.bom_reader import HanaBOMReader

        return HanaBOMReader(self.context).tree_exists(tree_code)

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
