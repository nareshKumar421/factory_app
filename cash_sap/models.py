"""
What the app keeps about posting a cash bunch to SAP.

THE SHAPE IN SAP (the imprest convention USER22 keys by hand today)
-------------------------------------------------------------------
One **purchase order** per bunch per SAP company, to the imprest vendor
(ORGV000465 in Oil), with one line per voucher at quantity 1. Then one **GRPO**
per voucher -- or per voucher group, by routing -- against that PO, with the
bill photographed onto it. Optionally an **A/P invoice draft** that head office
opens in JSAP, checks (the budget head comes prefilled; SAP refuses a draft
line without one), and adds. Heads with no item (fuel, toll, courier) never
touch a PO: they are A/P **service** lines. Beverages keeps the very same shape
in its own company, on its own imprest account (ORGV000245 there).

So the rows here are:

* :class:`ImprestBatch`   -- one SAP PO (bunch x SAP company), and the anchor of
  everything else that company's part of the bunch makes;
* :class:`ImprestGRPO`    -- one SAP GRPO;
* :class:`ImprestAPDraft` -- one SAP A/P draft (ITEM or SERVICE) per batch;
* :class:`ImprestVoucher` -- one voucher's posting overrides and the snapshot of
  what it was frozen as;
* :class:`VoucherSapInfo` -- the bill's own facts (bill date, G.no., bill no.),
  kept here because ``cash_book`` is not edited on this branch;
* :class:`BunchSapState`  -- the bunch's freeze and its member snapshot, which
  replaces the ``CashBunch.sap_locked_at`` the spec wanted (see its docstring);
* :class:`CashBranchSapRouting` and :class:`GLItemMap` -- settings.

NOTHING HERE IS WRITTEN BY SAP. Every SAP field is filled by the posting
handlers in ``sap_posting.py``, through ``sap_postings``.
"""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q

from cash_book.models import CashBranch, CashBunch, CashEntry
from gate_core.models.base import BaseModel

from .constants import (
    MARKER_AP_ITEM,
    MARKER_AP_SERVICE,
    MARKER_GRPO,
    MARKER_PO,
    marker,
)


class PostingMode(models.TextChoices):
    PO_GRPO = "PO_GRPO", "PO per bunch, GRPO per voucher"
    AP_DRAFT_ONLY = "AP_DRAFT_ONLY", "A/P draft only"


class HeadTreatment(models.TextChoices):
    """What a G/L head becomes in SAP, per target company (the G/L map)."""

    ITEM = "ITEM", "Goods — GRPO against the imprest PO"
    SERVICE = "SERVICE", "Service — A/P service line"
    NEVER = "NEVER", "Never posted"


class VoucherTreatment(models.TextChoices):
    """What one voucher became, after the map, the routing and any override."""

    ITEM = "ITEM", "Goods (GRPO)"
    SERVICE = "SERVICE", "Service line"
    SKIP = "SKIP", "Not posted"


class GrpoGrouping(models.TextChoices):
    """How vouchers share a GRPO. SAP shows both habits (spec D14, OPEN-7)."""

    NONE = "NONE", "One GRPO per voucher"
    GATE_NO = "GATE_NO", "One GRPO per bill date + G.no."
    BILL_NO = "BILL_NO", "One GRPO per bill date + bill no."


class SapDocStatus(models.TextChoices):
    """Where one SAP document the app makes has got to."""

    PLANNED = "PLANNED", "Not sent"
    QUEUED = "QUEUED", "Waiting for SAP"
    IN_APPROVAL = "IN_APPROVAL", "In SAP approval"
    POSTED = "POSTED", "In SAP"
    FAILED = "FAILED", "Refused / stopped"
    NOT_NEEDED = "NOT_NEEDED", "Not needed"


class BunchSapStatus(models.TextChoices):
    OPEN = "OPEN", "Not frozen"
    FROZEN = "FROZEN", "Frozen for SAP"
    CHANGED = "CHANGED", "Changed in the cash book after freezing"


class APDraftKind(models.TextChoices):
    ITEM = "ITEM", "Item (based on the GRPOs)"
    SERVICE = "SERVICE", "Service lines"


#: A unit in one of these may already be in SAP, or on its way there: its
#: vouchers are frozen and nothing about them may be recomputed.
COMMITTED_STATUSES = (SapDocStatus.QUEUED, SapDocStatus.IN_APPROVAL, SapDocStatus.POSTED)
#: A unit in one of these has nothing left to do.
DONE_STATUSES = (SapDocStatus.POSTED, SapDocStatus.NOT_NEEDED)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class CashBranchSapRouting(BaseModel):
    """Where one cash-book branch's vouchers go in SAP, and how.

    One row per :class:`cash_book.models.CashBranch`. A branch with no row, or
    a disabled one, has no SAP company: its vouchers are UNROUTED and block the
    bunch until somebody routes them, picks a company per voucher, or leaves
    them out. Every header field is snapshotted onto the batch at freeze, so a
    settings change never rewrites a document already on its way.
    """

    branch = models.OneToOneField(
        CashBranch, on_delete=models.CASCADE, related_name="sap_routing"
    )
    enabled = models.BooleanField(default=False)
    #: A key of ``sap_client.registry.COMPANY_SAP_REGISTRY``.
    sap_company_code = models.CharField(max_length=30, blank=True, default="")
    mode = models.CharField(
        max_length=16, choices=PostingMode.choices, default=PostingMode.PO_GRPO
    )
    #: The imprest vendor (ORGV000465 for the factory in Oil).
    vendor_code = models.CharField(max_length=30, blank=True, default="")
    #: SAP branch (OBPL.BPLId); null for a company without branches.
    bpl_id = models.PositiveIntegerField(null=True, blank=True)
    warehouse_code = models.CharField(max_length=20, blank=True, default="")
    #: Dim1 (product line), Dim3 (budget head), Dim5 (state). Dim2 is the
    #: voucher's bill month and is never configured.
    ocr_code = models.CharField(max_length=30, blank=True, default="")
    ocr_code3 = models.CharField(max_length=30, blank=True, default="")
    ocr_code5 = models.CharField(max_length=30, blank=True, default="")
    item_tax_code = models.CharField(max_length=20, default="IGST@0")
    #: SAP's own spelling.
    service_tax_code = models.CharField(max_length=20, default="Exampt")
    ap_drafts_enabled = models.BooleanField(default=False)
    #: SAP rule 200019 refuses a GRPO with no attachment unless the vendor's
    #: group is exempt; True blocks a voucher with no bill photo.
    grpo_needs_attachment = models.BooleanField(default=True)
    grpo_grouping = models.CharField(
        max_length=10, choices=GrpoGrouping.choices, default=GrpoGrouping.NONE
    )
    #: Picks one series when several fit the month and branch (spec D6).
    po_series_hint = models.CharField(max_length=40, blank=True, default="")
    grpo_series_hint = models.CharField(max_length=40, blank=True, default="")
    ap_series_hint = models.CharField(max_length=40, blank=True, default="")
    #: Vouchers billed before this date were keyed into SAP by hand: SKIP.
    cutover_date = models.DateField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        permissions = [
            (
                "can_manage_cash_sap_settings",
                "Can edit cash book SAP routing and G/L item map",
            ),
        ]

    def __str__(self):
        return f"{self.branch} -> {self.sap_company_code or '(none)'}"


class GLItemMap(BaseModel):
    """What one cash-book G/L head becomes in one target SAP company.

    The G/L code is the cash book's own (its company's chart). When the target
    is another company the code has to be mapped (``target_account_code``):
    Oil's OACT codes are not valid in Beverages. Retire rather than delete
    (``is_active=False``), so a head's history stays readable.
    """

    #: The **target** SAP company.
    sap_company_code = models.CharField(max_length=30)
    #: As recorded on vouchers (the cash book's own chart).
    gl_account_code = models.CharField(max_length=32)
    gl_account_name = models.CharField(max_length=255, blank=True, default="")
    #: The account in the target company; blank = the same code, which is only
    #: allowed when the target is the cash book's own company.
    target_account_code = models.CharField(max_length=32, blank=True, default="")
    treatment = models.CharField(
        max_length=8, choices=HeadTreatment.choices, default=HeadTreatment.ITEM
    )
    item_code = models.CharField(max_length=50, blank=True, default="")
    item_name = models.CharField(max_length=200, blank=True, default="")
    #: Dim3 for this head, when it differs from the routing's.
    ocr_code3 = models.CharField(max_length=30, blank=True, default="")
    #: The head buys several items (5680012, 5100015, 5100006); the mapped item
    #: is only the commonest, so the plan warns.
    needs_review = models.BooleanField(default=False)

    class Meta:
        default_permissions = ()
        ordering = ["sap_company_code", "gl_account_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["sap_company_code", "gl_account_code"],
                condition=Q(is_active=True),
                name="uq_cash_sap_gl_map_active",
            )
        ]

    def __str__(self):
        return f"{self.sap_company_code} {self.gl_account_code} -> {self.treatment}"


# ---------------------------------------------------------------------------
# The voucher's own facts, and the bunch's freeze
# ---------------------------------------------------------------------------


class VoucherSapInfo(models.Model):
    """The bill's own facts, for one voucher.

    The spec added these to ``CashEntry``; this branch may not change
    ``cash_book``, so they live here, one row per voucher, created on first
    edit. **This is the one source of truth** for the bill date, the G.no. and
    the supplier's bill number: the posting overrides on :class:`ImprestVoucher`
    do not repeat them, and the snapshot there is only a copy taken at freeze.

    Posting data, not book-keeping: it is edited on the cash -> SAP screens and
    stays editable after the voucher is APPROVED (which locks it in the cash
    book), until the voucher's SAP document is committed.
    """

    entry = models.OneToOneField(
        CashEntry, on_delete=models.CASCADE, related_name="sap_info"
    )
    #: Blank = the voucher date. SAP's TaxDate, NumAtCard date and Dim2 month.
    bill_date = models.DateField(null=True, blank=True)
    #: The "G.no." written on the voucher; goes into the GRPO's Comments.
    gate_entry_no = models.CharField(max_length=30, blank=True, default="")
    #: The shop's own bill number, when there is one.
    supplier_bill_no = models.CharField(max_length=60, blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()

    def __str__(self):
        return f"SAP info for entry {self.entry_id}"

    @property
    def effective_bill_date(self):
        return self.bill_date or self.entry.entry_date


class BunchSapState(models.Model):
    """The bunch's freeze, and what it held when it was frozen.

    The spec locked the bunch in ``cash_book`` (``CashBunch.sap_locked_at``) so
    no voucher could be taken out once posting began. That would be an edit to
    ``cash_book``, so instead the members are **snapshotted** here at the first
    freeze (entry id -> amount, G/L, branch, voucher date) and compared before
    every freeze, post, send-next and handler send. Any difference stops the
    bunch with ``BUNCH_CHANGED`` and nothing more is sent until a person resets
    the bunch (nothing in SAP yet) or accepts the change (see
    ``services.accept_bunch_changes``).
    """

    bunch = models.OneToOneField(
        CashBunch, on_delete=models.PROTECT, related_name="sap_state"
    )
    status = models.CharField(
        max_length=16, choices=BunchSapStatus.choices, default=BunchSapStatus.OPEN
    )
    frozen_at = models.DateTimeField(null=True, blank=True)
    frozen_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    #: ``{"<entry id>": {"amount", "gl", "branch", "entry_date"}}``.
    member_snapshot = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    #: The last difference found: ``{"added": [...], "removed": [...], "changed": {...}}``.
    drift = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    checked_at = models.DateTimeField(null=True, blank=True)
    changes_accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    changes_accepted_at = models.DateTimeField(null=True, blank=True)
    changes_accepted_note = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()

    def __str__(self):
        return f"SAP state of bunch {self.bunch_id}: {self.status}"

    @property
    def is_frozen(self) -> bool:
        return self.frozen_at is not None


# ---------------------------------------------------------------------------
# The documents
# ---------------------------------------------------------------------------


class ImprestBatch(BaseModel):
    """One SAP purchase order: a bunch's vouchers for one SAP company.

    Also the anchor of that company's GRPOs and A/P drafts, and the place the
    routing is snapshotted at freeze -- so changing the settings afterwards
    never changes a document already on its way.
    """

    bunch = models.ForeignKey(CashBunch, on_delete=models.PROTECT, related_name="sap_batches")
    sap_company_code = models.CharField(max_length=30)

    # -- header snapshot from the resolved routing --------------------------
    mode = models.CharField(
        max_length=16, choices=PostingMode.choices, default=PostingMode.PO_GRPO
    )
    vendor_code = models.CharField(max_length=30, blank=True, default="")
    bpl_id = models.PositiveIntegerField(null=True, blank=True)
    warehouse_code = models.CharField(max_length=20, blank=True, default="")
    ap_drafts_enabled = models.BooleanField(default=False)
    grpo_needs_attachment = models.BooleanField(default=True)
    grpo_grouping = models.CharField(
        max_length=10, choices=GrpoGrouping.choices, default=GrpoGrouping.NONE
    )
    po_series_hint = models.CharField(max_length=40, blank=True, default="")
    grpo_series_hint = models.CharField(max_length=40, blank=True, default="")
    ap_series_hint = models.CharField(max_length=40, blank=True, default="")

    #: 1st of the earliest ITEM bill month (or of all bill dates without ITEM).
    expense_month = models.DateField(null=True, blank=True)
    po_doc_date = models.DateField(null=True, blank=True)
    po_due_date = models.DateField(null=True, blank=True)

    # -- the SAP PO ----------------------------------------------------------
    po_round = models.PositiveSmallIntegerField(default=1)
    po_status = models.CharField(
        max_length=16, choices=SapDocStatus.choices, default=SapDocStatus.PLANNED
    )
    po_doc_entry = models.PositiveBigIntegerField(null=True, blank=True)
    po_doc_num = models.PositiveBigIntegerField(null=True, blank=True)
    po_draft_entry = models.PositiveBigIntegerField(null=True, blank=True)
    po_doc_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    po_payload = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    po_error = models.TextField(blank=True, default="")
    po_posted_at = models.DateTimeField(null=True, blank=True)
    po_close_status = models.CharField(
        max_length=16, choices=SapDocStatus.choices, default=SapDocStatus.NOT_NEEDED
    )
    po_close_error = models.TextField(blank=True, default="")

    simulated = models.BooleanField(default=False)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    requested_at = models.DateTimeField(null=True, blank=True)
    last_sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    last_sent_at = models.DateTimeField(null=True, blank=True)
    completion_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        permissions = [
            ("can_post_cash_to_sap", "Can post cash book bunches to SAP"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["bunch", "sap_company_code"], name="uq_imprest_batch_bunch_company"
            )
        ]

    def __str__(self):
        return f"Imprest batch {self.pk} (bunch {self.bunch_id}, {self.sap_company_code})"

    @property
    def marker(self) -> str:
        return marker(MARKER_PO, self.pk, self.po_round)

    @property
    def po_committed(self) -> bool:
        """The PO is in SAP, or on its way, or held in approval."""
        return bool(
            self.po_doc_entry is not None
            or self.po_draft_entry is not None
            or self.po_status in COMMITTED_STATUSES
        )


class ImprestGRPO(BaseModel):
    """One SAP GRPO against the batch's PO: one voucher, or a group (D14)."""

    batch = models.ForeignKey(ImprestBatch, on_delete=models.PROTECT, related_name="grpos")
    group_key = models.CharField(max_length=160)
    round = models.PositiveSmallIntegerField(default=1)
    sap_company_code = models.CharField(max_length=30)
    vendor_code = models.CharField(max_length=30, blank=True, default="")
    bill_date = models.DateField()
    #: DocDate/DocDueDate. Equals the bill date unless re-dated after a closed
    #: period; TaxDate, NumAtCard and Dim2 keep the bill's own date (D5).
    posting_date = models.DateField()
    gate_entry_no = models.CharField(max_length=60, blank=True, default="")
    supplier_bill_no = models.CharField(max_length=60, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    num_at_card = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=SapDocStatus.choices, default=SapDocStatus.PLANNED
    )
    grpo_doc_entry = models.PositiveBigIntegerField(null=True, blank=True)
    grpo_doc_num = models.PositiveBigIntegerField(null=True, blank=True)
    grpo_draft_entry = models.PositiveBigIntegerField(null=True, blank=True)
    grpo_doc_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    error = models.TextField(blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    #: Saved the moment the first file is uploaded, so a crash after it never
    #: uploads the same file twice.
    sap_attachment_entry = models.PositiveBigIntegerField(null=True, blank=True)
    #: ``["entry_att:<id>", "bill:<id>", ...]`` already in SAP.
    uploaded_files = models.JSONField(default=list, blank=True, encoder=DjangoJSONEncoder)
    #: ``{"<voucher id>": GRPO LineNum}``.
    line_map = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    simulated = models.BooleanField(default=False)
    last_sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "group_key"], name="uq_imprest_grpo_batch_key"
            ),
            models.UniqueConstraint(
                fields=["sap_company_code", "vendor_code", "num_at_card"],
                condition=~Q(num_at_card=""),
                name="uq_imprest_grpo_num_at_card",
            ),
        ]

    def __str__(self):
        return f"Imprest GRPO {self.pk} ({self.num_at_card or self.group_key})"

    @property
    def marker(self) -> str:
        return marker(MARKER_GRPO, self.pk, self.round)

    @property
    def committed(self) -> bool:
        return bool(
            self.grpo_doc_entry is not None
            or self.grpo_draft_entry is not None
            or self.status in COMMITTED_STATUSES
        )


class ImprestVoucher(BaseModel):
    """One voucher's posting overrides, and the snapshot it was frozen as.

    OVERRIDES are for the approved backlog (D17): an APPROVED voucher is locked
    in the cash book, so anything about how it posts that has to change is
    changed here -- company, treatment, item, Dim1, Dim3. The bill's date, G.no.
    and bill number are not overrides: they live on :class:`VoucherSapInfo`.

    ``bunch`` records which bunch the overrides were made in. A voucher taken
    out of one bunch and put in another starts clean there (the plan ignores a
    row whose ``bunch`` is another bunch).

    The SNAPSHOT is written at freeze and is what the payloads are built from;
    after the voucher's document is committed nothing recomputes it.
    """

    entry = models.OneToOneField(CashEntry, on_delete=models.PROTECT, related_name="sap_voucher")
    bunch = models.ForeignKey(
        CashBunch, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    batch = models.ForeignKey(
        ImprestBatch, on_delete=models.PROTECT, null=True, blank=True, related_name="vouchers"
    )
    grpo = models.ForeignKey(
        ImprestGRPO, on_delete=models.SET_NULL, null=True, blank=True, related_name="vouchers"
    )

    # -- overrides -----------------------------------------------------------
    sap_company_code_override = models.CharField(max_length=30, blank=True, default="")
    treatment_override = models.CharField(
        max_length=8, choices=VoucherTreatment.choices, blank=True, default=""
    )
    item_code_override = models.CharField(max_length=50, blank=True, default="")
    ocr_code_override = models.CharField(max_length=30, blank=True, default="")
    ocr_code3_override = models.CharField(max_length=30, blank=True, default="")
    override_note = models.TextField(blank=True, default="")
    overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    overridden_at = models.DateTimeField(null=True, blank=True)

    # -- snapshot (written at freeze) -----------------------------------------
    treatment = models.CharField(
        max_length=8, choices=VoucherTreatment.choices, blank=True, default=""
    )
    item_code = models.CharField(max_length=50, blank=True, default="")
    item_name = models.CharField(max_length=200, blank=True, default="")
    gl_account_code = models.CharField(max_length=32, blank=True, default="")
    gl_account_name = models.CharField(max_length=255, blank=True, default="")
    account_code = models.CharField(max_length=32, blank=True, default="")
    warehouse_code = models.CharField(max_length=20, blank=True, default="")
    tax_code = models.CharField(max_length=20, blank=True, default="")
    ocr_code = models.CharField(max_length=30, blank=True, default="")
    ocr_code2 = models.CharField(max_length=30, blank=True, default="")
    ocr_code3 = models.CharField(max_length=30, blank=True, default="")
    ocr_code5 = models.CharField(max_length=30, blank=True, default="")
    bill_date = models.DateField(null=True, blank=True)
    gate_entry_no = models.CharField(max_length=30, blank=True, default="")
    supplier_bill_no = models.CharField(max_length=60, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    po_line_num = models.PositiveIntegerField(null=True, blank=True)
    skip_reason = models.CharField(max_length=255, blank=True, default="")

    # -- left out after the PO -------------------------------------------------
    left_out_after_po = models.BooleanField(default=False)
    left_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    left_out_at = models.DateTimeField(null=True, blank=True)
    left_out_reason = models.TextField(blank=True, default="")

    class Meta:
        default_permissions = ()

    def __str__(self):
        return f"Imprest voucher for entry {self.entry_id}"


def _bill_path(instance, filename):
    """``cash_sap/<company id>/<entry id>/<filename>``, beside the cash book's own."""
    entry = instance.voucher.entry
    return f"cash_sap/{entry.company_id}/{entry.id}/{filename}"


class ImprestVoucherBill(BaseModel):
    """A "posting bill": a photograph added on the SAP page, not in the cash book.

    An APPROVED voucher cannot be given a bill in the cash book, and SAP refuses
    a GRPO with no attachment (rule 200019), so a voucher that reached approval
    without its bill is photographed here.
    """

    voucher = models.ForeignKey(ImprestVoucher, on_delete=models.CASCADE, related_name="bills")
    file = models.FileField(upload_to=_bill_path, max_length=255)
    original_filename = models.CharField(max_length=255)
    size_bytes = models.PositiveIntegerField(default=0)

    class Meta:
        default_permissions = ()
        ordering = ["id"]

    def __str__(self):
        return f"{self.original_filename} on voucher {self.voucher_id}"

    @property
    def key(self) -> str:
        return f"bill:{self.pk}"


class ImprestAPDraft(BaseModel):
    """One SAP A/P invoice draft (ODRF, ObjType 18) that head office completes.

    SERVICE: the batch's service vouchers as service lines, sent with the PO.
    ITEM: based on the batch's posted GRPOs, sent once every GRPO is in SAP.
    Head office opens it in JSAP, checks the prefilled budget head, and adds it.
    """

    batch = models.ForeignKey(ImprestBatch, on_delete=models.PROTECT, related_name="ap_drafts")
    kind = models.CharField(max_length=8, choices=APDraftKind.choices)
    round = models.PositiveSmallIntegerField(default=1)
    sap_company_code = models.CharField(max_length=30)
    vendor_code = models.CharField(max_length=30, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=SapDocStatus.choices, default=SapDocStatus.PLANNED
    )
    num_at_card = models.CharField(max_length=100, blank=True, default="")
    doc_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    payload = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    error = models.TextField(blank=True, default="")
    sap_draft_entry = models.PositiveBigIntegerField(null=True, blank=True)
    sap_doc_entry = models.PositiveBigIntegerField(null=True, blank=True)
    sap_doc_num = models.PositiveBigIntegerField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    simulated = models.BooleanField(default=False)
    last_sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "kind"], name="uq_imprest_ap_draft_batch_kind"
            ),
            models.UniqueConstraint(
                fields=["sap_company_code", "vendor_code", "num_at_card"],
                condition=~Q(num_at_card=""),
                name="uq_imprest_ap_num_at_card",
            ),
        ]

    def __str__(self):
        return f"Imprest A/P {self.kind.lower()} draft {self.pk}"

    @property
    def marker(self) -> str:
        prefix = MARKER_AP_ITEM if self.kind == APDraftKind.ITEM else MARKER_AP_SERVICE
        return marker(prefix, self.pk, self.round)

    @property
    def committed(self) -> bool:
        return bool(
            self.sap_draft_entry is not None
            or self.sap_doc_entry is not None
            or self.status in COMMITTED_STATUSES
        )
