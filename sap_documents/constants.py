"""The document types the browser opens — SAP Portal's ``DOC_TYPES`` whitelist.

``backend_v1/routes/sap.js:1402-1425``. The ``select`` lists are the portal's,
corrected against live SAP there (``StornoToDate`` → ``StornoDate`` on journal
entries; no ``CheckSum``/``DocTotal`` on payments; ``DocObjectCode``, not
``ObjType``, on drafts — 9d7f0c7). One addition: ``Cancelled`` on the eight
posted marketing documents, so a cancelled row reads "Cancelled" rather than
"Closed" (SAP closes a document when it cancels it).

Anything not in ``DOCUMENT_TYPES`` is refused before SAP is asked: the type
becomes a Service Layer entity name in the request path.
"""

from dataclasses import dataclass, field

_MARKETING = (
    "DocEntry,DocNum,DocDate,DocDueDate,CardCode,CardName,DocTotal,DocCurrency,Comments,"
    "DocumentStatus,BPL_IDAssignedToInvoice,AttachmentEntry"
)
_MARKETING_LIST = f"{_MARKETING},Cancelled"
_PAYMENT = (
    "DocEntry,DocNum,DocDate,CardCode,CardName,DocCurrency,CashSum,TransferSum,Remarks,"
    "JournalRemarks,TransferAccount,TransferDate,TransferReference,AuthorizationStatus,BPLID,BPLName,"
    "AttachmentEntry"
)

# Status filter codes (the portal's O / C / L) → a label.
STATUS_LABELS = {"O": "Open", "C": "Closed", "L": "Cancelled"}

MARKETING, DRAFT, TRANSFER, JOURNAL, PAYMENT, PAYMENT_DRAFT = (
    "marketing",
    "draft",
    "transfer",
    "journal",
    "payment",
    "payment_draft",
)


@dataclass(frozen=True)
class DocumentType:
    """One Service Layer document collection the browser can list and open."""

    key: str
    label: str
    group: str
    kind: str
    select: str
    object_type: str = ""
    purchase: bool = False
    line_table: str | None = None
    header_table: str | None = None
    tds_table: str | None = None
    statuses: tuple = field(default=())

    @property
    def is_journal(self) -> bool:
        return self.kind == JOURNAL

    @property
    def key_field(self) -> str:
        return "JdtNum" if self.is_journal else "DocEntry"

    @property
    def number_field(self) -> str:
        return "Number" if self.is_journal else "DocNum"

    @property
    def date_field(self) -> str:
        return "ReferenceDate" if self.is_journal else "DocDate"

    @property
    def has_partner(self) -> bool:
        return not self.is_journal

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "object_type": self.object_type,
            "filters": {
                "number": "Entry no." if self.is_journal else "Document no.",
                "partner": self.has_partner,
                "statuses": [{"value": code, "label": STATUS_LABELS[code]} for code in self.statuses],
            },
        }


DOCUMENT_TYPES: dict[str, DocumentType] = {
    t.key: t
    for t in (
        DocumentType(
            "PurchaseOrders", "Purchase Order", "Purchase", MARKETING, _MARKETING_LIST, "22", True,
            "POR1", "OPOR", None, ("O", "C", "L"),
        ),
        DocumentType(
            "PurchaseDeliveryNotes", "GRPO", "Purchase", MARKETING, _MARKETING_LIST, "20", True,
            "PDN1", "OPDN", None, ("O", "C", "L"),
        ),
        DocumentType(
            "PurchaseInvoices", "AP Invoice", "Purchase", MARKETING, _MARKETING_LIST, "18", True,
            "PCH1", "OPCH", "PCH5", ("O", "C", "L"),
        ),
        DocumentType(
            "PurchaseCreditNotes", "AP Credit Note", "Purchase", MARKETING, _MARKETING_LIST, "19", True,
            "RPC1", "ORPC", "RPC5", ("O", "C", "L"),
        ),
        DocumentType(
            "PurchaseReturns", "Goods Return", "Purchase", MARKETING, _MARKETING_LIST, "21", True,
            "RPD1", "ORPD", None, ("O", "C", "L"),
        ),
        DocumentType(
            "Invoices", "AR Invoice", "Sales", MARKETING, _MARKETING_LIST, "13", False,
            "INV1", "OINV", "INV5", ("O", "C", "L"),
        ),
        DocumentType(
            "CreditNotes", "AR Credit Memo", "Sales", MARKETING, _MARKETING_LIST, "14", False,
            "RIN1", "ORIN", "RIN5", ("O", "C", "L"),
        ),
        DocumentType(
            "Returns", "Return Notes", "Sales", MARKETING, _MARKETING_LIST, "16", False,
            "RDN1", "ORDN", None, ("O", "C", "L"),
        ),
        # Warehouse-to-warehouse documents carry no partner total; the route
        # (from → to warehouse) is what identifies them.
        DocumentType(
            "StockTransfers", "Inventory Transfer", "Inventory", TRANSFER,
            "DocEntry,DocNum,DocDate,DueDate,CardCode,CardName,FromWarehouse,ToWarehouse,Comments,"
            "JournalMemo,PriceList,AttachmentEntry",
            "67", False, "WTR1",
        ),
        DocumentType(
            "InventoryTransferRequests", "Inventory Transfer Request", "Inventory", TRANSFER,
            "DocEntry,DocNum,DocDate,DueDate,CardCode,CardName,FromWarehouse,ToWarehouse,Comments,"
            "JournalMemo,DocumentStatus,AttachmentEntry",
            "1250000001", False, "WTQ1", None, None, ("O", "C"),
        ),
        DocumentType(
            "JournalEntries", "Journal Entry", "Finance", JOURNAL,
            "JdtNum,Number,ReferenceDate,DueDate,TaxDate,Memo,Reference,Reference2,StornoDate,"
            "TransactionCode,ProjectCode",
            "30",
        ),
        DocumentType("VendorPayments", "Outgoing Payment", "Finance", PAYMENT, _PAYMENT, "46", True),
        # Documents held for approval. A draft's own object type (DocObjectCode)
        # says what it will become.
        DocumentType(
            "Drafts", "Draft", "Drafts", DRAFT,
            "DocEntry,DocNum,DocDate,DocDueDate,CardCode,CardName,DocTotal,DocCurrency,Comments,"
            "DocumentStatus,AttachmentEntry,DocObjectCode",
            "", False, "DRF1", "ODRF", "DRF5", ("O", "C"),
        ),
        DocumentType("PaymentDrafts", "Outgoing Payment Draft", "Drafts", PAYMENT_DRAFT, _PAYMENT, "46", True),
    )
}

# SAP's DocObjectCode (Service Layer) or ObjType (HANA) → a label, for drafts.
OBJECT_CODE_LABELS = {
    "oInvoices": "AR Invoice",
    "oCreditNotes": "AR Credit Memo",
    "oDeliveryNotes": "Delivery",
    "oReturns": "Return",
    "oOrders": "Sales Order",
    "oQuotations": "Sales Quotation",
    "oDownPayments": "AR Down Payment",
    "oPurchaseInvoices": "AP Invoice",
    "oPurchaseCreditNotes": "AP Credit Note",
    "oPurchaseDeliveryNotes": "Goods Receipt PO",
    "oPurchaseReturns": "Goods Return",
    "oPurchaseOrders": "Purchase Order",
    "oPurchaseQuotations": "Purchase Quotation",
    "oPurchaseRequest": "Purchase Request",
    "oPurchaseDownPayments": "AP Down Payment",
    "oStockTransfer": "Inventory Transfer",
    "oInventoryTransferRequest": "Inventory Transfer Request",
    "oInventoryGenEntry": "Goods Receipt",
    "oInventoryGenExit": "Goods Issue",
}
OBJECT_CODE_TYPES = {
    "oInvoices": "13", "oCreditNotes": "14", "oDeliveryNotes": "15", "oReturns": "16", "oOrders": "17",
    "oQuotations": "23", "oPurchaseInvoices": "18", "oPurchaseCreditNotes": "19",
    "oPurchaseDeliveryNotes": "20", "oPurchaseReturns": "21", "oPurchaseOrders": "22",
    "oPurchaseRequest": "540000006", "oStockTransfer": "67", "oInventoryTransferRequest": "1250000001",
    "oInventoryGenEntry": "59", "oInventoryGenExit": "60",
}

# Purchase documents ship FROM the counterparty (portal 1866-1872). The Service
# Layer reports DocObjectCode, HANA the numeric ObjType.
PURCHASE_OBJECTS = frozenset(
    {
        "oPurchaseInvoices", "oPurchaseCreditNotes", "oPurchaseDeliveryNotes", "oPurchaseReturns",
        "oPurchaseOrders", "18", "19", "20", "21", "22",
    }
)

# SAP's DocumentSubType values that mean something on an Indian invoice.
DOCUMENT_SUBTYPES = {
    "bod_GSTTaxInvoice": "GST Tax Invoice",
    "bod_BillOfSupply": "Bill of Supply",
    "bod_ExportInvoice": "Export Invoice",
    "bod_DeliveryChallan": "Delivery Challan",
    "bod_SEZInvoice": "SEZ Invoice",
}

# The journal behind an A/P invoice's goods receipt, or an A/R invoice's
# delivery: SAP Portal's "in transit" entry (inTransitTransTypeForDocument).
IN_TRANSIT_BASE_TYPES = {"18": "20", "19": "20", "13": "15", "14": "15"}

# A payment's settled documents: Service Layer InvoiceType → SAP object type.
PAYMENT_INVOICE_TYPES = {
    "it_Invoice": "13",
    "it_CredItnote": "14",
    "it_PurchaseInvoice": "18",
    "it_PurchaseCreditNote": "19",
    "it_Receipt": "24",
    "it_PaymentAdvice": "46",
    "it_JournalEntry": "30",
}

# Files a browser may show in a tab. Everything else downloads: an HTML or SVG
# attachment opened inline from this origin would run its scripts here.
INLINE_CONTENT_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "text/plain", "text/csv"}
)
