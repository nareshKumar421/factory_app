"""Issue for production and receipt from production, against a SAP order.

Ported from SAP Portal's ``/api/sap/issue-production`` and
``/receipt-production`` (``backend_v1/routes/sap.js`` ~1176–1290). Both are
ordinary inventory documents whose lines point back at the production order
(``BaseType 202``): an issue (``InventoryGenExits``) consumes a component line
(``BaseLine`` = ``WOR1.LineNum``); a receipt (``InventoryGenEntries``) brings
the finished product in (SAP derives the item from the order, so no base line).
The payloads are built by ``production_execution.services.sap_order_service``.

What the portal did after a receipt and this does not: it set the line's
Complete/Reject transaction type by updating SAP's ``IGN1`` table directly.
JI never writes SAP tables, so a receipt posted here is SAP's default
(Complete). See ``sap_client/docs/sap_portal_port.md``.

Both subclass ``_ServiceLayerDocWriter`` so a document SAP routes into an
approval procedure comes back as ``{"pending_approval": True, "draft_entry": N}``
instead of an error.
"""

from .delivery_note_writer import _ServiceLayerDocWriter


class IssueForProductionWriter(_ServiceLayerDocWriter):
    """POST /b1s/v2/InventoryGenExits with BaseType 202 lines."""

    endpoint = "InventoryGenExits"
    label = "Issue for production"


class ReceiptFromProductionWriter(_ServiceLayerDocWriter):
    """POST /b1s/v2/InventoryGenEntries with BaseType 202 lines."""

    endpoint = "InventoryGenEntries"
    label = "Receipt from production"
