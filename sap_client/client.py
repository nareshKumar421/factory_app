from typing import List, Optional, Dict, Any
from .context import CompanyContext
from .hana.po_reader import HanaPOReader
from .hana.warehouse_reader import HanaWarehouseReader
from .hana.vendor_reader import HanaVendorReader
from .hana.sales_order_reader import HanaSalesOrderReader
from .service_layer.grpo_writer import GRPOWriter
from .service_layer.goods_issue_writer import GoodsIssueWriter
from .service_layer.attachment_writer import AttachmentWriter
from .service_layer.production_order_writer import ProductionOrderWriter
from .dtos import PODTO, WarehouseDTO, VendorDTO


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

    def get_active_warehouses(self) -> List[WarehouseDTO]:
        reader = HanaWarehouseReader(self.context)
        return reader.get_active_warehouses()

    def get_active_vendors(self) -> List[VendorDTO]:
        reader = HanaVendorReader(self.context)
        return reader.get_active_vendors()

    # ---- WRITE ----
    def create_production_order(self, payload: dict) -> dict:
        writer = ProductionOrderWriter(self.context)
        return writer.create(payload)

    def create_grpo(self, payload: dict):
        self.grpo_writer = GRPOWriter(self.context)
        return self.grpo_writer.create(payload)

    def upload_attachment(self, file_path: str, filename: str) -> dict:
        """Upload a file to SAP Attachments2"""
        writer = AttachmentWriter(self.context)
        return writer.upload(file_path, filename)

    def get_grpo_attachment_entry(self, doc_entry: int) -> Optional[int]:
        """Get the existing AttachmentEntry from a GRPO document"""
        writer = AttachmentWriter(self.context)
        return writer.get_document_attachment_entry(doc_entry)

    def add_line_to_existing_attachment(
        self, absolute_entry: int, file_path: str, filename: str
    ) -> dict:
        """Add a new file line to an existing Attachments2 entry"""
        writer = AttachmentWriter(self.context)
        return writer.add_line_to_existing_attachment(
            absolute_entry, file_path, filename
        )

    def link_attachment_to_grpo(self, doc_entry: int, absolute_entry: int) -> dict:
        """Link an attachment to a GRPO document"""
        writer = AttachmentWriter(self.context)
        return writer.link_to_document(doc_entry, absolute_entry)

    # ---- OUTBOUND ----
    def get_open_sales_orders(self, filters: Dict[str, Any] = None) -> List[Dict]:
        """Get open Sales Orders from HANA for outbound dispatch"""
        reader = HanaSalesOrderReader(self.context)
        return reader.get_open_sales_orders(filters)

    def create_goods_issue(self, payload: dict) -> dict:
        """Create Goods Issue (Inventory Gen Exit) in SAP"""
        writer = GoodsIssueWriter(self.context)
        return writer.create(payload)
