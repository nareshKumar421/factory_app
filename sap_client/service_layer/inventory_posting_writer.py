"""Inventory Posting: set SAP's stock of items to what was counted.

``POST /b1s/v2/InventoryPostings`` (SAP's Inventory Counting -> Posting, table
OIQR). Each line gives the counted quantity of an item in a warehouse, and for
a batch item the counted quantity of each batch that changes; SAP posts the
difference against what it holds. Used by the stock audit
(``stock_audit.services.post_to_sap``) once an audit is approved.

SAP answers with ``DocumentEntry`` / ``DocumentNumber`` rather than the
``DocEntry`` / ``DocNum`` of a marketing document; the caller reads both.
"""
from .delivery_note_writer import _ServiceLayerDocWriter


class InventoryPostingWriter(_ServiceLayerDocWriter):
    """POST /b1s/v2/InventoryPostings."""
    endpoint = "InventoryPostings"
    label = "Inventory posting"
