"""A/R Invoice writer for the SAP Business One Service Layer.

``create`` POSTs ``/b1s/v2/Invoices`` (OINV). When the Service Layer login is
an originator on an active SAP approval template for A/R invoices (ObjType 13),
SAP holds the post as an ODRF draft with an OWDD approval request — the shared
``_ServiceLayerDocWriter.create`` surfaces that as
``{"pending_approval": True, "draft_entry": N}`` (see the A/P writer for the
mechanics; both families behave identically).

A/R specifics: drafts carry NO batch allocations (verified live — DRF16 is
empty for ObjType 13; batches are picked when the approved draft is added), so
batch-managed lines must be written onto the draft (``patch_draft`` with
``DocumentLines[].BatchNumbers``) before ``save_draft_to_document``. The posted
invoice is read back via ``OINV.draftKey``.
"""
from .delivery_note_writer import _ServiceLayerDocWriter


class ARInvoiceWriter(_ServiceLayerDocWriter):
    endpoint = "Invoices"
    label = "A/R Invoice"
    DOC_OBJECT_CODE = "oInvoices"
