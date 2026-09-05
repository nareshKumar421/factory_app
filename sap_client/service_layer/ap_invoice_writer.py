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
