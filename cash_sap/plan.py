"""What would post: the bunch's SAP plan, worked out fresh on every read.

The plan is the screen. Every voucher of the bunch is resolved to ITEM (a PO
line and a GRPO), SERVICE (an A/P service line), SKIP (not posted, with the
reason) or UNROUTED (nobody has said where it goes); the routed ones are
grouped by SAP company; and every reason something cannot post yet is a
:class:`Problem` on the voucher, the group or the bunch. ``freeze_bunch`` uses
the same plan, so what the screen shows is exactly what is frozen.

RULES, IN ORDER (spec §2.3.6; the bill's facts come from ``VoucherSapInfo``)
---------------------------------------------------------------------------
 1. Cash received                         -> SKIP (rule)
 2. Not APPROVED                          -> blocking NOT_APPROVED
 3. Frozen (its document is committed)    -> the snapshot, nothing recomputed
 4. Company = override, else the branch routing's (when enabled)
 5. Salary-advance head                   -> SKIP (rule, cannot be overridden)
 6. Override SKIP                         -> SKIP
 7. Bill date = VoucherSapInfo.bill_date or the voucher date
 8. No company                            -> UNROUTED (blocking)
 9. No enabled routing for the company    -> blocking NO_ROUTING_FOR_COMPANY
10. Billed before the routing's cut-over  -> SKIP
11. Map says NEVER                        -> SKIP
12. Treatment = override, else map, else SERVICE (warning UNMAPPED_HEAD);
    AP_DRAFT_ONLY turns ITEM into SERVICE (info AP_ONLY_COMPANY)
13. ITEM needs an item (NO_ITEM); a multi-item head warns (ITEM_REVIEW)
14. Would it be sent at all?
15. Account in the target company, or blocking GL_NOT_IN_TARGET
16. Dims, tax, warehouse          17. G.no. and bill no.
18. ITEM needs a bill photo (NO_BILL) when the routing says so
19. Future bill date (FUTURE_DATE)  20. Date warnings (no DATE_OLD here)

THE BUNCH IS ALSO CHECKED FOR DRIFT. A frozen bunch whose members changed in
the cash book carries a blocking ``BUNCH_CHANGED`` problem (``drift.py``).

ACROSS BUNCHES. A voucher has one ``ImprestVoucher`` row (OneToOne on the
entry). When the cash book moves a voucher from bunch A to bunch B while that
row is frozen under A (its PO, GRPO or service draft committed, or it was left
out after A's PO), B must not post it again: it resolves to UNROUTED with a
blocking ``IN_OTHER_BUNCH`` problem, and the bunch carries one too. Rows made
in another bunch that are not frozen still start clean here.

AFTER THE FREEZE. A goods voucher not on a committed PO (``NOT_ON_PO``) and a
service voucher not on a committed SERVICE draft (``NOT_ON_AP_DRAFT``) block.
A PO or SERVICE draft that is not committed yet but whose frozen vouchers
differ from this plan carries a non-blocking ``CHANGED_SINCE_FREEZE``; send-next
freezes it again before sending it, and the handler refuses to send it stale
(:func:`snapshot_changes`).
"""

import dataclasses
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Dict, List, Optional

from django.db.models import F
from django.utils import timezone

from cash_book.models import CashDirection, EntryApprovalStatus

from . import drift as drift_mod
from . import flags
from .constants import (
    BUNCH_CHANGED,
    CHANGED_SINCE_FREEZE,
    IN_OTHER_BUNCH,
    KIND_AP,
    KIND_GRPO,
    KIND_PO,
    KIND_PO_CLOSE,
    NEVER_POSTED_GL_CODES,
    NOT_ON_AP_DRAFT,
    NOT_ON_PO,
)
from .models import (
    DONE_STATUSES,
    APDraftKind,
    BunchSapState,
    CashBranchSapRouting,
    GLItemMap,
    GrpoGrouping,
    HeadTreatment,
    ImprestBatch,
    ImprestVoucher,
    PostingMode,
    SapDocStatus,
    VoucherSapInfo,
    VoucherTreatment,
)
from .payloads import month_code, person_label
from .routing import header_tuple, home_company_code, routing_for
from .voucher_dates import fmt, voucher_date_warnings

UNROUTED = "UNROUTED"

SOURCE_MAP = "MAP"
SOURCE_DEFAULT = "DEFAULT"
SOURCE_OVERRIDE = "OVERRIDE"
SOURCE_RULE = "RULE"
SOURCE_FROZEN = "FROZEN"

#: Statuses a unit still has work in.
OPEN_STATUSES = (
    SapDocStatus.PLANNED,
    SapDocStatus.QUEUED,
    SapDocStatus.FAILED,
    SapDocStatus.IN_APPROVAL,
)


def _native(value):
    """Decimal -> str, date -> ISO, recursively; what the API returns."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native(v) for v in value]
    return value


class _AsDict:
    def as_dict(self) -> dict:
        return _native(dataclasses.asdict(self))


@dataclass
class Problem(_AsDict):
    code: str
    message: str
    blocking: bool = True


@dataclass
class VoucherPlan(_AsDict):
    entry_id: int
    serial_number: Optional[int] = None
    entry_date: Optional[date] = None
    bill_date: Optional[date] = None
    gate_entry_no: str = ""
    supplier_bill_no: str = ""
    branch_id: Optional[int] = None
    branch_name: str = ""
    gl_account_code: str = ""
    gl_account_name: str = ""
    account_code: str = ""
    item: str = ""
    detail: str = ""
    amount: Decimal = Decimal("0")
    sap_company_code: str = ""
    routing_id: Optional[int] = None
    treatment: str = UNROUTED
    treatment_source: str = SOURCE_RULE
    skip_reason: str = ""
    item_code: str = ""
    item_name: str = ""
    warehouse_code: str = ""
    tax_code: str = ""
    ocr_code: str = ""
    ocr_code2: str = ""
    ocr_code3: str = ""
    ocr_code5: str = ""
    has_bill: bool = False
    bill_count: int = 0
    overridden: bool = False
    frozen: bool = False
    left_out_after_po: bool = False
    grpo_id: Optional[int] = None
    po_line_num: Optional[int] = None
    warnings: List[dict] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(p.blocking for p in self.problems)


@dataclass
class UnitStatus(_AsDict):
    kind: str
    source_id: int
    status: str
    doc_num: Optional[int] = None
    error: str = ""
    simulated: bool = False
    sap_posting: Optional[dict] = None
    draft_entry: Optional[int] = None
    stopped: bool = False
    label: str = ""


@dataclass
class GroupPlan(_AsDict):
    sap_company_code: str
    mode: str = ""
    vendor_code: str = ""
    bpl_id: Optional[int] = None
    warehouse_code: str = ""
    ap_drafts_enabled: bool = False
    grpo_grouping: str = GrpoGrouping.NONE
    is_test_target: bool = False
    expense_month: Optional[date] = None
    po_doc_date: Optional[date] = None
    po_due_date: Optional[date] = None
    item_total: Decimal = Decimal("0")
    service_total: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    sendable: bool = False
    vouchers: List[VoucherPlan] = field(default_factory=list)
    grpos: List[dict] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    batch: Optional[dict] = None
    po: Optional[UnitStatus] = None
    po_close: Optional[UnitStatus] = None
    ap_drafts: List[UnitStatus] = field(default_factory=list)
    can_post: bool = False

    @property
    def blocking(self) -> bool:
        return any(p.blocking for p in self.problems) or any(
            v.blocking for v in self.vouchers if not v.frozen
        )


@dataclass
class BunchPlan(_AsDict):
    bunch: dict
    enabled: bool
    simulate: bool
    allow_live: bool
    groups: List[GroupPlan] = field(default_factory=list)
    unrouted: List[VoucherPlan] = field(default_factory=list)
    skipped: List[VoucherPlan] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    can_post: bool = False
    summary: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return any(p.blocking for p in self.problems)

    def group(self, code) -> Optional[GroupPlan]:
        return next((g for g in self.groups if g.sap_company_code == code), None)


#: The override when a voucher has none here (or has one made in another bunch).
_NO_OVERRIDE = SimpleNamespace(
    pk=None,
    id=None,
    bunch_id=None,
    batch_id=None,
    grpo_id=None,
    sap_company_code_override="",
    treatment_override="",
    item_code_override="",
    ocr_code_override="",
    ocr_code3_override="",
    override_note="",
    overridden_by=None,
    treatment="",
    left_out_after_po=False,
    skip_reason="",
)


def _bills(override) -> list:
    if override is None or getattr(override, "pk", None) is None:
        return []
    return list(override.bills.all())


def _overridden(ov) -> bool:
    return bool(
        ov.sap_company_code_override or ov.treatment_override or ov.item_code_override
        or ov.ocr_code_override or ov.ocr_code3_override
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class _Context:
    """Everything one plan reads, loaded once."""

    def __init__(self, bunch):
        self.bunch = bunch
        self.routing_rows = list(
            CashBranchSapRouting.objects.filter(branch__company_id=bunch.company_id)
            .select_related("branch")
        )
        self.routing_by_branch = {r.branch_id: r for r in self.routing_rows}
        self.gl_map = {
            (m.sap_company_code, m.gl_account_code): m
            for m in GLItemMap.objects.filter(is_active=True)
        }
        self.batches = {
            b.id: b
            for b in ImprestBatch.objects.filter(bunch_id=bunch.pk)
            .select_related("requested_by", "last_sent_by")
            .prefetch_related("grpos", "ap_drafts")
        }
        self.batch_by_company = {b.sap_company_code: b for b in self.batches.values()}
        self.grpos = {g.id: g for b in self.batches.values() for g in b.grpos.all()}
        self.home = home_company_code(bunch.company)
        self.state = BunchSapState.objects.filter(bunch_id=bunch.pk).first()

    def service_draft(self, batch_id):
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        return next((d for d in batch.ap_drafts.all() if d.kind == APDraftKind.SERVICE), None)


def _entries(bunch):
    return list(
        bunch.entries.filter(is_active=True)
        .select_related("branch")
        .prefetch_related("attachments")
        .order_by(F("serial_number").asc(nulls_last=True), "id")
    )


# ---------------------------------------------------------------------------
# One voucher
# ---------------------------------------------------------------------------


def is_frozen(override, ctx=None) -> bool:
    """Rule 3: the voucher's document is committed, so its snapshot stands.

    ITEM: the PO is QUEUED / IN_APPROVAL / POSTED (or has an SAP id), or its
    GRPO is, or it was left out after the PO. SERVICE: the batch's SERVICE
    draft is committed. SKIP and a NOT_NEEDED PO never freeze.
    """
    if override is None or getattr(override, "pk", None) is None:
        return False
    if override.left_out_after_po:
        return True
    batch_id = override.batch_id
    if batch_id is None:
        return False
    if ctx is not None:
        batch = ctx.batches.get(batch_id)
        grpo = ctx.grpos.get(override.grpo_id) if override.grpo_id else None
        service = ctx.service_draft(batch_id)
    else:
        batch = override.batch
        grpo = override.grpo
        service = (
            batch.ap_drafts.filter(kind=APDraftKind.SERVICE).first() if batch else None
        )
    if batch is None:
        return False
    if override.treatment == VoucherTreatment.ITEM:
        return bool(batch.po_committed or (grpo is not None and grpo.committed))
    if override.treatment == VoucherTreatment.SERVICE:
        return bool(service is not None and service.committed)
    return False


def _home_bunch_id(override):
    """The bunch whose documents hold this row: its batch's bunch, else the row's own."""
    if override.batch_id is not None:
        return override.batch.bunch_id
    return override.bunch_id


def frozen_elsewhere(override, bunch) -> bool:
    """Rule 3 across bunches: the row is frozen under a bunch other than ``bunch``.

    The cash book lets a voucher be taken out of an unsent bunch and put in
    another one, and posting does not wait for a bunch to be sent. The
    voucher's single row then still belongs to the first bunch's PO, GRPO or
    service draft; re-homing it here would post it to SAP a second time. Reads
    the row's batch, GRPO and service draft from the database (no plan
    context: the batch is another bunch's).
    """
    if override is None or getattr(override, "pk", None) is None:
        return False
    home = _home_bunch_id(override)
    if home is None or home == bunch.pk:
        return False
    return is_frozen(override)


def elsewhere_message(override, serial=None) -> str:
    """Why a voucher frozen under another bunch cannot be posted from this one."""
    other = override.batch.bunch if override.batch_id is not None else override.bunch
    number = other.number if other is not None else "?"
    which = f"Voucher {serial or override.entry_id}"
    if override.left_out_after_po:
        return (
            f"{which} was left out of bunch {number}'s PO after the PO was raised. Its SAP "
            f"record stays with bunch {number}, so it cannot be posted from this bunch as well. "
            f"Take it out of this bunch in the cash book, and key it into SAP by hand if it is "
            f"still owed."
        )
    return (
        f"{which} is already on its way to SAP with bunch {number}. Take it out of this "
        f"bunch in the cash book, or cancel that document in SAP and Re-check bunch {number} "
        f"first."
    )


#: What ``services._snapshot`` copies from a :class:`VoucherPlan` onto the
#: ``ImprestVoucher`` row at freeze -- and so what the payloads are built from.
SNAPSHOT_FIELDS = (
    "treatment", "item_code", "item_name", "gl_account_code", "gl_account_name",
    "account_code", "warehouse_code", "tax_code", "ocr_code", "ocr_code2", "ocr_code3",
    "ocr_code5", "bill_date", "gate_entry_no", "supplier_bill_no", "amount",
)


def _same(a, b) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    return a == b


def snapshot_changes(group, rows, treatment) -> List[str]:
    """How the batch's frozen ``treatment`` vouchers differ from what the plan freezes now.

    ``group`` is the plan's :class:`GroupPlan` for the batch's company (None
    when no voucher resolves to it any more); ``rows`` are the batch's
    ``ImprestVoucher`` rows. Vouchers the plan reads from their snapshot
    (frozen) and vouchers left out after the PO are not compared. ``[]`` means
    the snapshot is what a freeze would write now.

    Used for a PO or SERVICE draft that is not committed yet (PLANNED or
    FAILED): their vouchers can still be overridden, re-dated or changed in an
    accepted change, but the handlers build from the snapshot. So send-next
    freezes again first, and the handlers refuse a stale snapshot.
    """
    vouchers = group.vouchers if group is not None else []
    frozen_ids = {v.entry_id for v in vouchers if v.frozen}
    planned = {v.entry_id: v for v in vouchers if not v.frozen and v.treatment == treatment}
    held = {
        iv.entry_id: iv for iv in rows
        if iv.treatment == treatment and not iv.left_out_after_po and iv.entry_id not in frozen_ids
    }
    out = []
    for entry_id in sorted(set(planned) - set(held)):
        out.append(f"voucher {planned[entry_id].serial_number or entry_id} joined")
    for entry_id in sorted(set(held) - set(planned)):
        out.append(f"voucher {entry_id} left")
    for entry_id in sorted(set(planned) & set(held)):
        vp, iv = planned[entry_id], held[entry_id]
        fields = [name for name in SNAPSHOT_FIELDS if not _same(getattr(vp, name), getattr(iv, name))]
        if fields:
            out.append(f"voucher {vp.serial_number or entry_id} ({', '.join(fields)})")
    return out


def _from_snapshot(vp: VoucherPlan, ov, ctx) -> VoucherPlan:
    batch = ctx.batches.get(ov.batch_id) if ctx else ov.batch
    vp.frozen = True
    vp.treatment_source = SOURCE_FROZEN
    vp.treatment = ov.treatment or VoucherTreatment.SKIP
    vp.sap_company_code = batch.sap_company_code if batch else ""
    vp.skip_reason = ov.skip_reason
    for name in ("item_code", "item_name", "account_code", "warehouse_code", "tax_code",
                 "ocr_code", "ocr_code2", "ocr_code3", "ocr_code5", "gate_entry_no",
                 "supplier_bill_no", "po_line_num"):
        setattr(vp, name, getattr(ov, name))
    if ov.bill_date:
        vp.bill_date = ov.bill_date
    if ov.amount is not None:
        vp.amount = ov.amount
    vp.left_out_after_po = bool(ov.left_out_after_po)
    return vp


def _skip(vp, reason, source):
    vp.treatment = VoucherTreatment.SKIP
    vp.treatment_source = source
    vp.skip_reason = reason
    return vp


def resolve_voucher(entry, *, bunch, branch_routing, gl_map, override, home, today,
                    info=None, routing_rows=None, ctx=None) -> VoucherPlan:
    """One voucher through rules 1-20. ``override`` may be None (no row here)."""
    ov = override if override is not None else _NO_OVERRIDE
    bills = _bills(override)
    attachments = list(entry.attachments.all())
    bill_date = (info.bill_date if info else None) or entry.entry_date

    vp = VoucherPlan(
        entry_id=entry.id,
        serial_number=entry.serial_number,
        entry_date=entry.entry_date,
        bill_date=bill_date,
        gate_entry_no=(info.gate_entry_no if info else "") or "",
        supplier_bill_no=(info.supplier_bill_no if info else "") or "",
        branch_id=entry.branch_id,
        branch_name=entry.branch.name if entry.branch_id else "",
        gl_account_code=entry.gl_account_code or "",
        gl_account_name=entry.gl_account_name or "",
        item=entry.item or "",
        detail=entry.detail or "",
        amount=entry.amount,
        has_bill=bool(attachments or bills),
        bill_count=len(attachments) + len(bills),
        overridden=_overridden(ov),
        grpo_id=ov.grpo_id,
    )

    # 1. Not a payment.
    if entry.direction != CashDirection.OUT:
        return _skip(vp, "Cash received — never posted", SOURCE_RULE)

    # 2. Not approved.
    if entry.approval_state != EntryApprovalStatus.APPROVED:
        vp.problems.append(Problem(
            "NOT_APPROVED", f"Voucher {entry.serial_number or entry.id} is not approved."))
        return vp

    # 3. Frozen.
    if is_frozen(override, ctx):
        return _from_snapshot(vp, ov, ctx)

    # 4. Company.
    company = ov.sap_company_code_override or (
        branch_routing.sap_company_code if branch_routing and branch_routing.enabled else ""
    )
    vp.sap_company_code = company
    gl = vp.gl_account_code

    # 5. Salary advance.
    if gl in NEVER_POSTED_GL_CODES:
        return _skip(vp, "Salary advance — never posted", SOURCE_RULE)

    # 6. Override SKIP.
    if ov.treatment_override == VoucherTreatment.SKIP:
        reason = ov.override_note or f"Left out by {person_label(ov.overridden_by) or 'a poster'}"
        return _skip(vp, reason, SOURCE_OVERRIDE)

    # 8. No company.
    if not company:
        vp.problems.append(Problem(
            UNROUTED,
            f"Branch {vp.branch_name or '(none)'} has no SAP company. Set it in Cash → SAP "
            f"settings, choose a company for this voucher, or leave it out.",
        ))
        return vp

    # 9. Routing for that company.
    r = routing_for(bunch.company, company, preferred_branch=entry.branch_id, rows=routing_rows)
    if r is None:
        vp.problems.append(Problem(
            "NO_ROUTING_FOR_COMPANY",
            f"No enabled Cash → SAP routing posts to {company}. Enable one in settings "
            f"or choose another company for this voucher.",
        ))
        return vp
    vp.routing_id = r.id
    vp._routing = r  # noqa: SLF001 -- not serialised; the group header reads it

    # 10. Cut-over.
    if r.cutover_date and bill_date < r.cutover_date:
        return _skip(
            vp, f"Dated before the cut-over ({fmt(r.cutover_date)}) — keyed into SAP by hand",
            SOURCE_RULE,
        )

    # 11. Map.
    m = gl_map.get((company, gl))
    if m is not None and m.treatment == HeadTreatment.NEVER:
        return _skip(vp, f"Head {gl} is never posted", SOURCE_MAP)

    # 12. Treatment.
    if ov.treatment_override in (VoucherTreatment.ITEM, VoucherTreatment.SERVICE):
        treatment, source = ov.treatment_override, SOURCE_OVERRIDE
    elif m is not None:
        treatment = VoucherTreatment.ITEM if m.treatment == HeadTreatment.ITEM else VoucherTreatment.SERVICE
        source = SOURCE_MAP
    else:
        treatment, source = VoucherTreatment.SERVICE, SOURCE_DEFAULT
        vp.problems.append(Problem(
            "UNMAPPED_HEAD",
            f"Head {gl} has no SAP mapping; treated as a service line. Map it in settings "
            f"if it is goods.",
            blocking=False,
        ))
    if r.mode == PostingMode.AP_DRAFT_ONLY and treatment == VoucherTreatment.ITEM:
        treatment = VoucherTreatment.SERVICE
        vp.problems.append(Problem(
            "AP_ONLY_COMPANY",
            f"{company} takes cash vouchers as A/P service lines only.",
            blocking=False,
        ))
    vp.treatment, vp.treatment_source = treatment, source

    # 13. Item.
    if treatment == VoucherTreatment.ITEM:
        vp.item_code = ov.item_code_override or (m.item_code if m else "")
        vp.item_name = "" if ov.item_code_override else (m.item_name if m else "")
        if not vp.item_code:
            vp.problems.append(Problem(
                "NO_ITEM", f"Head {gl} has no SAP item for {company}. Map it or choose an item."))
        elif m is not None and m.needs_review and not ov.item_code_override:
            vp.problems.append(Problem(
                "ITEM_REVIEW",
                f"Head {gl} buys several items; {vp.item_code} is only the commonest. "
                f"Check it against the bill.",
                blocking=False,
            ))

    # 14. Would it be sent?
    would_send = (
        (treatment == VoucherTreatment.ITEM and r.mode == PostingMode.PO_GRPO)
        or (treatment == VoucherTreatment.SERVICE and r.ap_drafts_enabled)
    )

    # 15. Account in the target company.
    if company == home:
        account = (m.target_account_code if m else "") or gl
    else:
        account = m.target_account_code if m else ""
    vp.account_code = account
    if not account and would_send:
        vp.problems.append(Problem(
            "GL_NOT_IN_TARGET",
            f"G/L {gl} is {home or 'the cash book'}'s account. Map it to a {company} "
            f"account in Cash → SAP settings, or leave this voucher out.",
        ))

    # 16. Dims, tax, warehouse.
    vp.ocr_code = ov.ocr_code_override or r.ocr_code
    vp.ocr_code2 = month_code(bill_date)
    vp.ocr_code3 = ov.ocr_code3_override or (m.ocr_code3 if m else "") or r.ocr_code3
    vp.ocr_code5 = r.ocr_code5
    vp.tax_code = r.item_tax_code if treatment == VoucherTreatment.ITEM else r.service_tax_code
    vp.warehouse_code = r.warehouse_code

    # 18. Bills.
    if treatment == VoucherTreatment.ITEM and r.grpo_needs_attachment and not vp.has_bill:
        vp.problems.append(Problem(
            "NO_BILL",
            "No bill photo — SAP refuses a GRPO without an attachment. Add one or leave "
            "this voucher out.",
        ))

    # 19. Future date.
    if bill_date > today:
        vp.problems.append(Problem("FUTURE_DATE", "The bill date is in the future."))

    # 20. Warnings (age suppressed: the backlog is old by design).
    vp.warnings = voucher_date_warnings(
        entry.entry_date, info.bill_date if info else None, today=today, include_age=False
    )
    return vp


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def group_key_for(v, grouping) -> str:
    key = f"V{v.entry_id}"
    if grouping == GrpoGrouping.GATE_NO and (v.gate_entry_no or "").strip():
        key = f"N:{v.bill_date:%Y%m%d}:{v.gate_entry_no.strip().upper()}"
    elif grouping == GrpoGrouping.BILL_NO and (v.supplier_bill_no or "").strip():
        key = f"B:{v.bill_date:%Y%m%d}:{v.supplier_bill_no.strip().upper()}"
    return key[:160]


def group_vouchers(vouchers, grouping) -> "OrderedDict[str, list]":
    """ITEM vouchers into GRPO groups, keeping plan order (spec §2.3.6)."""
    groups: "OrderedDict[str, list]" = OrderedDict()
    for v in vouchers:
        if v.treatment != VoucherTreatment.ITEM:
            continue
        groups.setdefault(group_key_for(v, grouping), []).append(v)
    return groups


# ---------------------------------------------------------------------------
# Unit statuses
# ---------------------------------------------------------------------------


def newest_postings(pairs) -> Dict[tuple, object]:
    """``{(kind, source_id): newest SapPosting}`` for the given pairs, a query per kind."""
    from sap_postings.models import SapPosting

    by_kind: Dict[str, set] = {}
    for kind, source_id in pairs:
        by_kind.setdefault(kind, set()).add(source_id)
    out = {}
    for kind, ids in by_kind.items():
        for p in SapPosting.objects.filter(kind=kind, source_id__in=ids).order_by("source_id", "-id"):
            out.setdefault((kind, p.source_id), p)
    return out


def _posting_dict(p) -> Optional[dict]:
    if p is None:
        return None
    return {
        "id": p.pk,
        "status": p.status,
        "attempts": p.attempts,
        "last_error": p.last_error,
    }


def _unit(kind, source_id, status, postings, *, doc_num=None, error="", simulated=False,
          draft_entry=None, label="") -> UnitStatus:
    from sap_postings.models import SapPostingStatus

    p = postings.get((kind, source_id))
    stopped = bool(
        p is not None and p.status == SapPostingStatus.CANCELLED
        and status not in DONE_STATUSES
    )
    return UnitStatus(
        kind=kind, source_id=source_id, status=status, doc_num=doc_num, error=error,
        simulated=simulated, sap_posting=_posting_dict(p), draft_entry=draft_entry,
        stopped=stopped, label="Stopped" if stopped else label,
    )


def _batch_dict(b) -> dict:
    return {
        "id": b.id,
        "po_status": b.po_status,
        "po_doc_num": b.po_doc_num,
        "po_doc_entry": b.po_doc_entry,
        "po_draft_entry": b.po_draft_entry,
        "po_error": b.po_error,
        "po_round": b.po_round,
        "po_close_status": b.po_close_status,
        "po_close_error": b.po_close_error,
        "simulated": b.simulated,
        "marker": b.marker,
        "requested_by_name": person_label(b.requested_by),
        "requested_at": b.requested_at.isoformat() if b.requested_at else None,
        "last_sent_by_name": person_label(b.last_sent_by),
        "last_sent_at": b.last_sent_at.isoformat() if b.last_sent_at else None,
    }


# ---------------------------------------------------------------------------
# The bunch
# ---------------------------------------------------------------------------


def build_plan(bunch, *, today=None) -> BunchPlan:
    """The whole plan for one bunch. Reads only JI; never SAP."""
    today = today or timezone.localdate()
    ctx = _Context(bunch)
    entries = _entries(bunch)
    entry_ids = [e.id for e in entries]
    infos = {i.entry_id: i for i in VoucherSapInfo.objects.filter(entry_id__in=entry_ids)}
    overrides = {
        o.entry_id: o
        for o in ImprestVoucher.objects.filter(entry_id__in=entry_ids)
        .select_related("overridden_by", "batch__bunch", "bunch")
        .prefetch_related("bills")
    }

    voucher_plans: List[VoucherPlan] = []
    for entry in entries:
        ov = overrides.get(entry.id)
        elsewhere = ov if frozen_elsewhere(ov, bunch) else None
        if ov is not None and ov.bunch_id not in (None, bunch.pk):
            ov = None  # made in another bunch: does not carry over
        vp = resolve_voucher(
            entry,
            bunch=bunch,
            branch_routing=ctx.routing_by_branch.get(entry.branch_id),
            gl_map=ctx.gl_map,
            override=ov,
            home=ctx.home,
            today=today,
            info=infos.get(entry.id),
            routing_rows=ctx.routing_rows,
            ctx=ctx,
        )
        if elsewhere is not None:
            # Never re-planned here: freezing would re-home the row and post it twice.
            vp.problems.insert(0, Problem(IN_OTHER_BUNCH, elsewhere_message(elsewhere, entry.serial_number)))
            vp.treatment, vp.treatment_source = UNROUTED, SOURCE_RULE
            vp.skip_reason = ""
        vp._override = ov  # noqa: SLF001
        voucher_plans.append(vp)

    unit_pairs = []
    for b in ctx.batches.values():
        unit_pairs += [(KIND_PO, b.id), (KIND_PO_CLOSE, b.id)]
        unit_pairs += [(KIND_GRPO, g.id) for g in b.grpos.all()]
        unit_pairs += [(KIND_AP, d.id) for d in b.ap_drafts.all()]
    postings = newest_postings(unit_pairs)

    plan = BunchPlan(
        bunch={},
        enabled=flags.enabled(),
        simulate=flags.simulate(),
        allow_live=flags.allow_live(),
    )

    # Bunch-level drift.
    drift = {}
    if ctx.state is not None and ctx.state.is_frozen:
        drift = drift_mod.compare(ctx.state.member_snapshot, drift_mod.current_members(bunch))
        if drift:
            plan.problems.append(Problem(BUNCH_CHANGED, drift_mod.describe(drift, bunch.number)))

    by_company: "OrderedDict[str, List[VoucherPlan]]" = OrderedDict()
    for vp in voucher_plans:
        if vp.treatment == VoucherTreatment.SKIP:
            plan.skipped.append(vp)
        elif vp.treatment == UNROUTED:
            plan.unrouted.append(vp)
        else:
            by_company.setdefault(vp.sap_company_code, []).append(vp)

    elsewhere_vps = [v for v in plan.unrouted if any(p.code == IN_OTHER_BUNCH for p in v.problems)]
    if elsewhere_vps:
        plan.problems.append(Problem(
            IN_OTHER_BUNCH,
            f"{len(elsewhere_vps)} voucher(s) of this bunch "
            f"({', '.join(str(v.serial_number or v.entry_id) for v in elsewhere_vps)}) are already "
            f"on their way to SAP with another bunch. Take them out of this bunch in the cash "
            f"book before anything posts.",
        ))
    elsewhere_ids = {id(v) for v in elsewhere_vps}
    blocked_unrouted = [v for v in plan.unrouted if v.blocking and id(v) not in elsewhere_ids]
    if blocked_unrouted:
        codes = sorted({p.code for v in blocked_unrouted for p in v.problems if p.blocking})
        plan.problems.append(Problem(
            UNROUTED,
            f"{len(blocked_unrouted)} voucher(s) cannot be routed to SAP ({', '.join(codes)}). "
            f"Every voucher must be routed, or left out, before anything posts.",
        ))

    for company, vouchers in by_company.items():
        plan.groups.append(_group_plan(company, vouchers, ctx, postings))

    # Groups with a batch but no vouchers resolved to them any more (all
    # moved elsewhere) still show their units.
    for company, batch in ctx.batch_by_company.items():
        if company not in by_company:
            plan.groups.append(_group_plan(company, [], ctx, postings))

    if not any(g.sendable for g in plan.groups):
        plan.problems.append(Problem(
            "NOTHING_TO_POST", "Nothing in this bunch goes to SAP.", blocking=False))

    bunch_blocking = plan.blocking
    for g in plan.groups:
        g.can_post = bool(
            plan.enabled and g.sendable and not g.blocking and not bunch_blocking
            and _has_unsent(g, ctx)
        )
    plan.can_post = any(g.can_post for g in plan.groups)

    from .services import bunch_total

    plan.bunch = {
        "id": bunch.pk,
        "number": bunch.number,
        "remarks": bunch.remarks,
        "sent_at": bunch.sent_at.isoformat() if bunch.sent_at else None,
        "entry_count": len(entries),
        "total": bunch_total(bunch),
        "frozen_at": ctx.state.frozen_at.isoformat() if ctx.state and ctx.state.frozen_at else None,
        "sap_state": ctx.state.status if ctx.state else "OPEN",
        "drift": drift,
    }
    plan.summary = bunch_status_summary(
        bunch, sendable=any(g.sendable for g in plan.groups), ctx=ctx, drift=drift
    )
    return plan


def _group_plan(company, vouchers, ctx, postings) -> GroupPlan:
    g = GroupPlan(sap_company_code=company, vouchers=vouchers)
    batch = ctx.batch_by_company.get(company)
    unfrozen = [v for v in vouchers if not v.frozen]
    rows = []
    for v in unfrozen:
        r = getattr(v, "_routing", None)
        if r is not None and r not in rows:
            rows.append(r)
    rows.sort(key=lambda r: (r.branch.sort_order, r.branch_id))

    committed = bool(batch is not None and (
        batch.po_committed
        or any(gr.committed for gr in batch.grpos.all())
        or any(d.committed for d in batch.ap_drafts.all())
    ))
    r0 = rows[0] if rows else None

    if len({header_tuple(r) for r in rows}) > 1:
        names = ", ".join(r.branch.name for r in rows)
        g.problems.append(Problem(
            "ROUTING_CONFLICT",
            f"The routings of {names} disagree about how {company} is posted (vendor, "
            f"branch, warehouse, mode, drafts or grouping). Make them match.",
        ))

    head = batch if (committed or r0 is None) and batch is not None else r0
    if head is not None:
        g.mode = head.mode
        g.vendor_code = head.vendor_code
        g.bpl_id = head.bpl_id
        g.warehouse_code = head.warehouse_code
        g.ap_drafts_enabled = bool(head.ap_drafts_enabled)
        g.grpo_grouping = head.grpo_grouping
    g.is_test_target = flags.is_test_target(company)

    items = [v for v in vouchers if v.treatment == VoucherTreatment.ITEM]
    services = [v for v in vouchers if v.treatment == VoucherTreatment.SERVICE]
    g.item_total = sum((v.amount for v in items), Decimal("0"))
    g.service_total = sum((v.amount for v in services), Decimal("0"))
    g.total = g.item_total + g.service_total

    if r0 is not None:
        missing = []
        if g.mode == PostingMode.PO_GRPO and items:
            for name, value in (("vendor", g.vendor_code), ("SAP branch", g.bpl_id),
                                ("warehouse", g.warehouse_code), ("item tax code", r0.item_tax_code)):
                if value in (None, ""):
                    missing.append(name)
        if g.ap_drafts_enabled and services:
            for name, value in (("vendor", g.vendor_code), ("service tax code", r0.service_tax_code)):
                if value in (None, "") and name not in missing:
                    missing.append(name)
        if missing:
            g.problems.append(Problem(
                "ROUTING_INCOMPLETE",
                f"The {company} routing has no {', '.join(missing)}. Fill it in Cash → SAP settings.",
            ))

    g.sendable = bool((g.mode == PostingMode.PO_GRPO and items) or (g.ap_drafts_enabled and services))
    if services and not g.ap_drafts_enabled:
        g.notes.append("Service heads are not sent (A/P drafts are off); HO keys them by hand.")
    if not g.sendable:
        g.notes.append(f"Nothing for {company} goes to SAP.")

    if g.sendable and not flags.simulate() and not flags.allow_live() and not g.is_test_target:
        g.problems.append(Problem(
            "LIVE_SAP_BLOCKED",
            f"{company} is live SAP and CASH_SAP_ALLOW_LIVE is off: only TEST_ company "
            f"databases can be posted to.",
        ))
    if batch is not None and batch.po_committed:
        for v in unfrozen:
            if v.treatment == VoucherTreatment.ITEM:
                v.problems.append(Problem(
                    NOT_ON_PO,
                    f"This voucher became goods after PO {batch.po_doc_num or ''} was raised, "
                    f"so it has no line on it. Make it a service line or leave it out.",
                ))
    service_draft = ctx.service_draft(batch.id) if batch is not None else None
    if service_draft is not None and service_draft.committed and g.ap_drafts_enabled:
        # The SERVICE draft is in SAP (or on its way): a service voucher not on it
        # would be attached to the batch at the next Post, count as frozen, and
        # never be sent. Mirrors NOT_ON_PO.
        for v in unfrozen:
            if v.treatment == VoucherTreatment.SERVICE:
                v.problems.append(Problem(
                    NOT_ON_AP_DRAFT,
                    f"This voucher became a service line after the {company} A/P service draft "
                    f"{service_draft.sap_draft_entry or ''} was sent, so it is not on it. Make it "
                    f"goods while the PO is open, or leave it out and add it to the draft in SAP "
                    f"by hand.",
                ))
    if batch is not None and batch.simulated and not flags.simulate():
        g.problems.append(Problem(
            "SIMULATED_ONLY",
            "Posted in simulate mode only — nothing is in SAP. Reset this bunch to post for real.",
            blocking=False,
        ))

    # Dates.
    item_dates = [v.bill_date for v in items if v.bill_date]
    all_dates = [v.bill_date for v in vouchers if v.bill_date]
    base = item_dates or all_dates
    if base:
        first = min(base)
        g.expense_month = first.replace(day=1)
        g.po_doc_date = g.expense_month
    if item_dates:
        g.po_due_date = max(item_dates)
        if len({(d.year, d.month) for d in item_dates}) > 1:
            g.problems.append(Problem(
                "SPANS_MONTHS",
                f"PO dated {fmt(g.po_doc_date)}; GRPOs keep their own month in Dim2.",
                blocking=False,
            ))
    if batch is not None and batch.po_committed:
        g.expense_month = batch.expense_month or g.expense_month
        g.po_doc_date = batch.po_doc_date or g.po_doc_date
        g.po_due_date = batch.po_due_date or g.po_due_date

    # GRPOs and unit statuses.
    if batch is None:
        for key, members in group_vouchers(items, g.grpo_grouping).items():
            g.grpos.append({
                "id": None,
                "group_key": key,
                "voucher_ids": [m.entry_id for m in members],
                "bill_date": members[0].bill_date,
                "posting_date": members[0].bill_date,
                "gate_entry_no": ",".join(sorted({m.gate_entry_no for m in members if m.gate_entry_no})),
                "amount": sum((m.amount for m in members), Decimal("0")),
                "num_at_card": "",
                "status": SapDocStatus.PLANNED,
                "doc_num": None,
                "error": "",
                "simulated": False,
                "sap_posting": None,
            })
        return g

    g.batch = _batch_dict(batch)
    g.po = _unit(KIND_PO, batch.id, batch.po_status, postings, doc_num=batch.po_doc_num,
                 error=batch.po_error, simulated=batch.simulated,
                 draft_entry=batch.po_draft_entry, label="Imprest PO")
    g.po_close = _unit(KIND_PO_CLOSE, batch.id, batch.po_close_status, postings,
                       error=batch.po_close_error, simulated=batch.simulated,
                       label="Close the PO")
    rows = list(batch.vouchers.all())
    members_by_grpo: Dict[int, list] = {}
    for iv in rows:
        if iv.grpo_id and not iv.left_out_after_po:
            members_by_grpo.setdefault(iv.grpo_id, []).append(iv.entry_id)

    # A PO or SERVICE draft not committed yet, whose vouchers changed since the
    # freeze (override, bill facts, settings, accepted change). Send-next
    # freezes it again before sending; the handler refuses to send it stale.
    stale = []
    if (batch.mode == PostingMode.PO_GRPO and not batch.po_committed
            and batch.po_status in (SapDocStatus.PLANNED, SapDocStatus.FAILED)):
        stale += snapshot_changes(g, rows, VoucherTreatment.ITEM)
    if (service_draft is not None and not service_draft.committed
            and service_draft.status in (SapDocStatus.PLANNED, SapDocStatus.FAILED)):
        stale += snapshot_changes(g, rows, VoucherTreatment.SERVICE)
    if stale:
        g.problems.append(Problem(
            CHANGED_SINCE_FREEZE,
            f"Changed since it was frozen: {'; '.join(stale)}. The new values are frozen "
            f"again before the next send.",
            blocking=False,
        ))
    for gr in sorted(batch.grpos.all(), key=lambda x: x.id):
        unit = _unit(KIND_GRPO, gr.id, gr.status, postings, doc_num=gr.grpo_doc_num,
                     error=gr.error, simulated=gr.simulated, draft_entry=gr.grpo_draft_entry)
        g.grpos.append({
            "id": gr.id,
            "group_key": gr.group_key,
            "voucher_ids": sorted(members_by_grpo.get(gr.id, [])),
            "bill_date": gr.bill_date,
            "posting_date": gr.posting_date,
            "gate_entry_no": gr.gate_entry_no,
            "amount": gr.amount,
            "num_at_card": gr.num_at_card,
            "status": gr.status,
            "doc_num": gr.grpo_doc_num,
            "error": gr.error,
            "simulated": gr.simulated,
            "sap_posting": unit.sap_posting,
            "stopped": unit.stopped,
            "marker": gr.marker,
        })
    for d in sorted(batch.ap_drafts.all(), key=lambda x: x.id):
        g.ap_drafts.append(_unit(
            KIND_AP, d.id, d.status, postings, doc_num=d.sap_doc_num, error=d.error,
            simulated=d.simulated, draft_entry=d.sap_draft_entry,
            label=f"A/P {d.kind.lower()} draft",
        ))
    return g


def _has_unsent(g: GroupPlan, ctx) -> bool:
    batch = ctx.batch_by_company.get(g.sap_company_code)
    if batch is None:
        return True
    unfrozen_items = [v for v in g.vouchers if not v.frozen and v.treatment == VoucherTreatment.ITEM]
    unfrozen_services = [v for v in g.vouchers if not v.frozen and v.treatment == VoucherTreatment.SERVICE]
    if unfrozen_items and g.mode == PostingMode.PO_GRPO and not batch.po_committed:
        return True
    if unfrozen_services and g.ap_drafts_enabled:
        return True
    if batch.po_status in OPEN_STATUSES and batch.mode == PostingMode.PO_GRPO:
        return True
    if any(gr.status in OPEN_STATUSES for gr in batch.grpos.all()):
        return True
    if any(d.status in OPEN_STATUSES for d in batch.ap_drafts.all()):
        return True
    return batch.po_close_status in OPEN_STATUSES


# ---------------------------------------------------------------------------
# The one-line summary (the bunch list's badge)
# ---------------------------------------------------------------------------

STATE_LABELS = {
    "NOT_STARTED": "Not posted",
    "NOTHING_SENT": "Nothing goes to SAP",
    "BUNCH_CHANGED": "Changed after freezing",
    "NEEDS_ATTENTION": "Needs attention",
    "POSTED": "In SAP",
    "SIMULATED": "Simulated only",
    "IN_PROGRESS": "Posting",
}


def bunch_status_summary(bunch, *, sendable=None, ctx=None, drift=None) -> dict:
    """``{"bunch_id","state","label","po_doc_nums","grpo_posted","grpo_total","failed","simulated"}``.

    With no batches the state needs the plan (is anything sendable at all?);
    ``sendable`` passes that in when the caller already has it.
    """
    batches = list(ctx.batches.values()) if ctx is not None else list(
        ImprestBatch.objects.filter(bunch_id=bunch.pk).prefetch_related("grpos", "ap_drafts")
    )
    if drift is None:
        drift = drift_mod.state_drift(bunch)
    grpos = [g for b in batches for g in b.grpos.all()]
    drafts = [d for b in batches for d in b.ap_drafts.all()]
    statuses = (
        [b.po_status for b in batches if b.mode == PostingMode.PO_GRPO]
        + [b.po_close_status for b in batches]
        + [g.status for g in grpos]
        + [d.status for d in drafts]
    )
    failed = sum(1 for s in statuses if s == SapDocStatus.FAILED)
    simulated_rows = [b.simulated for b in batches]
    summary = {
        "bunch_id": bunch.pk,
        "po_doc_nums": [b.po_doc_num for b in batches if b.po_doc_num],
        "grpo_posted": sum(1 for g in grpos if g.status == SapDocStatus.POSTED),
        "grpo_total": sum(1 for g in grpos if g.status != SapDocStatus.NOT_NEEDED),
        "failed": failed,
        "simulated": bool(batches) and all(simulated_rows),
    }
    if not batches:
        if sendable is None:
            sendable = any(g.sendable for g in build_plan(bunch).groups)
        state = "NOT_STARTED" if sendable else "NOTHING_SENT"
    elif drift:
        state = "BUNCH_CHANGED"
    elif any(s in (SapDocStatus.FAILED, SapDocStatus.IN_APPROVAL) for s in statuses):
        state = "NEEDS_ATTENTION"
    elif statuses and all(s in DONE_STATUSES for s in statuses):
        state = "SIMULATED" if summary["simulated"] else "POSTED"
    else:
        state = "IN_PROGRESS"
    summary["state"] = state
    summary["label"] = STATE_LABELS[state]
    return summary
