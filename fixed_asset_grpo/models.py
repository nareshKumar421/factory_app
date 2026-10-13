"""Fixed Asset GRPO: posting a vendor's fixed-asset delivery to SAP as a GRPO.

A fixed asset bought from a vendor is ordered on an ordinary item PO whose
lines sit in a fixed-asset warehouse (BH-FA, DL-FA, GP-FA). The gate records
the arrival on the fixed-asset wizard (``fixed_asset_gatein``) with free text:
it does not know the PO. This app links the PO **later**, on the Fixed Asset
GRPO screen, and posts the GRPO through the existing material GRPO engine.

NO NEW RECEIPT TABLES. The PO lines are stored in raw_material_gatein's own
``POReceipt`` / ``POItemReceipt`` tables, exactly as finished-goods receipts
are (``finished_goods_gatein/models.py``), and told apart only by the gate
entry's ``entry_type == "FIXED_ASSET"``. ``grpo.GRPOPosting`` already
PROTECT-references those tables, so the engine posts them unchanged. The two
proxies below add an admin view and a permission namespace, and create no
table.

TWO NEW TABLES, both owned here and both pointing *at* existing rows, so no
existing table gains a column:

* ``FixedAssetGRPOClosure`` -- "Done in SAP by hand": an FA entry whose GRPO
  accounts keyed in SAP themselves. It hides the entry from the lists and
  refuses any post, so the same asset is never received twice.
* ``FixedAssetGRPORecord`` -- one row per GRPO this app posted, beside the
  engine's ``GRPOPosting``. It says whether the post was **simulated** (no SAP
  document exists) and who posted it, and keeps the payload a simulated post
  would have sent. It replaces the spec's ``GRPOPosting.simulated`` column,
  which would have been a change to grpo's table.
"""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from driver_management.models import VehicleEntry
from grpo.models import GRPOPosting
from raw_material_gatein.models import POItemReceipt, POReceipt

from .constants import FA_ENTRY_TYPE


class FAReceiptManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(vehicle_entry__entry_type=FA_ENTRY_TYPE)


class FAReceipt(POReceipt):
    """A PO receipt linked to a FIXED_ASSET gate entry (proxy of POReceipt)."""

    objects = FAReceiptManager()

    class Meta:
        proxy = True
        verbose_name = "Fixed asset PO receipt"
        verbose_name_plural = "Fixed asset PO receipts"


class FAItemReceiptManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(po_receipt__vehicle_entry__entry_type=FA_ENTRY_TYPE)
        )


class FAItemReceipt(POItemReceipt):
    """A PO line on a fixed-asset receipt (proxy of POItemReceipt)."""

    objects = FAItemReceiptManager()

    class Meta:
        proxy = True
        verbose_name = "Fixed asset PO receipt line"
        verbose_name_plural = "Fixed asset PO receipt lines"


class FixedAssetGRPOClosure(models.Model):
    """An FA gate entry whose GRPO was done in SAP by hand.

    Accounts key some fixed-asset receipts in SAP themselves (the backlog
    before the cut-over, an asset whose PO sits in another warehouse, a
    service invoice). Closing the entry here says so: it drops out of the
    Needs PO and Ready lists, post refuses it, and its PO link is frozen. The
    optional ``sap_doc_num`` records which SAP document it became.

    Reopening deletes the row. Closing is refused while the app itself has
    posted the entry for real, or has a GRPO of it on its way to SAP.
    """

    vehicle_entry = models.OneToOneField(
        VehicleEntry,
        on_delete=models.CASCADE,
        related_name="fa_grpo_closure",
    )
    sap_doc_num = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="The SAP GRPO (or other document) number it was keyed as, if known.",
    )
    note = models.TextField(blank=True, default="")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fixed_asset_grpo_closures",
    )
    closed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ()
        verbose_name = "Fixed asset GRPO closed by hand"
        verbose_name_plural = "Fixed asset GRPOs closed by hand"

    def __str__(self):
        ref = f" as {self.sap_doc_num}" if self.sap_doc_num else ""
        return f"{self.vehicle_entry.entry_no} done in SAP by hand{ref}"


class FixedAssetGRPORecord(models.Model):
    """What this app knows about a GRPO it posted for a fixed-asset entry.

    One row per POSTED ``GRPOPosting`` that came through the Fixed Asset GRPO
    screen or its queue handler. ``simulated`` is True when
    ``FIXED_ASSET_GRPO_SIMULATE`` was on: every check ran, SAP was read but
    not written, the posting carries no ``sap_doc_entry``, and ``sap_payload``
    holds the exact document that would have been sent. A simulated posting
    blocks a real one for the same PO until it is discarded from history.

    The model also carries the app's three permissions, so they live under
    the ``fixed_asset_grpo`` label without touching ``grpo.GRPOPosting.Meta``.
    """

    grpo_posting = models.OneToOneField(
        GRPOPosting,
        on_delete=models.CASCADE,
        related_name="fa_record",
    )
    simulated = models.BooleanField(
        default=False,
        help_text=(
            "Marked posted without calling SAP (FIXED_ASSET_GRPO_SIMULATE). "
            "No SAP document exists."
        ),
    )
    sap_payload = models.JSONField(
        null=True,
        blank=True,
        encoder=DjangoJSONEncoder,
        help_text="For a simulated post: the GRPO document that would have been sent.",
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fixed_asset_grpo_records",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        verbose_name = "Fixed asset GRPO record"
        verbose_name_plural = "Fixed asset GRPO records"
        permissions = [
            ("can_view_fixed_asset_grpo", "Can view fixed asset GRPOs"),
            ("can_post_fixed_asset_grpo", "Can post fixed asset GRPOs to SAP"),
            ("can_link_fixed_asset_po", "Can link an SAP PO to a fixed asset gate entry"),
        ]

    def __str__(self):
        kind = "simulated" if self.simulated else "posted"
        return f"FA GRPO {self.grpo_posting_id} ({kind})"
