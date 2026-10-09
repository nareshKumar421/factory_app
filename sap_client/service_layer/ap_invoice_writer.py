"""A/P Invoice writer for the SAP Business One Service Layer.

``create`` POSTs ``/b1s/v2/PurchaseInvoices`` (OPCH). When the Service Layer
login is an originator on an active SAP approval template for A/P invoices
(ObjType 18), SAP does NOT post the document: it saves an ODRF draft, opens an
OWDD approval request and answers with a ``Location: .../Drafts(N)`` header
(SAP Note 3066294). The shared ``_ServiceLayerDocWriter.create`` surfaces that
as ``{"pending_approval": True, "draft_entry": N}`` instead of an error.

Once the request is approved, the inherited ``save_draft_to_document`` turns
the draft into the real OPCH invoice; read it back via ``OPCH.draftKey``.
"""
from .delivery_note_writer import _ServiceLayerDocWriter


class APInvoiceWriter(_ServiceLayerDocWriter):
    endpoint = "PurchaseInvoices"
    label = "A/P Invoice"
    DOC_OBJECT_CODE = "oPurchaseInvoices"


class APInvoiceDraftWriter(_ServiceLayerDocWriter):
    """Saves an A/P invoice as a SAP draft (``ODRF``, object 18) -- never posts it.

    ``POST /b1s/v2/Drafts`` with ``DocObjectCode: oPurchaseInvoices`` answers
    201 with the draft's DocEntry. Nothing is booked and no approval procedure
    runs: those happen when someone adds the draft in SAP. Used by
    ``ap_invoice_draft``, where the warehouse prepares the invoice and accounts
    adds it.
    """
    endpoint = "Drafts"
    label = "A/P Invoice draft"
    DOC_OBJECT_CODE = "oPurchaseInvoices"

    def create(self, payload: dict) -> dict:
        return super().create({**payload, "DocObjectCode": self.DOC_OBJECT_CODE})
