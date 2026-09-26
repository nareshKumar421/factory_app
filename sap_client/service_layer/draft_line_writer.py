"""Set SAP's "Without Qty Posting" on a draft's item lines.

Ported from SAP Portal's credit-note decision (``backend_v1/routes/creditNotes.js``
``PATCH /:code``, the "Without Qty Posting" block): an approver may decide a
credit note should credit the value only and not move stock — a rate
difference, not a goods return. The flag lives on the DRAFT's item lines
(``DRF1.NoInvtryMv``; ``WithoutInventoryMovement`` in the Service Layer), and on
full approval SAP posts the draft exactly as it stands, so it has to be written
before the approval, not after.

A ``PATCH Drafts(N)`` merges ``DocumentLines`` by ``LineNum`` (no
``B1S-ReplaceCollectionsOnPatch``), so only the lines named are touched — the
portal sent only the lines whose flag differed, and so does the caller here.
Sent as the service account, as the portal did: the decision itself is what
SAP must record against the authorizer.
"""

import logging

from .entity_client import ServiceLayerEntityClient

logger = logging.getLogger(__name__)


class DraftLineWriter:
    """PATCH one draft's lines through the Service Layer."""

    def __init__(self, context):
        self.client = ServiceLayerEntityClient(context)

    def set_without_qty_posting(self, draft_entry: int, line_nums, without_qty: bool) -> None:
        """Flag (or clear) Without Qty Posting on ``line_nums`` of draft ``draft_entry``."""
        lines = [
            {
                "LineNum": int(line_num),
                "WithoutInventoryMovement": "tYES" if without_qty else "tNO",
            }
            for line_num in line_nums
        ]
        if not lines:
            return
        self.client.patch(
            f"Drafts({int(draft_entry)})",
            {"DocumentLines": lines},
            label=f"update Without Qty Posting on draft {int(draft_entry)}",
        )
        logger.info(
            "Draft %s: Without Qty Posting %s on lines %s",
            draft_entry,
            "set" if without_qty else "cleared",
            ",".join(str(line["LineNum"]) for line in lines),
        )
