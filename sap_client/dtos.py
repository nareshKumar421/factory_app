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


@dataclass
class PODTO:
    po_number: str
    supplier_code: str
    supplier_name: str
    items: List[POItemDTO]
    doc_entry: int = 0
    branch_id: Optional[int] = None
    vendor_ref: str = ""


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
    round_dif: Optional[float] = None
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
