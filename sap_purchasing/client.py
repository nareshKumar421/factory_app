"""One object per SAP company with every purchasing call the handlers make.

The spec (§2.1.6) put these methods on ``sap_client.client.SAPClient``; this
branch may not edit that file, so ``PurchasingClient`` carries them instead,
**with the spec's method names and signatures**, so the cash_sap and
fixed_asset_grpo handlers read the way the spec describes:

    client = PurchasingClient(code)
    found = client.find_document_by_marker("22", vendor, batch.marker)

Each method builds its writer or reader on demand from the company code --
nothing is cached, nothing is read from settings, and no simulate flag is
looked at. Tests patch ``<caller module>.PurchasingClient`` (one object) or
the functions in ``service_layer`` / ``readback`` / ``series`` / ``fa_po_reader``.

The two attachment calls are passed through to ``SAPClient`` unchanged, so a
handler needs only this one client.
"""

from typing import Dict, Iterable, List, Optional, Sequence

from . import fa_po_reader, live_guard, series, service_layer
from .readback import PurchasingReadback


class PurchasingClient:
    """Purchasing writes and read-backs for one SAP company (``company_code``)."""

    def __init__(self, company_code: str):
        self.company_code = company_code

    # -- writers (service_layer) ---------------------------------------------

    def create_purchase_order_document(self, payload: dict) -> dict:
        """POST ``PurchaseOrders``; created JSON or the approval-draft shape."""
        return service_layer.create_purchase_order(self.company_code, payload)

    def close_purchase_order(self, doc_entry: int) -> None:
        """``PurchaseOrders(doc_entry)/Close``."""
        service_layer.close_purchase_order(self.company_code, doc_entry)

    def create_purchase_delivery_note_document(self, payload: dict) -> dict:
        """POST ``PurchaseDeliveryNotes``; created JSON or the approval-draft shape."""
        return service_layer.create_purchase_delivery_note(self.company_code, payload)

    def create_purchase_invoice_draft(self, payload: dict) -> dict:
        """POST ``Drafts`` (oPurchaseInvoices); ``["DocEntry"]`` is the draft entry."""
        return service_layer.create_purchase_invoice_draft(self.company_code, payload)

    def save_purchasing_draft_to_document(self, object_code: str, draft_entry: int) -> None:
        """Add an approved "22" (PO) or "20" (GRPO) draft; other codes -> ``ValueError``."""
        service_layer.save_draft_to_document(self.company_code, object_code, draft_entry)

    # -- read-back (readback) ------------------------------------------------

    def _readback(self) -> PurchasingReadback:
        return PurchasingReadback(self.company_code)

    def find_document_by_marker(self, object_code, card_code, marker) -> Optional[dict]:
        return self._readback().find_by_marker(object_code, card_code, marker)

    def find_document_by_draft_key(self, object_code, draft_entry) -> Optional[dict]:
        return self._readback().find_by_draft_key(object_code, draft_entry)

    def find_draft_by_marker(self, object_code, card_code, marker) -> Optional[dict]:
        return self._readback().find_draft_by_marker(object_code, card_code, marker)

    def document_lines(self, object_code, doc_entry) -> List[dict]:
        return self._readback().document_lines(object_code, doc_entry)

    def document_state(self, object_code, doc_entry) -> Optional[dict]:
        return self._readback().document_state(object_code, doc_entry)

    def num_at_card_taken(self, object_code, card_code, num_at_card) -> bool:
        return self._readback().num_at_card_taken(object_code, card_code, num_at_card)

    def item_flags(self, item_codes: Iterable[str]) -> Dict[str, dict]:
        return self._readback().item_flags(item_codes)

    # -- series, POs, guard --------------------------------------------------

    def resolve_series_for_branch(self, object_code, posting_date, bpl_id=None,
                                  name_hint: str = "") -> dict:
        return series.resolve_series_for_branch(
            self.company_code, object_code, posting_date, bpl_id=bpl_id, name_hint=name_hint)

    def get_open_fixed_asset_pos(self, supplier_code: str,
                                 warehouses: Optional[Sequence[str]] = None):
        return fa_po_reader.get_open_fixed_asset_pos(self.company_code, supplier_code, warehouses)

    def is_test_target(self) -> bool:
        return live_guard.is_test_target(self.company_code)

    # -- attachments (passed through to SAPClient) ---------------------------

    def upload_attachment(self, file_path: str, filename: str, *,
                          allow_metadata_fallback: bool = False) -> dict:
        from sap_client.client import SAPClient

        return SAPClient(self.company_code).upload_attachment(
            file_path, filename, allow_metadata_fallback=allow_metadata_fallback)

    def add_line_to_existing_attachment(self, absolute_entry: int, file_path: str,
                                        filename: str, *,
                                        allow_metadata_fallback: bool = False) -> dict:
        from sap_client.client import SAPClient

        return SAPClient(self.company_code).add_line_to_existing_attachment(
            absolute_entry, file_path, filename, allow_metadata_fallback=allow_metadata_fallback)
