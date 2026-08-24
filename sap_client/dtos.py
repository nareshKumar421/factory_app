from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class POItemDTO:
    po_item_code: str
    item_name: str
    ordered_qty: float
    received_qty: float
    remaining_qty: float
    uom: str
    rate: float = 0.0
    line_num: int = 0
    tax_code: str = ""
    warehouse_code: str = ""
    account_code: str = ""
    variety: str = ""


@dataclass
class POAdditionalExpenseDTO:
    """One PO additional-expense (freight) line — SAP POR3 joined to OEXD.

    Freight is agreed at purchase time and SAP's own copy-from-PO carries these
    rows onto the GRPO verbatim, so the GRPO screen pre-fills from here instead
    of asking the operator to guess an expense code. Note ``expense_code`` is
    company-scoped: freight-inward-direct is 2 in Oil but 3 in Mart.
    """
    expense_code: int
    expense_name: str
    amount: float
    line_num: int
    tax_code: str = ""
    # Service Layer enum, e.g. "aedm_Quantity" — mirrors POR3.DistrbMthd so the
    # charge lands on item cost the way purchase intended.
    distribution_method: str = ""
    remarks: str = ""
    # 'O' open / 'C' closed on the PO expense line.
    status: str = ""
    # How much of this PO expense line already sits on posted GRPOs (summed from
    # PDN3 by base linkage). POR3.DrawnTotal is not maintained in these company
    # DBs, so this is the only reliable basis for "what is still to be charged".
    posted_amount: float = 0.0
    # amount - posted_amount, floored at 0. What a fresh GRPO should carry.
    remaining_amount: float = 0.0
    expense_account: str = ""
    sac_code: str = ""


@dataclass
class PODTO:
    po_number: str
    supplier_code: str
    supplier_name: str
    items: List[POItemDTO]
    doc_entry: int = 0
    branch_id: Optional[int] = None
    vendor_ref: str = ""
    doc_date: Optional[date] = None
    additional_expenses: List[POAdditionalExpenseDTO] = field(default_factory=list)


@dataclass
class GRPOLineDTO:
    """GRPO Document Line Item"""
    item_code: str
    quantity: float
    tax_code: Optional[str] = None
    unit_price: Optional[float] = None
    base_entry: Optional[int] = None  # PO DocEntry for PO-based GRPO
    base_line: Optional[int] = None   # PO line number
    base_type: Optional[int] = None   # 22 for Purchase Order
    warehouse_code: Optional[str] = None


@dataclass
class GRPORequestDTO:
    """GRPO Document Request"""
    card_code: str
    document_lines: List[GRPOLineDTO]
    doc_date: Optional[str] = None
    doc_due_date: Optional[str] = None
    tax_date: Optional[str] = None
    round_dif: Optional[float] = None  # auto-calculated when should_roundoff=True
    comments: Optional[str] = None


@dataclass
class GRPOResponseDTO:
    """GRPO Document Response from SAP"""
    doc_entry: int
    doc_num: int
    card_code: str
    card_name: Optional[str] = None
    doc_date: Optional[str] = None
    doc_total: Optional[float] = None


@dataclass
class WarehouseDTO:
    """Active Warehouse from SAP"""
    warehouse_code: str
    warehouse_name: str


@dataclass
class VendorDTO:
    """Active Vendor from SAP"""
    vendor_code: str
    vendor_name: str


# ---------------------------------------------------------------------------
# Production Planning DTOs
# ---------------------------------------------------------------------------

@dataclass
class ProductionComponentDTO:
    """BOM component line from SAP WOR1 (production order component)"""
    component_code: str
    component_name: str
    planned_qty: float
    issued_qty: float
    remaining_qty: float
    uom: str


@dataclass
class ItemDTO:
    """Item master record from SAP OITM (for dropdown lists)"""
    item_code: str
    item_name: str
    uom: str = ""
    item_group: str = ""
    make_item: bool = False      # MakeItem='Y' → finished good (can be manufactured)
    purchase_item: bool = False  # PrchseItem='Y' → raw material (can be purchased)


@dataclass
class UoMDTO:
    """Unit of Measure from SAP OUOM"""
    uom_code: str
    uom_name: str


@dataclass
class ProductionOrderDTO:
    """Production order header from SAP OWOR"""
    doc_entry: int
    doc_num: int
    item_code: str
    item_name: str
    planned_qty: float
    completed_qty: float
    rejected_qty: float
    remaining_qty: float
    planned_start_date: date
    due_date: date
    sap_status: str              # 'P'=Planned, 'R'=Released
    customer_code: str
    customer_name: str
    branch_id: Optional[int]
    remarks: str
    components: List[ProductionComponentDTO] = field(default_factory=list)
