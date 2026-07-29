from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List

from django.utils import timezone
from django.utils.dateparse import parse_date

from dispatch_plans.models import DispatchPlan
from dispatch_plans.serializers import DispatchPlanSerializer
from dispatch_plans.services import DispatchPlansService
from sap_client.client import SAPClient

from gate_core.models import SalesDispatchDocumentType


class SalesDispatchDocumentService:
    """Returns normalized SAP documents that can be docked."""

    def __init__(self, company):
        self.company = company
        self.company_code = company.code

    def list_documents(self, document_type: str, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        normalized_type = self.normalize_document_type(document_type, allow_all=True)
        if normalized_type == "ALL":
            return [
                *self._list_invoices(filters),
                *self._list_stock_transfers(filters),
            ]
        if normalized_type == SalesDispatchDocumentType.INVOICE:
            return self._list_invoices(filters)
        return self._list_stock_transfers(filters)

    def get_document(self, document_type: str, doc_entry: int) -> Dict[str, Any] | None:
        normalized_type = self.normalize_document_type(document_type)
        if normalized_type == SalesDispatchDocumentType.INVOICE:
            return self._get_invoice(doc_entry)
        return self._get_stock_transfer(doc_entry)

    @staticmethod
    def normalize_document_type(document_type: str, allow_all: bool = False) -> str:
        value = (document_type or "").strip().upper()
        aliases = {
            "AR_INVOICE": SalesDispatchDocumentType.INVOICE,
            "OINV": SalesDispatchDocumentType.INVOICE,
            "INVOICE": SalesDispatchDocumentType.INVOICE,
            "STOCK_TRANSFER": SalesDispatchDocumentType.STOCK_TRANSFER,
            "OWTR": SalesDispatchDocumentType.STOCK_TRANSFER,
            "TRANSFER": SalesDispatchDocumentType.STOCK_TRANSFER,
        }
        if allow_all and value in ("", "ALL"):
            return "ALL"
        if value in aliases:
            return aliases[value]
        raise ValueError("document_type must be INVOICE, STOCK_TRANSFER, or ALL.")

    def _list_invoices(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        service = DispatchPlansService(self.company_code)
        invoice_doc_num = str(filters.get("invoice_doc_num") or "").strip()
        if invoice_doc_num:
            # Exact SAP DocNum lookup. The caller (e.g. the BST page) already has
            # the full invoice number, so match on it directly instead of the
            # date-windowed list. This bypasses both the default 30-day window and
            # the row cap, so any invoice is findable by its number, however old.
            bill_filters = {
                "invoice_doc_num": invoice_doc_num,
                "branch": filters.get("branch", ""),
                "booking_status": filters.get("booking_status", "all"),
            }
        else:
            bill_filters = {
                "date_from": filters.get("from_date") or self._default_from_date(),
                "date_to": filters.get("to_date") or self._default_to_date(),
                "branch": filters.get("branch", ""),
                "search": filters.get("search", ""),
                "booking_status": filters.get("booking_status", "all"),
                "limit": filters.get("limit", 100),
            }
        result = service.get_bills(bill_filters)
        return [self._normalize_invoice(row) for row in result["data"]]

    def _get_invoice(self, doc_entry: int) -> Dict[str, Any] | None:
        reader = DispatchPlansService(self.company_code).reader
        rows = reader.list_bills_by_doc_entries([doc_entry])
        if not rows:
            return None
        invoice = rows[0]
        invoice["items"] = reader.list_bill_lines(doc_entry)
        plan = DispatchPlan.objects.filter(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            is_active=True,
        ).first()
        invoice["plan"] = DispatchPlanSerializer(plan).data if plan else None
        return self._normalize_invoice(invoice)

    def _list_stock_transfers(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        client = SAPClient(company_code=self.company_code)
        rows = client.list_stock_transfers(
            search=filters.get("search"),
            from_date=parse_date(filters.get("from_date") or ""),
            to_date=parse_date(filters.get("to_date") or ""),
            limit=int(filters.get("limit") or 100),
        )
        return [self._normalize_stock_transfer(row) for row in rows]

    def _get_stock_transfer(self, doc_entry: int) -> Dict[str, Any] | None:
        client = SAPClient(company_code=self.company_code)
        row = client.get_stock_transfer(doc_entry)
        if not row:
            return None
        return self._normalize_stock_transfer(row)

    @staticmethod
    def _default_from_date() -> str:
        return (timezone.localdate() - timedelta(days=30)).isoformat()

    @staticmethod
    def _default_to_date() -> str:
        return timezone.localdate().isoformat()

    @staticmethod
    def _decimal(value: Any, places: str = "0.001") -> Decimal | None:
        if value in (None, ""):
            return None
        return Decimal(str(value)).quantize(Decimal(places))

    def _normalize_invoice(self, row: Dict[str, Any]) -> Dict[str, Any]:
        plan = row.get("plan")
        return {
            "document_type": SalesDispatchDocumentType.INVOICE,
            "doc_entry": int(row["doc_entry"]),
            "doc_num": row.get("doc_num", ""),
            "doc_date": row.get("doc_date"),
            "doc_total": row.get("doc_total"),
            "branch_id": row.get("branch_id"),
            "branch_name": row.get("branch_name", ""),
            "card_code": row.get("card_code", ""),
            "card_name": row.get("card_name", ""),
            "ship_to_code": row.get("ship_to_code", ""),
            "ship_to_address": row.get("ship_to_address", ""),
            "place_of_supply": row.get("state") or row.get("city") or "",
            "bp_gstin": row.get("bp_gstin", ""),
            "eway_bill": row.get("sap_eway_bill", ""),
            "vehicle_no": row.get("sap_vehicle_no") or row.get("gst_vehicle_no") or "",
            "transporter_name": row.get("sap_transporter_name", ""),
            "bilty_no": row.get("sap_bilty_no", ""),
            "bilty_date": row.get("sap_bilty_date"),
            "from_warehouse": "",
            "to_warehouse": "",
            "warehouses": row.get("warehouses", ""),
            "item_summary": row.get("item_summary", ""),
            "base_refs": row.get("base_refs", ""),
            "total_quantity": row.get("total_quantity"),
            "total_litres": row.get("total_litres"),
            "total_boxes": row.get("total_boxes"),
            "total_weight": row.get("total_weight"),
            "line_count": row.get("line_count", 0),
            "items": row.get("items", []),
            "plan": plan,
        }

    def _normalize_stock_transfer(self, row: Dict[str, Any]) -> Dict[str, Any]:
        lines = row.get("lines", []) or []
        item_summary = ", ".join(
            f"{line.get('item_code', '')} - {line.get('item_name', '')}".strip(" -")
            for line in lines[:10]
        )
        warehouses = ", ".join(
            sorted(
                {
                    value
                    for line in lines
                    for value in (line.get("from_warehouse"), line.get("to_warehouse"))
                    if value
                }
            )
        )
        return {
            "document_type": SalesDispatchDocumentType.STOCK_TRANSFER,
            "doc_entry": int(row["doc_entry"]),
            "doc_num": str(row.get("doc_num") or ""),
            "doc_date": row.get("doc_date"),
            "doc_total": None,
            "branch_id": row.get("branch_id"),
            "branch_name": "",
            "card_code": "",
            "card_name": "",
            "ship_to_code": "",
            "ship_to_address": "",
            "place_of_supply": "",
            "bp_gstin": "",
            "eway_bill": "",
            "vehicle_no": "",
            "transporter_name": "",
            "bilty_no": "",
            "bilty_date": None,
            "from_warehouse": row.get("from_warehouse", ""),
            "to_warehouse": row.get("to_warehouse", ""),
            "warehouses": warehouses,
            "item_summary": item_summary,
            "base_refs": row.get("reference", ""),
            "sap_reference": row.get("reference", ""),
            "sap_comments": row.get("comments", ""),
            "total_quantity": row.get("total_quantity"),
            "total_litres": None,
            "total_boxes": None,
            "total_weight": None,
            "line_count": row.get("line_count", len(lines)),
            "items": lines,
            "plan": None,
        }

    @staticmethod
    def iter_items(document: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        for index, item in enumerate(document.get("items") or []):
            yield {
                "line_num": int(item.get("line_num", index) or 0),
                "item_code": item.get("item_code", ""),
                "item_name": item.get("item_name", ""),
                "quantity": SalesDispatchDocumentService._decimal(
                    item.get("quantity") or 0
                ) or Decimal("0.000"),
                "uom": item.get("uom", ""),
                "rate": SalesDispatchDocumentService._decimal(item.get("rate"), "0.0001"),
                "line_total": SalesDispatchDocumentService._decimal(
                    item.get("line_total"), "0.01"
                ),
                "gross_total": SalesDispatchDocumentService._decimal(
                    item.get("gross_total"), "0.01"
                ),
                "warehouse_code": item.get("warehouse_code", ""),
                "from_warehouse": item.get("from_warehouse", ""),
                "to_warehouse": item.get("to_warehouse", ""),
                "base_ref": item.get("base_ref", ""),
                "base_entry": item.get("base_entry"),
                "base_type": item.get("base_type"),
                "tax_code": item.get("tax_code", ""),
                "total_litres": SalesDispatchDocumentService._decimal(
                    item.get("total_litres")
                ),
                "total_boxes": SalesDispatchDocumentService._decimal(
                    item.get("total_boxes")
                ),
                "total_weight": SalesDispatchDocumentService._decimal(
                    item.get("total_weight")
                ),
            }
