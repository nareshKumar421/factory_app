"""SAP's A/R credit note, printed on the same sheet as the invoice it credits.

A credit note is an ``ORIN`` document with ``RIN1`` lines and ``RIN4`` taxes —
structurally the A/R invoice with different table names — so this reader is
:class:`~sap_client.hana.ar_invoice_print_reader.HanaARInvoicePrintReader` with
those names changed and nothing else. That is deliberate: the customer reads the
credit note beside the bill it reverses, and the two only reconcile if the HSN,
the box/loose split, the letterhead and SAP's own tax-label blemish come out
identical on both.

What the credit note does NOT share is what may be printed at all, which is why
this class guards the read:

* **A cancelled credit note has no sheet.** SAP voids by cancelling, and the
  layout carries nothing that says so — a voided credit note reprinted on it
  reads as live money owed back to the customer. Refused, as the cash-sale
  print refuses a cancelled bill.

* **A service credit note has no item grid.** ``ORIN.DocType`` is ``'S'`` for
  one raised against a G/L account (roughly a third of them): no item, no
  warehouse, no quantity. Printed on this sheet it comes out as a letterhead
  over an empty grid with a total under it, so it is refused here rather than
  printed as a document that appears to credit nothing.

The A/P credit note (``ORPC``, a vendor being debited) is not printable here at
all and needs no guard: it is not in ``ORIN``, so the read simply finds nothing.
"""

import logging
from typing import Optional

from .ar_invoice_print_reader import HanaARInvoicePrintReader
from ..exceptions import SAPValidationError

logger = logging.getLogger(__name__)


class HanaCreditNotePrintReader(HanaARInvoicePrintReader):
    """One posted A/R credit note, shaped for the same print as the invoice."""

    HEADER_TABLE = "ORIN"
    LINE_TABLE = "RIN1"
    TAX_TABLE = "RIN4"
    BATCH_BASE_TYPE = "14"
    EDOC_DOC_TYPE = 14

    def document_print(self, doc_entry: int) -> Optional[dict]:
        """The credit note as data, or ``None`` when the company has no such one.

        Raises :class:`SAPValidationError` for a credit note that exists but has
        no sheet — cancelled, or a service document — because "there is nothing
        to print" and "this must not be printed" need different words in front
        of whoever pressed the button.
        """
        doc_entry = int(doc_entry)

        state = self.credit_note_state(doc_entry)
        if state is None:
            return None

        label = state["doc_num"] or doc_entry
        if state["is_cancelled"]:
            raise SAPValidationError(
                f"Credit note {label} was cancelled in SAP, so there is no sheet to print."
            )
        if not state["is_item"]:
            raise SAPValidationError(
                f"Credit note {label} is a service credit note — it credits a G/L "
                f"account rather than goods, and this sheet prints item lines."
            )

        return super().document_print(doc_entry)

    def credit_note_state(self, doc_entry: int) -> Optional[dict]:
        """``DocNum``, cancelled and item/service for one ``ORIN`` entry.

        A separate read rather than three more columns on the header query,
        which is shared with the invoice print: the invoice sheet has no use for
        them, and a credit note has to be refused before that query runs.
        """
        rows = self._query(
            """
            SELECT H."DocNum", IFNULL(H."CANCELED", 'N'), IFNULL(H."DocType", 'I')
            FROM "{schema}"."ORIN" H
            WHERE H."DocEntry" = ?
            """,
            (int(doc_entry),),
        )
        if not rows:
            return None
        doc_num, canceled, doc_type = rows[0]
        return {
            "doc_entry": int(doc_entry),
            "doc_num": int(doc_num) if doc_num is not None else None,
            "is_cancelled": (canceled or "N") == "Y",
            "is_item": (doc_type or "I") == "I",
        }
