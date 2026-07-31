"""Finished-goods gate-in reuses the raw-material PO receipt tables.

Purchased/traded finished goods (SAP item group 102, ``FG`` code prefix) follow
the identical PO -> GRPO document chain as raw materials, and the material GRPO
poster (``grpo.GRPOPosting``) already PROTECT-references
``raw_material_gatein.POReceipt`` / ``POItemReceipt``. So rather than fork a
parallel receipt+GRPO stack, finished-goods receipts are stored in those same
tables and told apart purely by their gate entry's ``entry_type`` (
``FINISHED_GOODS`` vs ``RAW_MATERIAL``). The only real difference in the flow is
that FG carries no QC arrival slip/inspection.

These proxy models add a dedicated permission namespace (
``finished_goods_gatein.*``) and an admin view without creating new tables.
"""

from django.db import models

from raw_material_gatein.models import POItemReceipt, POReceipt


class FGReceiptManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(vehicle_entry__entry_type="FINISHED_GOODS")
        )


class FGReceipt(POReceipt):
    """A PO receipt captured on a FINISHED_GOODS gate entry (proxy of POReceipt)."""

    objects = FGReceiptManager()

    class Meta:
        proxy = True
        verbose_name = "Finished Goods Receipt"
        verbose_name_plural = "Finished Goods Receipts"
        permissions = [
            ("can_receive_fg_po", "Can receive finished goods PO"),
            ("can_complete_fg_entry", "Can complete finished goods gate entry"),
        ]


class FGItemReceiptManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(po_receipt__vehicle_entry__entry_type="FINISHED_GOODS")
        )


class FGItemReceipt(POItemReceipt):
    """A PO line on a finished-goods receipt (proxy of POItemReceipt)."""

    objects = FGItemReceiptManager()

    class Meta:
        proxy = True
        verbose_name = "Finished Goods Receipt Item"
        verbose_name_plural = "Finished Goods Receipt Items"
