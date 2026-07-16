from typing import List, Optional
from .context import CompanyContext
from .hana.grpo_reader import HanaGRPOReader
from .hana.po_reader import HanaPOReader
from .hana.service_grpo_options_reader import HanaServiceGRPOOptionsReader
from .hana.stock_transfer_reader import HanaStockTransferReader
from .hana.warehouse_reader import HanaWarehouseReader
from .hana.vendor_reader import HanaVendorReader
from .service_layer.ap_invoice_writer import APInvoiceWriter
from .service_layer.delivery_note_writer import DeliveryNoteWriter, GoodsIssueWriter
from .service_layer.grpo_writer import GRPOWriter
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

    def get_open_po_by_number(self, po_number: str) -> Optional[PODTO]:
        reader = HanaPOReader(self.context)
        return reader.get_open_po_by_number(po_number)

    def get_po_date_by_doc_entry(self, doc_entry: int):
        reader = HanaPOReader(self.context)
        return reader.get_po_date_by_doc_entry(doc_entry)

    def get_active_warehouses(self) -> List[WarehouseDTO]:
        reader = HanaWarehouseReader(self.context)
        return reader.get_active_warehouses()

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

    def get_service_grpo_options(self) -> dict:
        reader = HanaServiceGRPOOptionsReader(self.context)
        return reader.get_options()

    # ---- WRITE ----
    def create_production_order(self, payload: dict) -> dict:
        writer = ProductionOrderWriter(self.context)
        return writer.create(payload)

    def create_grpo(self, payload: dict):
        self.grpo_writer = GRPOWriter(self.context)
        return self.grpo_writer.create(payload)

    def create_ap_invoice(self, payload: dict):
        writer = APInvoiceWriter(self.context)
        return writer.create(payload)

    def create_delivery_note(self, payload: dict) -> dict:
        """Create an outbound Delivery Note (decrements FG stock)."""
        writer = DeliveryNoteWriter(self.context)
        return writer.create(payload)

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
