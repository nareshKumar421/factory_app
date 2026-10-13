"""
Everything that changes cash book -> SAP state goes through here.

Three rules this module keeps, and why:

1. **At most one SAP write per HTTP request** (spec D16). ``post_bunch``
   freezes, queues and sends one unit; the browser then calls ``send_next``
   in a loop with a five-minute timeout. The worker (``run_sap_postings``)
   sends whatever the browser leaves behind. So a request is bounded by one
   writer's timeout, never by the size of the bunch.

2. **sap_postings is never called inside a transaction.** ``post_now`` /
   ``queue`` / ``retry`` / ``cancel`` are ``atomic(durable=True)``; called
   inside ``transaction.atomic()`` they raise. So every function here commits
   its own rows first and queues afterwards.

3. **Nothing posts while the bunch differs from what was frozen.** The cash
   book cannot be locked from here, so its members are snapshotted at the
   first freeze and compared before every freeze, post and send
   (``drift.py``). A difference raises :class:`BunchChanged` (400,
   ``code=BUNCH_CHANGED``).

User errors raise DRF ``ValidationError`` with a ``detail`` (and a ``code``
where the screen acts on it), which DRF returns as 400 (spec D19).

Row locks follow the cash book's rule (``cash_book/services.py:_lock_entries``):
never ``select_for_update()`` together with ``select_related`` on a nullable
FK -- PostgreSQL refuses the lock across an outer join. ``of=("self",)`` or no
joins at all.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, List, Optional

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from cash_book.models import CashBunch, CashDirection
from sap_postings import services as sap_postings
from sap_postings.models import ACTIVE_STATUSES, SapPosting, SapPostingStatus

from . import drift as drift_mod
from . import flags
from .constants import (
    BUNCH_CHANGED,
    IN_OTHER_BUNCH,
    KIND_AP,
    KIND_GRPO,
    KIND_PO,
    KIND_PO_CLOSE,
    NOT_ON_AP_DRAFT,
    NOT_ON_PO,
    OBJ_AP,
    OBJ_GRPO,
    OBJ_PO,
)
from .models import (
    DONE_STATUSES,
    APDraftKind,
    BunchSapState,
    BunchSapStatus,
    CashBranchSapRouting,
    GLItemMap,
    GrpoGrouping,
    HeadTreatment,
    ImprestAPDraft,
    ImprestBatch,
    ImprestGRPO,
    ImprestVoucher,
    ImprestVoucherBill,
    PostingMode,
    SapDocStatus,
    VoucherSapInfo,
    VoucherTreatment,
)
from .payloads import (
    ap_num_at_card,
    grpo_num_at_card,
    next_free_num_at_card,
    person_label,
)
from .plan import (
    SNAPSHOT_FIELDS,
    build_plan,
    bunch_status_summary,
    elsewhere_message,
    frozen_elsewhere,
    group_vouchers,
    is_frozen,
    snapshot_changes,
)
from .routing import home_company_code
from .titles import link_for, title_for_ap, title_for_close, title_for_grpo, title_for_po
from .uploads import check_upload
from .voucher_dates import check_voucher_dates, voucher_date_warnings

ZERO = Decimal("0.00")

__all__ = [
    "BunchChanged",
    "CashSapDisabled",
    "accept_bunch_changes",
    "attach_bill",
    "bunch_status_summaries",
    "bunch_status_summary",
    "bunch_total",
    "freeze_bunch",
    "gl_map_rows",
    "leave_out_after_po",
    "maybe_queue_item_ap_draft",
    "options",
    "post_bunch",
    "queue_grpo_units",
    "recheck_bunch",
    "redate_grpo",
    "remove_bill",
    "request_po_close",
    "reset_bunch",
    "retire_gl_map_row",
    "routing_rows",
    "save_gl_map_row",
    "save_routing",
    "send_next",
    "stale_snapshot",
    "stop_bunch",
    "update_voucher_info",
    "update_voucher_override",
    "voucher_info",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CashSapDisabled(ValidationError):
    """``CASH_SAP_ENABLED`` is off: 400 ``{"detail", "code": "CASH_SAP_DISABLED"}``."""

    default_code = "CASH_SAP_DISABLED"

    def __init__(self, message="Posting the cash book to SAP is switched off."):
        super().__init__({"detail": message, "code": self.default_code})


class BunchChanged(ValidationError):
    """The bunch differs from its freeze snapshot: 400 ``code=BUNCH_CHANGED``."""

    default_code = BUNCH_CHANGED

    def __init__(self, drift: dict, bunch_number=None):
        self.drift = drift
        super().__init__({
            "detail": drift_mod.describe(drift, bunch_number),
            "code": BUNCH_CHANGED,
            "drift": {k: [str(i) for i in v] if isinstance(v, list) else v
                      for k, v in drift.items()},
        })


def _refuse(message: str, code: Optional[str] = None):
    body = {"detail": message}
    if code:
        body["code"] = code
    raise ValidationError(body)


def error_text(exc) -> str:
    """A ValidationError (or anything) as one readable sentence, for logs and rows."""
    detail = getattr(exc, "detail", None)
    if detail is None:
        return str(exc)

    def flatten(value):
        if isinstance(value, dict):
            if "detail" in value:
                return flatten(value["detail"])
            return "; ".join(f"{k}: {flatten(v)}" for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return "; ".join(flatten(v) for v in value)
        return str(value)

    return flatten(detail)


# ---------------------------------------------------------------------------
# Small reads
# ---------------------------------------------------------------------------


def bunch_total(bunch) -> Decimal:
    """What the batch comes to -- the same sum as ``cash_book.services.bunch_total``.

    Computed here rather than imported, per the separate-mode rule, and so the
    A/P NumAtCard's envelope total can never be a different figure from the
    one the cash book prints on the export.
    """
    total = bunch.entries.filter(is_active=True).aggregate(total=Sum("amount"))["total"]
    return total or ZERO


def _newest(kind, source_id):
    return SapPosting.objects.filter(kind=kind, source_id=source_id).order_by("-id").first()


BEING_SENT = "This voucher's GRPO is being sent to SAP right now; try again in a minute."


def _grpo_being_sent(grpo_id, *, lock=False) -> bool:
    """Is a posting of this GRPO being sent (``SENDING``) right now?

    The GRPO handler does not change the row's status before its SAP write, so
    a GRPO being sent still reads PLANNED / QUEUED / FAILED; only its posting
    says SENDING. With ``lock`` (inside a transaction) the waiting postings
    are row-locked too: the worker claims with ``skip_locked`` and a person's
    Post or Retry waits on the lock, so neither can start sending until the
    caller commits -- and then the handler reads the members as they are.
    """
    qs = SapPosting.objects.filter(kind=KIND_GRPO, source_id=grpo_id, status__in=ACTIVE_STATUSES)
    if lock:
        qs = qs.select_for_update()
    return any(p.status == SapPostingStatus.SENDING for p in qs)


def bunch_status_summaries(company, bunch_ids) -> List[dict]:
    """The badge for each of up to 200 bunches of this company, in id order.

    Built for a list: a handful of queries for the whole page, never one plan
    per bunch. Building a plan for each of 47 never-posted bunches against the
    remote TEST database took long enough for the browser to give up (10 Oct
    2026), so a bunch with no batches is reported "Not posted" without its
    plan. Whether anything in it can go to SAP at all ("Nothing goes to SAP")
    is shown on the bunch's own page, which builds the plan anyway. Drift is
    only worked out for the bunches that are actually frozen.
    """
    bunches = list(
        CashBunch.objects.filter(company=company, id__in=list(bunch_ids)[:200]).order_by("id")
    )
    ids = [b.pk for b in bunches]
    batches_by_bunch = {}
    for batch in ImprestBatch.objects.filter(bunch_id__in=ids).prefetch_related("grpos", "ap_drafts"):
        batches_by_bunch.setdefault(batch.bunch_id, {})[batch.id] = batch
    frozen = {s.bunch_id for s in BunchSapState.objects.filter(bunch_id__in=ids) if s.is_frozen}
    out = []
    for bunch in bunches:
        ctx = _SummaryContext(batches=batches_by_bunch.get(bunch.pk, {}))
        drift = drift_mod.state_drift(bunch) if bunch.pk in frozen else {}
        out.append(bunch_status_summary(bunch, sendable=True, ctx=ctx, drift=drift))
    return out


@dataclass
class _SummaryContext:
    """The one part of a plan's context ``bunch_status_summary`` reads."""

    batches: dict


def options(company, user) -> dict:
    from .permissions import POST_PERMISSION, SETTINGS_PERMISSION

    codes = flags.sap_company_codes()
    return {
        "sap_companies": codes,
        "home_company": home_company_code(company),
        "modes": [{"value": v, "label": l} for v, l in PostingMode.choices],
        "treatments": [{"value": v, "label": l} for v, l in VoucherTreatment.choices],
        "head_treatments": [{"value": v, "label": l} for v, l in HeadTreatment.choices],
        "groupings": [{"value": v, "label": l} for v, l in GrpoGrouping.choices],
        "enabled": flags.enabled(),
        "simulate": flags.simulate(),
        "allow_live": flags.allow_live(),
        "send_series": flags.send_series(),
        "test_targets": {code: flags.is_test_target(code) for code in codes},
        "can_post": bool(user and user.has_perm(POST_PERMISSION)),
        "can_manage_settings": bool(user and user.has_perm(SETTINGS_PERMISSION)),
    }


# ---------------------------------------------------------------------------
# Settings: routing and the G/L item map
# ---------------------------------------------------------------------------

ROUTING_FIELDS = (
    "enabled", "sap_company_code", "mode", "vendor_code", "bpl_id", "warehouse_code",
    "ocr_code", "ocr_code3", "ocr_code5", "item_tax_code", "service_tax_code",
    "ap_drafts_enabled", "grpo_needs_attachment", "grpo_grouping",
    "po_series_hint", "grpo_series_hint", "ap_series_hint", "cutover_date",
)


def routing_rows(company) -> list:
    """``[(branch, routing or None)]`` for every branch of the company, retired too."""
    from cash_book.models import CashBranch

    branches = list(CashBranch.objects.filter(company=company).order_by("sort_order", "name"))
    rows = {
        r.branch_id: r
        for r in CashBranchSapRouting.objects.filter(branch__in=branches).select_related("updated_by")
    }
    return [(b, rows.get(b.id)) for b in branches]


def save_routing(*, user, company, branch, data) -> CashBranchSapRouting:
    """Create or update one branch's routing. Atomic; validated against the mode."""
    if branch.company_id != company.id:
        _refuse("That branch belongs to another company.")
    with transaction.atomic():
        row = (
            CashBranchSapRouting.objects.select_for_update().filter(branch=branch).first()
            or CashBranchSapRouting(branch=branch, created_by=user)
        )
        for name in ROUTING_FIELDS:
            if name not in data:
                continue
            value = data[name]
            if isinstance(value, str):
                value = value.strip()
            setattr(row, name, value)

        if row.sap_company_code and not flags.is_known_company(row.sap_company_code):
            raise ValidationError({"sap_company_code": f"{row.sap_company_code} is not a SAP company this app knows."})
        if row.mode not in PostingMode.values:
            raise ValidationError({"mode": "Unknown posting mode."})
        if row.grpo_grouping not in GrpoGrouping.values:
            raise ValidationError({"grpo_grouping": "Unknown GRPO grouping."})

        if row.enabled:
            needed = [("sap_company_code", "SAP company")]
            if row.mode == PostingMode.PO_GRPO:
                needed += [("vendor_code", "vendor"), ("bpl_id", "SAP branch"),
                           ("warehouse_code", "warehouse"), ("item_tax_code", "item tax code")]
            else:
                needed += [("vendor_code", "vendor")]
            if row.ap_drafts_enabled:
                needed += [("service_tax_code", "service tax code")]
            errors = {
                name: f"Needed while the routing is on ({label})."
                for name, label in needed
                if getattr(row, name) in (None, "")
            }
            if errors:
                raise ValidationError(errors)

        row.updated_by = user
        row.save()
    return row


def gl_map_rows(*, sap_company_code=None, include_retired=False):
    rows = GLItemMap.objects.all()
    if sap_company_code:
        rows = rows.filter(sap_company_code=sap_company_code)
    if not include_retired:
        rows = rows.filter(is_active=True)
    return rows.order_by("sap_company_code", "gl_account_code", "-is_active", "id")


GL_MAP_FIELDS = (
    "sap_company_code", "gl_account_code", "gl_account_name", "target_account_code",
    "treatment", "item_code", "item_name", "ocr_code3", "needs_review",
)


def save_gl_map_row(*, user, row=None, data) -> tuple:
    """Create or update a G/L head mapping. Returns ``(row, warnings)``.

    The item and the target account are checked in SAP when SAP answers; when
    it does not, the row is saved anyway with a warning, because a settings
    screen that cannot be used while HANA is down is worse than a mapping that
    is checked again the first time a document is refused.
    """
    from hdbcli import dbapi

    from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
    from sap_purchasing.client import PurchasingClient

    from cash_book.hana_reader import GLAccountReader

    row = row or GLItemMap(created_by=user)
    for name in GL_MAP_FIELDS:
        if name in data:
            value = data[name]
            setattr(row, name, value.strip() if isinstance(value, str) else value)

    if not flags.is_known_company(row.sap_company_code):
        raise ValidationError({"sap_company_code": f"{row.sap_company_code or '(blank)'} is not a SAP company this app knows."})
    if not row.gl_account_code:
        raise ValidationError({"gl_account_code": "Give the G/L code as the cash book records it."})
    if row.treatment not in HeadTreatment.values:
        raise ValidationError({"treatment": "Unknown treatment."})
    if row.treatment == HeadTreatment.ITEM and not row.item_code:
        raise ValidationError({"item_code": "A goods head needs the SAP item its GRPO line uses."})

    warnings = []
    if row.item_code:
        try:
            found = PurchasingClient(row.sap_company_code).item_flags([row.item_code])
        except (SAPConnectionError, dbapi.Error):
            warnings.append(f"SAP could not be read, so item {row.item_code} was not checked.")
        else:
            item = found.get(row.item_code)
            if item is None:
                raise ValidationError({"item_code": f"{row.item_code} is not an item in {row.sap_company_code}."})
            if item.get("frozen") or not item.get("valid", True):
                raise ValidationError({"item_code": f"{row.item_code} is inactive or frozen in SAP."})
            if not item.get("purchase", True):
                raise ValidationError({"item_code": f"{row.item_code} is not a purchase item in SAP."})
            if not row.item_name:
                row.item_name = (item.get("item_name") or "")[:200]
    if row.target_account_code:
        try:
            account = GLAccountReader(row.sap_company_code).resolve(row.target_account_code)
        except SAPConnectionError:
            warnings.append(f"SAP could not be read, so account {row.target_account_code} was not checked.")
        except (SAPDataError, SAPValidationError) as exc:
            raise ValidationError({"target_account_code": str(exc)})
        else:
            if not row.gl_account_name and account.get("account_name"):
                row.gl_account_name = account["account_name"][:255]

    with transaction.atomic():
        clash = GLItemMap.objects.filter(
            sap_company_code=row.sap_company_code,
            gl_account_code=row.gl_account_code,
            is_active=True,
        ).exclude(pk=row.pk)
        if row.is_active and clash.exists():
            raise ValidationError({"gl_account_code": f"{row.gl_account_code} is already mapped for {row.sap_company_code}."})
        row.updated_by = user
        row.save()
    return row, warnings


def retire_gl_map_row(*, user, row) -> GLItemMap:
    row.is_active = False
    row.updated_by = user
    row.save(update_fields=["is_active", "updated_by", "updated_at"])
    return row


# ---------------------------------------------------------------------------
# One voucher: its bill facts, overrides and posting bills
# ---------------------------------------------------------------------------

INFO_FIELDS = ("bill_date", "gate_entry_no", "supplier_bill_no")
INFO_LIMITS = {"gate_entry_no": 30, "supplier_bill_no": 60}


def voucher_info(entry) -> dict:
    info = VoucherSapInfo.objects.filter(entry=entry).first()
    bill_date = info.bill_date if info else None
    return {
        "entry_id": entry.id,
        "bill_date": bill_date,
        "effective_bill_date": bill_date or entry.entry_date,
        "gate_entry_no": info.gate_entry_no if info else "",
        "supplier_bill_no": info.supplier_bill_no if info else "",
        "updated_by_name": person_label(info.updated_by) if info else "",
        "updated_at": info.updated_at if info else None,
        "warnings": voucher_date_warnings(entry.entry_date, bill_date),
    }


def _refuse_if_committed(entry):
    iv = ImprestVoucher.objects.filter(entry=entry).select_related("batch", "grpo").first()
    if iv is not None and is_frozen(iv):
        _refuse(
            "This voucher is already on its way to SAP. Use 'Leave out' or change the "
            "GRPO's date and G.no. instead."
        )


def _apply_info(*, user, entry, data) -> Optional[VoucherSapInfo]:
    """Write the bill facts in ``data`` (any of ``INFO_FIELDS``). Inside a transaction."""
    if not any(name in data for name in INFO_FIELDS):
        return None
    if entry.direction != CashDirection.OUT:
        _refuse("Cash received has no bill to describe.")
    info = VoucherSapInfo.objects.select_for_update().filter(entry=entry).first() or VoucherSapInfo(entry=entry)
    if "bill_date" in data:
        info.bill_date = data["bill_date"] or None
        check_voucher_dates(entry.entry_date, info.bill_date)
    for name, limit in INFO_LIMITS.items():
        if name in data:
            value = (data[name] or "").strip()
            if len(value) > limit:
                raise ValidationError({name: f"At most {limit} characters."})
            setattr(info, name, value)
    info.updated_by = user
    info.save()
    return info


def update_voucher_info(*, user, entry, data) -> dict:
    """The bill date, G.no. and bill no. of one voucher, until it is committed."""
    with transaction.atomic():
        _refuse_if_committed(entry)
        _apply_info(user=user, entry=entry, data=data)
    return voucher_info(entry)


OVERRIDE_FIELDS = {
    "sap_company_code": "sap_company_code_override",
    "treatment": "treatment_override",
    "item_code": "item_code_override",
    "ocr_code": "ocr_code_override",
    "ocr_code3": "ocr_code3_override",
    "note": "override_note",
}


def _clear_overrides(iv):
    for attr in OVERRIDE_FIELDS.values():
        setattr(iv, attr, "")
    iv.overridden_by = None
    iv.overridden_at = None


def update_voucher_override(*, user, bunch, entry, data) -> ImprestVoucher:
    """How one voucher posts: company, treatment, item, Dim1, Dim3, note -- and its bill facts.

    Refused once the voucher's document is committed (plan rule 3).
    """
    if entry.bunch_id != bunch.pk or entry.company_id != bunch.company_id:
        _refuse("That voucher is not in this bunch.")
    with transaction.atomic():
        iv = (
            ImprestVoucher.objects.select_for_update(of=("self",)).filter(entry=entry).first()
            or ImprestVoucher.objects.create(entry=entry, bunch=bunch, created_by=user, updated_by=user)
        )
        if frozen_elsewhere(iv, bunch):
            _refuse(elsewhere_message(iv, entry.serial_number), code=IN_OTHER_BUNCH)
        if iv.bunch_id != bunch.pk:
            _clear_overrides(iv)
            iv.bunch = bunch
        if is_frozen(iv):
            _refuse(
                "This voucher is already on its way to SAP. Use 'Leave out' or change the "
                "GRPO's date and G.no. instead."
            )

        if "sap_company_code" in data:
            code = (data["sap_company_code"] or "").strip()
            if code and not flags.is_known_company(code):
                raise ValidationError({"sap_company_code": f"{code} is not a SAP company this app knows."})
            iv.sap_company_code_override = code
        if "treatment" in data:
            treatment = (data["treatment"] or "").strip().upper()
            if treatment and treatment not in VoucherTreatment.values:
                raise ValidationError({"treatment": "Choose goods, service, not posted, or nothing."})
            iv.treatment_override = treatment
        if "item_code" in data:
            item = (data["item_code"] or "").strip()
            if len(item) > 50:
                raise ValidationError({"item_code": "At most 50 characters."})
            iv.item_code_override = item
        for key in ("ocr_code", "ocr_code3"):
            if key in data:
                value = (data[key] or "").strip()
                if len(value) > 30:
                    raise ValidationError({key: "At most 30 characters."})
                setattr(iv, OVERRIDE_FIELDS[key], value)
        if "note" in data:
            iv.override_note = (data["note"] or "").strip()

        _apply_info(user=user, entry=entry, data=data)
        iv.overridden_by = user
        iv.overridden_at = timezone.now()
        iv.updated_by = user
        iv.save()
    return iv


def attach_bill(*, user, entry, upload, bunch=None) -> ImprestVoucherBill:
    """A posting bill for a voucher whose cash-book bill is missing or locked."""
    if not entry.is_active or entry.direction != CashDirection.OUT:
        _refuse("Only an active payment can be given a bill.")
    if bunch is not None and entry.bunch_id != bunch.pk:
        _refuse("That voucher is not in this bunch.")
    name, size = check_upload(upload)
    with transaction.atomic():
        iv = (
            ImprestVoucher.objects.select_for_update(of=("self",)).filter(entry=entry).first()
            or ImprestVoucher.objects.create(entry=entry, bunch=entry.bunch, created_by=user, updated_by=user)
        )
        if entry.bunch_id is not None and frozen_elsewhere(iv, entry.bunch):
            # The row's GRPO is another bunch's: a bill added here would go with it.
            _refuse(elsewhere_message(iv, entry.serial_number), code=IN_OTHER_BUNCH)
        grpo = ImprestGRPO.objects.filter(pk=iv.grpo_id).first() if iv.grpo_id else None
        if grpo is not None and grpo.sap_attachment_entry:
            _refuse("SAP already has this GRPO's attachments; add the file in SAP.")
        return ImprestVoucherBill.objects.create(
            voucher=iv,
            file=upload,
            original_filename=name,
            size_bytes=size,
            created_by=user,
            updated_by=user,
        )


def remove_bill(*, user, bill) -> None:
    grpo = bill.voucher.grpo
    if grpo is not None and bill.key in (grpo.uploaded_files or []):
        _refuse("This bill is already in SAP; it cannot be taken off here.")
    bill.file.delete(save=False)
    bill.delete()


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def record_drift(bunch, drift: dict) -> None:
    BunchSapState.objects.filter(bunch_id=bunch.pk).update(
        drift=drift,
        status=BunchSapStatus.CHANGED if drift else BunchSapStatus.FROZEN,
        checked_at=timezone.now(),
    )


def check_drift(bunch) -> None:
    """Raise :class:`BunchChanged` when a frozen bunch's members moved. Records it."""
    state = BunchSapState.objects.filter(bunch_id=bunch.pk).first()
    if state is None or not state.is_frozen:
        return
    drift = drift_mod.compare(state.member_snapshot, drift_mod.current_members(bunch))
    if drift or state.status == BunchSapStatus.CHANGED:
        record_drift(bunch, drift)
    if drift:
        raise BunchChanged(drift, bunch.number)


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------


def _committed_po(batch) -> bool:
    return bool(batch.po_doc_entry is not None or (batch.simulated and batch.po_status == SapDocStatus.POSTED))


def _allocate(row, base: str, taken: Callable[[str], bool], *, field="num_at_card") -> str:
    """Save the first free ``base`` / ``base.`` ... on ``row``; one retry on a clash."""
    for _attempt in range(2):
        candidate = next_free_num_at_card(base, taken)
        try:
            with transaction.atomic():
                type(row).objects.filter(pk=row.pk).update(**{field: candidate})
            setattr(row, field, candidate)
            return candidate
        except IntegrityError:
            continue
    _refuse(f"Could not give {base} a free vendor reference; try again.")


def _grpo_taken(row):
    def taken(candidate):
        return ImprestGRPO.objects.filter(
            sap_company_code=row.sap_company_code,
            vendor_code=row.vendor_code,
            num_at_card=candidate,
        ).exclude(pk=row.pk).exists()
    return taken


def _ap_taken(row):
    def taken(candidate):
        return ImprestAPDraft.objects.filter(
            sap_company_code=row.sap_company_code,
            vendor_code=row.vendor_code,
            num_at_card=candidate,
        ).exclude(pk=row.pk).exists()
    return taken


def allocate_grpo_num_at_card(grpo) -> str:
    return _allocate(grpo, grpo_num_at_card(grpo.amount, grpo.bill_date), _grpo_taken(grpo))


def _snapshot(iv, vp, batch):
    """Copy the plan's answer onto the row. ``SNAPSHOT_FIELDS`` is the one list,
    shared with :func:`plan.snapshot_changes`, so "stale" means exactly "a
    freeze now would write something else"."""
    iv.batch = batch
    for name in SNAPSHOT_FIELDS:
        setattr(iv, name, getattr(vp, name))
    iv.skip_reason = ""


def _voucher_row(user, bunch, entry_id) -> ImprestVoucher:
    """The voucher's row, locked, made this bunch's.

    A row frozen under another bunch is never re-homed (the plan blocks it as
    ``IN_OTHER_BUNCH`` first; this is the last line): re-homing would move its
    batch and GRPO here and post it to SAP a second time.
    """
    iv = ImprestVoucher.objects.select_for_update(of=("self",)).filter(entry_id=entry_id).first()
    if iv is None:
        iv = ImprestVoucher.objects.create(entry_id=entry_id, bunch=bunch, created_by=user, updated_by=user)
    if frozen_elsewhere(iv, bunch):
        _refuse(elsewhere_message(iv), code=IN_OTHER_BUNCH)
    if iv.bunch_id != bunch.pk:
        _clear_overrides(iv)
        iv.bunch = bunch
    return iv


def freeze_bunch(*, user, bunch, sap_company_code=None) -> List[ImprestBatch]:
    """Lock the bunch, snapshot it, and lay out its SAP documents. Atomic.

    Only the groups that send something are frozen (a Beverages group with
    A/P drafts off is never frozen and reports NOTHING_SENT). A group whose PO
    is not committed yet has its lines and GRPO groups rebuilt; once the PO is
    committed the groups are fixed and only "Leave out" changes them.
    """
    check_drift(bunch)
    now = timezone.now()
    with transaction.atomic():
        bunch = CashBunch.objects.select_for_update().get(pk=bunch.pk)
        state, _ = BunchSapState.objects.get_or_create(bunch=bunch)
        state = BunchSapState.objects.select_for_update().get(pk=state.pk)
        current = drift_mod.current_members(bunch)
        if state.is_frozen:
            drift = drift_mod.compare(state.member_snapshot, current)
            if drift:
                raise BunchChanged(drift, bunch.number)

        plan = build_plan(bunch)
        groups = [
            g for g in plan.groups
            if g.sendable and (not sap_company_code or g.sap_company_code == sap_company_code)
        ]
        messages = [p.message for p in plan.problems if p.blocking]
        for g in groups:
            messages += [p.message for p in g.problems if p.blocking]
            for v in g.vouchers:
                if not v.frozen:
                    messages += [
                        f"Voucher {v.serial_number or v.entry_id}: {p.message}"
                        for p in v.problems if p.blocking
                    ]
        if messages:
            _refuse("; ".join(messages))
        if not groups:
            _refuse("Nothing in this bunch goes to SAP.")

        if not state.is_frozen:
            state.frozen_at = now
            state.frozen_by = user
            state.member_snapshot = current
            state.status = BunchSapStatus.FROZEN
            state.drift = {}
            state.save()

        batches = [_freeze_group(user, bunch, g, now) for g in groups]

        for vp in plan.skipped:
            if vp.frozen:
                continue
            iv = _voucher_row(user, bunch, vp.entry_id)
            iv.treatment = VoucherTreatment.SKIP
            iv.skip_reason = vp.skip_reason[:255]
            iv.batch = None
            iv.grpo = None
            iv.po_line_num = None
            iv.updated_by = user
            iv.save()
    return batches


def _freeze_group(user, bunch, g, now) -> ImprestBatch:
    batch, _ = ImprestBatch.objects.get_or_create(
        bunch=bunch, sap_company_code=g.sap_company_code,
        defaults={"created_by": user, "updated_by": user},
    )
    batch = ImprestBatch.objects.select_for_update(of=("self",)).get(pk=batch.pk)
    unfrozen = [v for v in g.vouchers if not v.frozen]
    items = [v for v in unfrozen if v.treatment == VoucherTreatment.ITEM]
    services = [v for v in unfrozen if v.treatment == VoucherTreatment.SERVICE]
    rows = sorted(
        {id(getattr(v, "_routing")): getattr(v, "_routing") for v in unfrozen if getattr(v, "_routing", None)}.values(),
        key=lambda r: (r.branch.sort_order, r.branch_id),
    )
    routing = rows[0] if rows else None

    po_free = (
        batch.po_status in (SapDocStatus.PLANNED, SapDocStatus.FAILED, SapDocStatus.NOT_NEEDED)
        and batch.po_doc_entry is None
        and batch.po_draft_entry is None
    )
    if po_free:
        if routing is not None:
            for name in ("mode", "vendor_code", "bpl_id", "warehouse_code", "ap_drafts_enabled",
                         "grpo_needs_attachment", "grpo_grouping", "po_series_hint",
                         "grpo_series_hint", "ap_series_hint"):
                setattr(batch, name, getattr(routing, name))
        batch.expense_month = g.expense_month
        batch.po_doc_date = g.po_doc_date
        batch.po_due_date = g.po_due_date
        batch.simulated = flags.simulate()
        batch.requested_by = user
        batch.requested_at = now
        planned = batch.mode == PostingMode.PO_GRPO and bool(items)
        batch.po_status = SapDocStatus.PLANNED if planned else SapDocStatus.NOT_NEEDED
        batch.po_error = ""
        batch.po_payload = {}
    batch.updated_by = user
    batch.save()

    current_ids = {v.entry_id for v in unfrozen}
    if po_free:
        # Vouchers that were on this batch and now resolve elsewhere.
        stale = ImprestVoucher.objects.filter(batch=batch, left_out_after_po=False).exclude(
            entry_id__in=current_ids
        )
        for iv in stale:
            if not is_frozen(iv):
                ImprestVoucher.objects.filter(pk=iv.pk).update(batch=None, grpo=None, po_line_num=None)
        ImprestVoucher.objects.filter(batch=batch).update(grpo=None)

    service_draft = ImprestAPDraft.objects.filter(batch=batch, kind=APDraftKind.SERVICE).first()
    service_free = service_draft is None or not service_draft.committed

    vrows = {}
    for vp in unfrozen:
        if vp.treatment == VoucherTreatment.ITEM and not po_free:
            continue  # the plan blocks a new goods voucher on a committed PO
        if vp.treatment == VoucherTreatment.SERVICE and not service_free:
            # Attached now it would count as frozen (the draft is committed) and
            # never be sent. The plan blocks it (NOT_ON_AP_DRAFT) while drafts are on.
            continue
        iv = _voucher_row(user, bunch, vp.entry_id)
        _snapshot(iv, vp, batch)
        if vp.treatment == VoucherTreatment.SERVICE:
            iv.po_line_num = None
            iv.grpo = None
        iv.updated_by = user
        iv.save()
        vrows[vp.entry_id] = iv

    if po_free:
        for line_num, vp in enumerate(items):
            ImprestVoucher.objects.filter(pk=vrows[vp.entry_id].pk).update(po_line_num=line_num)
            vrows[vp.entry_id].po_line_num = line_num
        _rebuild_grpos(user, batch, items, vrows)

    _allocate_grpo_numbers(batch)
    # The A/P drafts cover the whole group, frozen vouchers included: after the
    # PO is in SAP every goods voucher is frozen, and the ITEM draft still needs them.
    all_items = [v for v in g.vouchers if v.treatment == VoucherTreatment.ITEM]
    all_services = [v for v in g.vouchers if v.treatment == VoucherTreatment.SERVICE]
    _freeze_ap_drafts(user, bunch, batch, all_items, all_services)
    batch.refresh_from_db()
    return batch


def _rebuild_grpos(user, batch, items, vrows):
    """Recreate the batch's GRPO groups from its ITEM vouchers (PO not committed)."""
    groups = group_vouchers(items, batch.grpo_grouping)
    existing = {
        gr.group_key: gr
        for gr in ImprestGRPO.objects.select_for_update(of=("self",)).filter(batch=batch)
    }
    for key, gr in list(existing.items()):
        editable = (
            gr.status in (SapDocStatus.PLANNED, SapDocStatus.FAILED, SapDocStatus.QUEUED, SapDocStatus.NOT_NEEDED)
            and gr.grpo_doc_entry is None and gr.grpo_draft_entry is None
        )
        if not editable:
            continue
        if gr.sap_attachment_entry is None and key not in groups:
            gr.delete()
            existing.pop(key)
        elif key not in groups:
            # SAP already holds an attachment entry for it: keep the row (and the
            # entry), with nobody on it, in case the key comes back.
            ImprestGRPO.objects.filter(pk=gr.pk).update(
                status=SapDocStatus.NOT_NEEDED, amount=ZERO, num_at_card="", payload={}
            )

    for key, members in groups.items():
        amount = sum((m.amount for m in members), ZERO)
        bill_date = members[0].bill_date
        gnos = []
        for m in members:
            gno = (m.gate_entry_no or "").strip()
            if gno and gno not in gnos:
                gnos.append(gno)
        bill_no = next((m.supplier_bill_no.strip() for m in members if (m.supplier_bill_no or "").strip()), "")
        gr = existing.get(key)
        if gr is not None and not (gr.grpo_doc_entry is None and gr.grpo_draft_entry is None):
            continue
        same_ref = gr is not None and gr.amount == amount and gr.bill_date == bill_date
        fields = {
            "sap_company_code": batch.sap_company_code,
            "vendor_code": batch.vendor_code,
            "bill_date": bill_date,
            "posting_date": bill_date,
            "gate_entry_no": ",".join(gnos)[:60],
            "supplier_bill_no": bill_no[:60],
            "amount": amount,
            "status": SapDocStatus.PLANNED,
            "error": "",
            "payload": {},
            "simulated": batch.simulated,
            "updated_by": user,
        }
        if gr is None:
            gr = ImprestGRPO.objects.create(batch=batch, group_key=key, created_by=user, **fields)
        else:
            if not same_ref:
                fields["num_at_card"] = ""
            ImprestGRPO.objects.filter(pk=gr.pk).update(**fields)
        ImprestVoucher.objects.filter(pk__in=[vrows[m.entry_id].pk for m in members]).update(grpo=gr)


def _allocate_grpo_numbers(batch):
    """NumAtCard for each GRPO that has none, in GRPO id order (deterministic, D12)."""
    for gr in ImprestGRPO.objects.filter(batch=batch, num_at_card="").exclude(
        status=SapDocStatus.NOT_NEEDED
    ).order_by("id"):
        allocate_grpo_num_at_card(gr)


def _freeze_ap_drafts(user, bunch, batch, items, services):
    if not batch.ap_drafts_enabled:
        return
    total = bunch_total(bunch)
    wanted = []
    if services:
        wanted.append((APDraftKind.SERVICE, services))
    if batch.mode == PostingMode.PO_GRPO and items:
        wanted.append((APDraftKind.ITEM, items))
    kinds = {k for k, _ in wanted}
    for kind, vouchers in wanted:
        d, _ = ImprestAPDraft.objects.get_or_create(
            batch=batch, kind=kind,
            defaults={"sap_company_code": batch.sap_company_code, "vendor_code": batch.vendor_code,
                      "created_by": user, "updated_by": user},
        )
        if d.committed and d.status != SapDocStatus.FAILED:
            continue
        if d.sap_draft_entry is not None or d.sap_doc_entry is not None:
            continue
        amount = sum((v.amount for v in vouchers), ZERO)
        d.sap_company_code = batch.sap_company_code
        d.vendor_code = batch.vendor_code
        d.simulated = batch.simulated
        d.amount = amount
        d.doc_date = max(v.bill_date for v in vouchers) if kind == APDraftKind.SERVICE else None
        d.status = SapDocStatus.PLANNED
        d.error = ""
        d.payload = {}
        d.updated_by = user
        base = ap_num_at_card(batch.expense_month, total, amount)
        keep = d.num_at_card and d.num_at_card.rstrip(".") == base
        d.save()
        if not keep:
            ImprestAPDraft.objects.filter(pk=d.pk).update(num_at_card="")
            d.num_at_card = ""
            _allocate(d, base, _ap_taken(d))
    for d in ImprestAPDraft.objects.filter(batch=batch).exclude(kind__in=kinds):
        if not d.committed:
            ImprestAPDraft.objects.filter(pk=d.pk).update(status=SapDocStatus.NOT_NEEDED)


# ---------------------------------------------------------------------------
# Posting: post, send-next, units
# ---------------------------------------------------------------------------


@dataclass
class Unit:
    kind: str
    source_id: int
    title: str
    label: str
    model: type
    status_field: str
    notify_person: bool


def _ready(include_failed):
    statuses = [SapDocStatus.PLANNED, SapDocStatus.QUEUED]
    if include_failed:
        statuses += [SapDocStatus.FAILED, SapDocStatus.IN_APPROVAL]
    return statuses


def _units(bunch, include_failed=False, *, everything=False) -> List[Unit]:
    """The bunch's units in sending order: POs, SERVICE drafts, GRPOs, ITEM drafts, PO closes.

    Each kind is filtered by its own ids, so a GRPO id is never read as a PO id.
    ``everything`` lists every unit whatever its status (stop / reset).
    """
    ready = _ready(include_failed)
    batches = list(ImprestBatch.objects.filter(bunch_id=bunch.pk).select_related("bunch").order_by("id"))
    units: List[Unit] = []

    def ok(status):
        return everything or status in ready

    for b in batches:
        if everything or (b.mode == PostingMode.PO_GRPO and ok(b.po_status)):
            units.append(Unit(KIND_PO, b.id, title_for_po(b), "Imprest PO", ImprestBatch, "po_status", True))
    drafts = list(ImprestAPDraft.objects.filter(batch__bunch_id=bunch.pk).select_related("batch__bunch").order_by("id"))
    for d in drafts:
        if d.kind == APDraftKind.SERVICE and ok(d.status):
            units.append(Unit(KIND_AP, d.id, title_for_ap(d), "A/P service draft", ImprestAPDraft, "status", True))
    for b in batches:
        grpos = list(b.grpos.select_related("batch__bunch").order_by("id"))
        for gr in grpos:
            if everything:
                units.append(Unit(KIND_GRPO, gr.id, title_for_grpo(gr), "Voucher GRPO", ImprestGRPO, "status", False))
                continue
            if b.po_status != SapDocStatus.POSTED or gr.status not in ready:
                continue
            if not gr.vouchers.filter(left_out_after_po=False).exists():
                continue
            units.append(Unit(KIND_GRPO, gr.id, title_for_grpo(gr), "Voucher GRPO", ImprestGRPO, "status", False))
    for d in drafts:
        if d.kind != APDraftKind.ITEM or not ok(d.status):
            continue
        if not everything:
            batch_grpos = ImprestGRPO.objects.filter(batch_id=d.batch_id)
            if batch_grpos.exclude(status__in=DONE_STATUSES).exists():
                continue
        units.append(Unit(KIND_AP, d.id, title_for_ap(d), "A/P item draft", ImprestAPDraft, "status", False))
    for b in batches:
        if everything or ok(b.po_close_status):
            if everything and b.po_close_status == SapDocStatus.NOT_NEEDED:
                continue
            units.append(Unit(KIND_PO_CLOSE, b.id, title_for_close(b), "Close the PO", ImprestBatch, "po_close_status", True))
    return units


def _count_units(bunch, include_failed=False) -> int:
    return len(_units(bunch, include_failed))


def _touch_sender(unit: Unit, user):
    unit.model.objects.filter(pk=unit.source_id).update(last_sent_by=user, last_sent_at=timezone.now())


def _error_field(unit: Unit) -> str:
    if unit.status_field == "po_close_status":
        return "po_close_error"
    if unit.status_field == "po_status":
        return "po_error"
    return "error"


def _fail_unit(unit: Unit, message: str) -> None:
    unit.model.objects.filter(pk=unit.source_id).exclude(
        **{f"{unit.status_field}__in": DONE_STATUSES}
    ).update(**{unit.status_field: SapDocStatus.FAILED, _error_field(unit): message[:5000]})


STALE_MESSAGE = (
    "Changed since it was frozen ({changes}). Press Post (or Retry) on the bunch page: the "
    "new values are frozen again before it is sent."
)


def stale_snapshot(batch, treatment, plan=None) -> List[str]:
    """``snapshot_changes`` for one batch's PO (``ITEM``) or SERVICE draft (``SERVICE``).

    ``[]`` when the frozen vouchers are what a freeze would write now. Builds
    the plan unless one is passed. Only meaningful while that document is not
    committed: once it is, its vouchers are frozen and read from the snapshot.
    """
    plan = plan or build_plan(batch.bunch)
    rows = list(ImprestVoucher.objects.filter(batch=batch))
    return snapshot_changes(plan.group(batch.sap_company_code), rows, treatment)


def _unit_snapshot_changes(unit: Unit) -> tuple:
    """``(batch, changes)`` for a PO or SERVICE draft unit not yet committed; else ``(None, [])``."""
    if unit.kind == KIND_PO:
        batch = ImprestBatch.objects.select_related("bunch").get(pk=unit.source_id)
        if batch.po_committed or batch.po_status not in (SapDocStatus.PLANNED, SapDocStatus.FAILED):
            return None, []
        return batch, stale_snapshot(batch, VoucherTreatment.ITEM)
    if unit.kind == KIND_AP:
        d = ImprestAPDraft.objects.select_related("batch__bunch").get(pk=unit.source_id)
        if d.kind != APDraftKind.SERVICE or d.committed or d.status not in (
            SapDocStatus.PLANNED, SapDocStatus.FAILED
        ):
            return None, []
        return d.batch, stale_snapshot(d.batch, VoucherTreatment.SERVICE)
    return None, []


def send_next(*, user, bunch, include_failed=False, skip_posting_ids=()) -> dict:
    """Send the next unit of the bunch -- at most one SAP write -- and return the plan.

    A unit whose newest posting is waiting or refused is retried on that same
    posting; one with none, or whose newest was cancelled or belonged to an
    earlier round, gets a fresh ``post_now``. A unit being sent right now
    (``SENDING``) is passed over.

    A PO or SERVICE draft that is not committed yet (PLANNED or FAILED) is
    built by its handler from the freeze snapshot, while its vouchers can
    still be overridden, given new bill facts or changed in an accepted
    change. So before such a unit is sent, its snapshot is compared with the
    plan (:func:`stale_snapshot`); when they differ its company is frozen
    again -- exactly what Post does, no SAP call -- and the units are listed
    afresh. If the freeze is refused (the change made a voucher blocking) the
    unit is marked FAILED with the reason and the next unit is tried. A unit
    is frozen again at most once per call, and never sent stale.
    """
    if not flags.enabled():
        raise CashSapDisabled()
    check_drift(bunch)
    skip = set(int(i) for i in (skip_posting_ids or ()))
    refrozen = set()
    restart = True
    while restart:
        restart = False
        for unit in _units(bunch, include_failed):
            newest = _newest(unit.kind, unit.source_id)
            if newest is not None and (newest.pk in skip or newest.status == SapPostingStatus.SENDING):
                continue
            batch, changes = _unit_snapshot_changes(unit)
            if changes:
                key = (unit.kind, unit.source_id)
                if key in refrozen:
                    continue  # still differs after a freeze: never send it so
                refrozen.add(key)
                try:
                    freeze_bunch(user=user, bunch=bunch, sap_company_code=batch.sap_company_code)
                except BunchChanged:
                    raise
                except ValidationError as exc:
                    _fail_unit(unit, STALE_MESSAGE.format(changes="; ".join(changes))
                               + f" It cannot be frozen again: {error_text(exc)}")
                    continue
                restart = True  # the freeze may have rebuilt GRPOs: list the units afresh
                break
            _touch_sender(unit, user)
            try:
                if newest is not None and newest.status in (SapPostingStatus.QUEUED, SapPostingStatus.REJECTED):
                    posting, outcome = sap_postings.retry(newest.pk)
                else:
                    posting, outcome = sap_postings.post_now(
                        kind=unit.kind,
                        company=bunch.company,
                        source_id=unit.source_id,
                        title=unit.title,
                        link=link_for(bunch.pk),
                        user=user if unit.notify_person else None,
                    )
            except (sap_postings.PostingInProgress, ValueError):
                continue
            return {
                "sent": {
                    "kind": unit.kind,
                    "source_id": unit.source_id,
                    "label": unit.label,
                    "outcome": str(outcome.kind).lower(),
                    "message": outcome.message,
                    "sap_posting_id": posting.pk,
                },
                "remaining": _count_units(bunch, False),
                "plan": build_plan(bunch).as_dict(),
            }
    return {"sent": None, "remaining": 0, "plan": build_plan(bunch).as_dict()}


def post_bunch(*, user, bunch, sap_company_code=None) -> dict:
    """Freeze, queue every unit that can go now, and send one."""
    if not flags.enabled():
        raise CashSapDisabled()
    batches = freeze_bunch(user=user, bunch=bunch, sap_company_code=sap_company_code)
    for b in batches:  # outside any transaction: queue() is durable
        if b.po_status == SapDocStatus.PLANNED and _newest(KIND_PO, b.id) is None:
            sap_postings.queue(kind=KIND_PO, company=bunch.company, source_id=b.id,
                               title=title_for_po(b), link=link_for(bunch.pk), user=user)
            ImprestBatch.objects.filter(pk=b.pk, po_status=SapDocStatus.PLANNED).update(po_status=SapDocStatus.QUEUED)
        for d in b.ap_drafts.filter(kind=APDraftKind.SERVICE, status=SapDocStatus.PLANNED):
            if _newest(KIND_AP, d.id) is None:
                sap_postings.queue(kind=KIND_AP, company=bunch.company, source_id=d.id,
                                   title=title_for_ap(d), link=link_for(bunch.pk), user=user)
                ImprestAPDraft.objects.filter(pk=d.pk, status=SapDocStatus.PLANNED).update(status=SapDocStatus.QUEUED)
        if b.po_status == SapDocStatus.POSTED:
            queue_grpo_units(b)
    return send_next(user=user, bunch=bunch)


def queue_grpo_units(batch) -> int:
    """Queue each ready GRPO with no posting waiting or being sent. Never inside a transaction.

    * Newest posting QUEUED or SENDING: it is already on its way; left alone.
    * Newest CANCELLED: a person stopped it in the posting log (the plan shows
      "Stopped"); left alone -- Post / send-next sends it when asked.
    * Newest REJECTED: typically a GRPO refused only because its PO had been
      refused, which ``ImprestPOHandler._adopt`` has just set back to PLANNED,
      or one set back to PLANNED by Re-check. The refused posting is cancelled
      (so the log cannot send it a second time beside the new one) and a fresh
      one is queued for the worker. If it is being retried right now, the
      cancel is refused and the GRPO is left to that retry.
    * None, or POSTED (a "nothing to post" outcome before a rebuild): queued.
    """
    count = 0
    bunch = batch.bunch
    for gr in ImprestGRPO.objects.filter(
        batch=batch, status__in=(SapDocStatus.PLANNED, SapDocStatus.QUEUED)
    ).select_related("batch__bunch").order_by("id"):
        if not gr.vouchers.filter(left_out_after_po=False).exists():
            continue
        newest = _newest(KIND_GRPO, gr.id)
        if newest is not None and newest.status in (*ACTIVE_STATUSES, SapPostingStatus.CANCELLED):
            continue
        if newest is not None and newest.status == SapPostingStatus.REJECTED:
            try:
                sap_postings.cancel(newest.pk, None, "Queued again as a new posting once its PO was in SAP.")
            except ValueError:
                continue  # being retried right now
        sap_postings.queue(kind=KIND_GRPO, company=bunch.company, source_id=gr.id,
                           title=title_for_grpo(gr), link=link_for(bunch.pk), user=None)
        ImprestGRPO.objects.filter(pk=gr.pk).update(status=SapDocStatus.QUEUED)
        count += 1
    return count


def maybe_queue_item_ap_draft(batch) -> bool:
    """Queue the ITEM A/P draft once every GRPO is done and at least one is in SAP."""
    if not batch.ap_drafts_enabled:
        return False
    d = ImprestAPDraft.objects.filter(batch=batch, kind=APDraftKind.ITEM, status=SapDocStatus.PLANNED).first()
    if d is None:
        return False
    statuses = list(ImprestGRPO.objects.filter(batch=batch).values_list("status", flat=True))
    if not statuses or any(s not in DONE_STATUSES for s in statuses):
        return False
    if SapDocStatus.POSTED not in statuses:
        return False
    sap_postings.queue(kind=KIND_AP, company=batch.bunch.company, source_id=d.id,
                       title=title_for_ap(d), link=link_for(batch.bunch_id), user=None)
    ImprestAPDraft.objects.filter(pk=d.pk).update(status=SapDocStatus.QUEUED)
    return True


# ---------------------------------------------------------------------------
# Escape hatches
# ---------------------------------------------------------------------------


def stop_bunch(*, user, bunch, reason) -> dict:
    """Stop sending: cancel every waiting or refused posting and mark its row FAILED."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Say why sending is being stopped."})
    name = person_label(user)
    stopped, sending = 0, []
    for unit in _units(bunch, everything=True):
        newest = _newest(unit.kind, unit.source_id)
        if newest is None:
            continue
        if newest.status == SapPostingStatus.SENDING:
            sending.append(unit.label)
            continue
        if newest.status not in (SapPostingStatus.QUEUED, SapPostingStatus.REJECTED):
            continue
        try:
            sap_postings.cancel(newest.pk, user, reason)
        except ValueError:
            # The worker or another tab claimed it between the read and the cancel.
            sending.append(unit.label)
            continue
        _fail_unit(unit, f"Stopped by {name}: {reason}")
        stopped += 1
    out = {"stopped": stopped, "sending": sending, "plan": build_plan(bunch).as_dict()}
    if sending:
        out["notes"] = [f"{label} is being sent right now; it cannot be stopped." for label in sending]
    return out


def recheck_bunch(*, user, bunch) -> dict:
    """Read SAP back and correct JI: cancelled POs/GRPOs, deleted or HO-added A/P drafts.

    Reads only -- no SAP write -- so it runs in the request. HANA down raises
    ``SAPUnavailable``, which the project's exception handler returns as 503.
    """
    from sap_purchasing.client import PurchasingClient

    notes = []
    for batch in ImprestBatch.objects.filter(bunch_id=bunch.pk, simulated=False).order_by("id"):
        client = PurchasingClient(batch.sap_company_code)
        if batch.po_doc_entry is not None:
            state = client.document_state(OBJ_PO, batch.po_doc_entry)
            grpos_in_sap = ImprestGRPO.objects.filter(batch=batch).filter(
                Q(grpo_doc_entry__isnull=False) | Q(grpo_draft_entry__isnull=False)
            ).exists()
            cancelled = state is None or state.get("canceled") == "Y"
            closed_empty = state is not None and state.get("doc_status") == "C" and not grpos_in_sap
            if (cancelled or closed_empty) and grpos_in_sap:
                notes.append(f"PO {batch.po_doc_num} is cancelled in SAP but GRPOs exist — fix in SAP.")
            elif cancelled or closed_empty:
                with transaction.atomic():
                    ImprestBatch.objects.filter(pk=batch.pk).update(
                        po_doc_entry=None, po_doc_num=None, po_draft_entry=None, po_doc_total=None,
                        po_posted_at=None, po_payload={}, po_round=batch.po_round + 1,
                        po_status=SapDocStatus.PLANNED,
                        po_error="Cancelled/closed in SAP — will be raised again",
                    )
                    ImprestGRPO.objects.filter(batch=batch).exclude(status=SapDocStatus.NOT_NEEDED).update(
                        status=SapDocStatus.PLANNED, error="", payload={}, line_map={}
                    )
                notes.append(f"PO {batch.po_doc_num} was cancelled or closed in SAP; it will be raised again.")
        for gr in ImprestGRPO.objects.filter(batch=batch, status=SapDocStatus.POSTED, grpo_doc_entry__isnull=False):
            state = client.document_state(OBJ_GRPO, gr.grpo_doc_entry)
            if state is None or state.get("canceled") == "Y":
                ImprestGRPO.objects.filter(pk=gr.pk).update(
                    status=SapDocStatus.FAILED, error="Cancelled in SAP",
                    grpo_doc_entry=None, grpo_doc_num=None, grpo_draft_entry=None,
                    grpo_doc_total=None, posted_at=None, payload={}, line_map={},
                    round=gr.round + 1,
                )
                notes.append(f"GRPO {gr.grpo_doc_num} was cancelled in SAP; retry sends it again.")
        for d in ImprestAPDraft.objects.filter(batch=batch, status=SapDocStatus.POSTED, sap_draft_entry__isnull=False,
                                               sap_doc_entry__isnull=True):
            found = client.find_document_by_marker(OBJ_AP, d.vendor_code, d.marker)
            if found:
                ImprestAPDraft.objects.filter(pk=d.pk).update(
                    sap_doc_entry=found["doc_entry"], sap_doc_num=found["doc_num"])
                notes.append(f"A/P {d.kind.lower()} draft was added by head office as {found['doc_num']}.")
                continue
            if client.find_draft_by_marker(OBJ_AP, d.vendor_code, d.marker) is None:
                ImprestAPDraft.objects.filter(pk=d.pk).update(
                    status=SapDocStatus.FAILED, error="Draft deleted in SAP",
                    sap_draft_entry=None, round=d.round + 1,
                )
                notes.append(f"A/P {d.kind.lower()} draft {d.sap_draft_entry} was deleted in SAP.")
    out = build_plan(bunch).as_dict()
    out["notes"] = notes
    return out


def _recompute_grpo(gr, *, user=None):
    """After a member left: new amount, new NumAtCard, no stored payload. Inside a transaction."""
    members = list(gr.vouchers.filter(left_out_after_po=False))
    if not members:
        ImprestGRPO.objects.filter(pk=gr.pk).update(
            status=SapDocStatus.NOT_NEEDED, amount=ZERO, num_at_card="", payload={})
        return
    amount = sum((m.amount or ZERO for m in members), ZERO)
    ImprestGRPO.objects.filter(pk=gr.pk).update(amount=amount, num_at_card="", payload={})
    gr.refresh_from_db()
    allocate_grpo_num_at_card(gr)


def _leave_out(iv, *, user, reason_text):
    gr = iv.grpo
    iv.treatment = VoucherTreatment.SKIP
    iv.left_out_after_po = True
    iv.left_out_by = user
    iv.left_out_at = timezone.now()
    iv.left_out_reason = reason_text
    iv.skip_reason = reason_text[:255]
    iv.grpo = None
    iv.updated_by = user
    iv.save()
    if gr is not None:
        _recompute_grpo(gr, user=user)


def leave_out_after_po(*, user, bunch, entry, reason) -> dict:
    """Take one goods voucher out after its PO is in SAP; its PO line stays open."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Say why the voucher is being left out."})
    iv = ImprestVoucher.objects.filter(entry=entry, bunch=bunch).select_related("batch", "grpo").first()
    if iv is None or iv.batch is None or iv.batch.bunch_id != bunch.pk \
            or iv.treatment != VoucherTreatment.ITEM or iv.left_out_after_po:
        _refuse("Only a goods voucher on this bunch's PO can be left out after the PO.")
    batch, gr = iv.batch, iv.grpo
    if not _committed_po(batch):
        _refuse("The PO is not in SAP yet; change the voucher instead (Not posted).")

    def grpo_open(row) -> bool:
        return (row is not None
                and row.status in (SapDocStatus.PLANNED, SapDocStatus.QUEUED, SapDocStatus.FAILED)
                and row.grpo_doc_entry is None and row.grpo_draft_entry is None)

    if not grpo_open(gr):
        _refuse("This voucher's GRPO is already in SAP (or in approval); it cannot be left out.")
    # The handler builds its payload from the members it read when the send
    # began, and does not change the row's status first: a GRPO being sent
    # still reads PLANNED/QUEUED/FAILED. Leaving a member out now would let SAP
    # receive it while JI calls it left out.
    if _grpo_being_sent(gr.id):
        _refuse(BEING_SENT)

    others = gr.vouchers.filter(left_out_after_po=False).exclude(pk=iv.pk).exists()
    if not others:
        newest = _newest(KIND_GRPO, gr.id)
        if newest is not None and newest.status in (SapPostingStatus.QUEUED, SapPostingStatus.REJECTED):
            try:
                sap_postings.cancel(newest.pk, user, f"Voucher left out: {reason}")
            except ValueError:
                _refuse(BEING_SENT)  # claimed between the read and the cancel

    with transaction.atomic():
        iv = ImprestVoucher.objects.select_for_update(of=("self",)).get(pk=iv.pk)
        gr = ImprestGRPO.objects.select_for_update(of=("self",)).filter(pk=gr.pk).first()
        if not grpo_open(gr):
            _refuse("This voucher's GRPO is already in SAP (or in approval); it cannot be left out.")
        if _grpo_being_sent(gr.id, lock=True):
            _refuse(BEING_SENT)
        _leave_out(iv, user=user, reason_text=f"Left out after the PO by {person_label(user)}: {reason}")

    out = build_plan(bunch).as_dict()
    out["notes"] = [
        f"PO line {iv.po_line_num} of PO {batch.po_doc_num or '(simulated)'} stays open — use "
        f"'Close the PO in SAP' once the rest are in."
    ]
    return out


def redate_grpo(*, user, bunch, grpo, posting_date=None, gate_entry_no=None) -> ImprestGRPO:
    """Move a GRPO's posting date after a closed period, or fix its G.no.

    NumAtCard, TaxDate and Dim2 keep the bill's own date and month (D5).
    """
    today = timezone.localdate()
    with transaction.atomic():
        gr = ImprestGRPO.objects.select_for_update(of=("self",)).get(pk=grpo.pk)
        batch = ImprestBatch.objects.get(pk=gr.batch_id)
        if batch.bunch_id != bunch.pk:
            _refuse("That GRPO is not in this bunch.")
        if gr.status not in (SapDocStatus.PLANNED, SapDocStatus.FAILED) or gr.grpo_doc_entry is not None \
                or gr.grpo_draft_entry is not None:
            _refuse("Only a GRPO that is not in SAP can be re-dated.")
        if not _committed_po(batch):
            _refuse("The PO is not in SAP yet; change the voucher's bill date instead.")
        if posting_date is not None:
            if posting_date > today:
                raise ValidationError({"posting_date": "A GRPO cannot be dated in the future."})
            if batch.po_doc_date and posting_date < batch.po_doc_date:
                raise ValidationError({"posting_date": f"A GRPO cannot be dated before its PO ({batch.po_doc_date:%d-%m-%Y})."})
            if posting_date < gr.bill_date:
                raise ValidationError({"posting_date": "A GRPO cannot be dated before its bill."})
            gr.posting_date = posting_date
        if gate_entry_no is not None:
            gno = gate_entry_no.strip()
            if len(gno) > 60:
                raise ValidationError({"gate_entry_no": "At most 60 characters."})
            gr.gate_entry_no = gno
        gr.payload = {}
        if gr.status == SapDocStatus.FAILED:
            gr.status = SapDocStatus.PLANNED
            gr.error = ""
        gr.updated_by = user
        gr.save()
    return gr


def request_po_close(*, user, bunch, batch) -> dict:
    """Close the PO in SAP once its remaining GRPOs are in and something was left out."""
    if batch.bunch_id != bunch.pk:
        _refuse("That PO is not in this bunch.")
    with transaction.atomic():
        b = ImprestBatch.objects.select_for_update(of=("self",)).get(pk=batch.pk)
        if b.po_status != SapDocStatus.POSTED:
            _refuse("The PO is not in SAP.")
        if not ImprestVoucher.objects.filter(batch=b, left_out_after_po=True).exists():
            _refuse("Nothing was left out of this PO, so it closes by itself when received.")
        if ImprestGRPO.objects.filter(batch=b).exclude(status__in=DONE_STATUSES).exists():
            _refuse("Some GRPOs of this PO are not in SAP yet.")
        if b.po_close_status not in (SapDocStatus.NOT_NEEDED, SapDocStatus.FAILED):
            _refuse("Closing this PO is already under way.")
        ImprestBatch.objects.filter(pk=b.pk).update(po_close_status=SapDocStatus.PLANNED, po_close_error="")
    b.refresh_from_db()
    if _newest(KIND_PO_CLOSE, b.id) is None or _newest(KIND_PO_CLOSE, b.id).status not in (
        SapPostingStatus.QUEUED, SapPostingStatus.REJECTED, SapPostingStatus.SENDING
    ):
        sap_postings.queue(kind=KIND_PO_CLOSE, company=bunch.company, source_id=b.id,
                           title=title_for_close(b), link=link_for(bunch.pk), user=user)
    ImprestBatch.objects.filter(pk=b.pk, po_close_status=SapDocStatus.PLANNED).update(
        po_close_status=SapDocStatus.QUEUED)
    return send_next(user=user, bunch=bunch)


def _anything_in_sap(bunch) -> bool:
    return (
        ImprestBatch.objects.filter(bunch_id=bunch.pk).filter(
            Q(po_doc_entry__isnull=False) | Q(po_draft_entry__isnull=False)).exists()
        or ImprestGRPO.objects.filter(batch__bunch_id=bunch.pk).filter(
            Q(grpo_doc_entry__isnull=False) | Q(grpo_draft_entry__isnull=False)).exists()
        or ImprestAPDraft.objects.filter(batch__bunch_id=bunch.pk).filter(
            Q(sap_draft_entry__isnull=False) | Q(sap_doc_entry__isnull=False)).exists()
    )


def reset_bunch(*, user, bunch) -> None:
    """Throw the bunch's SAP plan away -- allowed only while nothing is in SAP.

    Order matters (spec A6): cancel the waiting postings first (outside any
    transaction), then null the vouchers' foreign keys, then delete GRPOs,
    drafts and batches, so nothing PROTECTed is deleted while referenced.
    Overrides, bills and the bill facts are kept.
    """
    refusal = ("Something from this bunch is already in SAP; it cannot be reset. "
               "Use Re-check SAP or Leave out.")
    if _anything_in_sap(bunch):
        _refuse(refusal)
    units = _units(bunch, everything=True)
    for unit in units:
        newest = _newest(unit.kind, unit.source_id)
        if newest is not None and newest.status == SapPostingStatus.SENDING:
            _refuse(f"{unit.label} is being sent to SAP right now; try again in a minute.")
    for unit in units:
        newest = _newest(unit.kind, unit.source_id)
        if newest is not None and newest.status in (SapPostingStatus.QUEUED, SapPostingStatus.REJECTED):
            try:
                sap_postings.cancel(newest.pk, user, "Reset from the bunch page")
            except ValueError:
                # Claimed by the worker or another tab between the read and the cancel.
                _refuse(f"{unit.label} is being sent to SAP right now; try again in a minute.")

    with transaction.atomic():
        CashBunch.objects.select_for_update().get(pk=bunch.pk)
        if _anything_in_sap(bunch):
            _refuse(refusal)
        ImprestVoucher.objects.filter(Q(bunch_id=bunch.pk) | Q(batch__bunch_id=bunch.pk)).update(
            batch=None, grpo=None, treatment="", item_code="", item_name="", gl_account_code="",
            gl_account_name="", account_code="", warehouse_code="", tax_code="", ocr_code="",
            ocr_code2="", ocr_code3="", ocr_code5="", bill_date=None, gate_entry_no="",
            supplier_bill_no="", amount=None, po_line_num=None, skip_reason="",
            left_out_after_po=False, left_out_by=None, left_out_at=None, left_out_reason="",
        )
        ImprestGRPO.objects.filter(batch__bunch_id=bunch.pk).delete()
        ImprestAPDraft.objects.filter(batch__bunch_id=bunch.pk).delete()
        ImprestBatch.objects.filter(bunch_id=bunch.pk).delete()
        BunchSapState.objects.filter(bunch_id=bunch.pk).update(
            status=BunchSapStatus.OPEN, frozen_at=None, frozen_by=None,
            member_snapshot={}, drift={}, checked_at=timezone.now(),
        )


def _row_is_here(iv, bunch) -> bool:
    """The row's documents are this bunch's: its batch's bunch, else (no batch) its own."""
    if iv.batch_id is not None:
        return ImprestBatch.objects.filter(pk=iv.batch_id, bunch_id=bunch.pk).exists()
    return iv.bunch_id == bunch.pk


def accept_bunch_changes(*, user, bunch, note) -> dict:
    """Accept what changed in the cash book since the freeze, when that is safe.

    * A voucher **taken out** of the bunch: if its own document is not
      committed it is simply dropped -- or, when its PO is already in SAP, it
      is left out after the PO (its PO line stays open; close the PO later).
      If its GRPO or service draft is in SAP the change is refused: cancel
      that document in SAP and Re-check first.
    * A voucher whose **amount, G/L, branch or date** changed: accepted only
      while its own document is not committed.
    * A voucher that **appeared**: accepted only while no PO of the bunch is
      committed.
    * A voucher that appeared or changed and would now land on a committed PO
      (``NOT_ON_PO``) or a committed SERVICE draft (``NOT_ON_AP_DRAFT``), or
      that is frozen under another bunch (``IN_OTHER_BUNCH``): refused.
    * A taken-out voucher whose GRPO is being sent right now: refused; try
      again in a minute.

    Taken-out vouchers are found by entry alone, not by ``bunch``: the row's
    ``bunch`` may already name the bunch the voucher moved to. A row is acted
    on only while its batch (or, with no batch, its ``bunch``) is this bunch's;
    a row that has moved on belongs to the other bunch.

    Then the snapshot is taken again. The next Post re-plans what is not
    committed yet; send-next freezes a stale uncommitted PO or SERVICE draft
    again before sending it.
    """
    note = (note or "").strip()
    if not note:
        raise ValidationError({"note": "Say why the change is being accepted."})
    state = BunchSapState.objects.filter(bunch_id=bunch.pk).first()
    if state is None or not state.is_frozen:
        _refuse("This bunch has not been frozen for SAP.")
    current = drift_mod.current_members(bunch)
    drift = drift_mod.compare(state.member_snapshot, current)
    if not drift:
        record_drift(bunch, {})
        _refuse("Nothing has changed since the bunch was frozen.")

    problems = []
    removed = [
        iv for iv in ImprestVoucher.objects.filter(entry_id__in=drift.get("removed", []))
        .select_related("batch", "grpo")
        if _row_is_here(iv, bunch)
    ]
    for iv in removed:
        if iv.grpo is not None and iv.grpo.committed:
            problems.append(f"voucher {iv.entry_id} is on GRPO {iv.grpo.grpo_doc_num or iv.grpo.id}, which is in SAP")
        elif iv.treatment == VoucherTreatment.SERVICE and is_frozen(iv):
            problems.append(f"voucher {iv.entry_id} is on an A/P draft that is in SAP")
        elif iv.grpo_id is not None and _grpo_being_sent(iv.grpo_id):
            problems.append(f"voucher {iv.entry_id}'s GRPO is being sent to SAP right now; try again in a minute")
    for key in drift.get("changed", {}):
        iv = ImprestVoucher.objects.filter(entry_id=int(key)).select_related("batch", "grpo").first()
        if iv is not None and is_frozen(iv):
            problems.append(f"voucher {key} changed after its document was committed")
    if drift.get("added") and ImprestBatch.objects.filter(bunch_id=bunch.pk).filter(
        Q(po_doc_entry__isnull=False) | Q(po_status__in=(SapDocStatus.QUEUED, SapDocStatus.IN_APPROVAL, SapDocStatus.POSTED))
    ).exists():
        problems.append(
            "voucher(s) " + ", ".join(str(i) for i in drift["added"]) + " joined after the PO was raised"
        )
    watched = set(drift.get("added", [])) | {int(k) for k in drift.get("changed", {})}
    if watched:
        plan = build_plan(bunch)
        for vp in [v for g in plan.groups for v in g.vouchers] + plan.unrouted:
            if vp.entry_id not in watched or vp.frozen:
                continue
            for p in vp.problems:
                if p.code in (NOT_ON_PO, NOT_ON_AP_DRAFT, IN_OTHER_BUNCH):
                    problems.append(f"voucher {vp.serial_number or vp.entry_id}: {p.message}")
    if problems:
        _refuse("The change cannot be accepted: " + "; ".join(problems) + ".", code=BUNCH_CHANGED)

    with transaction.atomic():
        CashBunch.objects.select_for_update().get(pk=bunch.pk)
        for iv in removed:
            iv = ImprestVoucher.objects.select_for_update(of=("self",)).get(pk=iv.pk)
            if not _row_is_here(iv, bunch):
                continue  # moved on to another bunch meanwhile
            if iv.grpo_id is not None and _grpo_being_sent(iv.grpo_id, lock=True):
                _refuse(f"The change cannot be accepted: voucher {iv.entry_id}'s GRPO is being sent to "
                        f"SAP right now; try again in a minute.", code=BUNCH_CHANGED)
            batch = ImprestBatch.objects.filter(pk=iv.batch_id).first()
            if iv.treatment == VoucherTreatment.ITEM and batch is not None and batch.po_committed:
                _leave_out(iv, user=user,
                           reason_text=f"Taken out of bunch {bunch.number} in the cash book; "
                                       f"accepted by {person_label(user)}: {note}")
            else:
                gr = iv.grpo
                ImprestVoucher.objects.filter(pk=iv.pk).update(
                    batch=None, grpo=None, po_line_num=None, treatment="", bunch=None)
                if gr is not None:
                    _recompute_grpo(gr, user=user)
        BunchSapState.objects.filter(pk=state.pk).update(
            member_snapshot=current, drift={}, status=BunchSapStatus.FROZEN,
            checked_at=timezone.now(), changes_accepted_by=user,
            changes_accepted_at=timezone.now(), changes_accepted_note=note,
        )
    return build_plan(bunch).as_dict()
