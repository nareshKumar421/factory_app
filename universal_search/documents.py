"""
universal_search/documents.py

Which SAP documents a number is looked for in, and where each one keeps its
header and its lines.

Every marketing, purchasing and inventory document in B1 carries the same
header columns -- ``DocEntry``, ``DocNum``, ``DocDate``, ``CardCode``,
``CardName``, ``DocTotal``, ``DocStatus``, ``CANCELED``, ``NumAtCard`` -- and
the same line columns, so one SELECT template covers fourteen of the fifteen
types here (verified against the live schema, not assumed).

The production order is the exception and is marked as such: ``OWOR`` has no
``DocDate``, no ``DocTotal`` and no customer, and its lines (``WOR1``) are
components with their own column names. It gets its own template.

A number is NOT unique across these tables. The same 626090411 can be an
invoice in one company and nothing at all in the next, and a plain 1001 may
genuinely be a purchase order *and* a goods receipt. Every type is therefore
searched and every hit returned -- picking one for the user would be guessing.
"""

from dataclasses import dataclass

#: How a document's header and lines are shaped.
SHAPE_MARKETING = "MARKETING"  # the fourteen that share the standard columns
SHAPE_PRODUCTION = "PRODUCTION"  # OWOR / WOR1


@dataclass(frozen=True)
class SapDocType:
    """One searchable SAP document type."""

    kind: str
    label: str
    #: Header table (OINV) and line table (INV1).
    header: str
    lines: str
    #: SAP's own object type, so a caller can cross-reference OINM/ODRF rows.
    obj_type: str
    #: What the business partner on it is called, for the detail heading.
    partner_label: str = ""
    shape: str = SHAPE_MARKETING


#: Ordered by how often someone standing in this factory holds the number.
DOC_TYPES: tuple[SapDocType, ...] = (
    SapDocType("AR_INVOICE", "A/R Invoice", "OINV", "INV1", "13", "Customer"),
    SapDocType("AR_CREDIT_NOTE", "A/R Credit Note", "ORIN", "RIN1", "14", "Customer"),
    SapDocType("DELIVERY", "Delivery", "ODLN", "DLN1", "15", "Customer"),
    SapDocType("AR_RETURN", "A/R Return", "ORDN", "RDN1", "16", "Customer"),
    SapDocType("SALES_ORDER", "Sales Order", "ORDR", "RDR1", "17", "Customer"),
    SapDocType("PURCHASE_ORDER", "Purchase Order", "OPOR", "POR1", "22", "Vendor"),
    SapDocType("GRPO", "Goods Receipt PO", "OPDN", "PDN1", "20", "Vendor"),
    SapDocType("GOODS_RETURN", "Goods Return", "ORPD", "RPD1", "21", "Vendor"),
    SapDocType("AP_INVOICE", "A/P Invoice", "OPCH", "PCH1", "18", "Vendor"),
    SapDocType("AP_CREDIT_NOTE", "A/P Credit Note", "ORPC", "RPC1", "19", "Vendor"),
    SapDocType("INVENTORY_TRANSFER", "Inventory Transfer", "OWTR", "WTR1", "67"),
    SapDocType("TRANSFER_REQUEST", "Inventory Transfer Request", "OWTQ", "WTQ1", "1250000001"),
    SapDocType("GOODS_RECEIPT", "Goods Receipt", "OIGN", "IGN1", "59"),
    SapDocType("GOODS_ISSUE", "Goods Issue", "OIGE", "IGE1", "60"),
    SapDocType(
        "PRODUCTION_ORDER",
        "Production Order",
        "OWOR",
        "WOR1",
        "202",
        shape=SHAPE_PRODUCTION,
    ),
)

DOC_TYPES_BY_KIND = {doc.kind: doc for doc in DOC_TYPES}

#: SAP's one-letter document status, spelled out for the modal.
DOC_STATUS_LABELS = {
    "O": "Open",
    "C": "Closed",
}

#: OWOR keeps its own status codes in the same column position.
PRODUCTION_STATUS_LABELS = {
    "P": "Planned",
    "R": "Released",
    "L": "Closed",
    "C": "Cancelled",
}


def status_label(doc: SapDocType, code: str) -> str:
    """The status on screen. Unknown codes come back as themselves, not blank."""
    code = (code or "").strip()
    if not code:
        return ""
    table = (
        PRODUCTION_STATUS_LABELS
        if doc.shape == SHAPE_PRODUCTION
        else DOC_STATUS_LABELS
    )
    return table.get(code, code)
