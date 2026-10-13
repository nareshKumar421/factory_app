"""The four kinds of cash book posting the SAP queue can send (and send again).

=================== =========================== ===================================
kind                handler                     one try
=================== =========================== ===================================
``cash_sap.po``       :class:`ImprestPOHandler`     the batch's imprest PO
``cash_sap.grpo``     :class:`ImprestGRPOHandler`   one voucher (group) GRPO
``cash_sap.ap_draft`` :class:`ImprestAPDraftHandler` one A/P invoice draft
``cash_sap.po_close`` :class:`ImprestPOCloseHandler` close the PO after a leave-out
=================== =========================== ===================================

Registered in ``CashSapConfig.ready()`` (``sap_postings/services.py`` is not
edited on this branch).

EVERY TRY, IN THIS ORDER (spec §2.3.9, D8, D9)
----------------------------------------------
1. Load the row. Gone -> rejected. Already POSTED / NOT_NEEDED -> posted.
2. ``CASH_SAP_ENABLED`` off -> the row waits (QUEUED); nothing is sent.
3. The bunch changed in the cash book since it was frozen -> rejected
   ``BUNCH_CHANGED``; nothing is sent.
4. JI-only preconditions (the GRPO waits for its PO; the A/P draft re-reads
   the live "A/P drafts" switch).
5. Simulated row -> build and store the payload, mark it POSTED (simulated),
   do the follow-ups. **No SAP call of any kind.**
6. Live guard (``flags.require_target``): a non-TEST_ company with
   ``CASH_SAP_ALLOW_LIVE`` off -> FAILED, rejected.
7. Read SAP back by the marker -- the posted table, then ODRF -- and adopt or
   settle what is there (approval drafts, D10). Only then write.

``SAPConnectionError`` / ``dbapi.Error`` -> the row waits (QUEUED) and the
worker tries again; ``ValueError`` / DRF ``ValidationError`` /
``SAPValidationError`` / ``SAPDataError`` -> FAILED, rejected; anything else
-> FAILED and re-raised (``sap_postings.attempt`` logs it). Status writes are
``.update()``s: no row lock is held across an SAP call.
"""

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from hdbcli import dbapi
from rest_framework.exceptions import ValidationError as DRFValidationError

from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_postings.services import Outcome
from sap_purchasing.client import PurchasingClient
from sap_purchasing.readback import (
    DRAFT_APPROVED,
    DRAFT_IN_APPROVAL,
    DRAFT_REJECTED,
    classify_draft,
    map_lines_by_base_line,
    map_lines_by_free_text,
    po_line_free_text,
)
from sap_purchasing.service_layer import is_pending_approval

from . import drift as drift_mod
from . import flags
from .constants import (
    KIND_AP,
    KIND_GRPO,
    KIND_PO,
    KIND_PO_CLOSE,
    MAX_DOT_SUFFIX,
    NUM_AT_CARD_MAX,
    OBJ_AP,
    OBJ_GRPO,
    OBJ_PO,
)
from .models import (
    DONE_STATUSES,
    APDraftKind,
    CashBranchSapRouting,
    ImprestAPDraft,
    ImprestBatch,
    ImprestGRPO,
    ImprestVoucher,
    PostingMode,
    SapDocStatus,
    VoucherTreatment,
)
from .payloads import (
    ap_num_at_card,
    build_ap_item_draft_payload,
    build_ap_service_draft_payload,
    build_grpo_payload,
    build_po_payload,
    grpo_num_at_card,
    person_label,
)
from .titles import link_for, title_for_ap, title_for_close, title_for_grpo, title_for_po  # noqa: F401

logger = logging.getLogger(__name__)

PO_REFUSED_MESSAGE = "The imprest PO was refused; fix and retry the PO first."
DISABLED_MESSAGE = (
    "Posting the cash book to SAP is switched off (CASH_SAP_ENABLED); this waits "
    "until it is on."
)
SIMULATED_MESSAGE = "Simulated: nothing was sent to SAP."


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _error_text(exc) -> str:
    from .services import error_text

    return error_text(exc)


def _po_vouchers(batch):
    """The PO's goods vouchers, in line order (left-out ones keep their line)."""
    return list(
        ImprestVoucher.objects.filter(batch=batch, treatment=VoucherTreatment.ITEM)
        .select_related("entry")
        .order_by("po_line_num", "id")
    )


def _grpo_members(grpo):
    return list(
        ImprestVoucher.objects.filter(grpo=grpo, left_out_after_po=False)
        .select_related("entry")
        .prefetch_related("entry__attachments", "bills")
        .order_by("po_line_num", "id")
    )


def _refuse_if_stale(handler, row, batch, treatment):
    """Refuse to send a PO / SERVICE draft built from a snapshot the plan no longer agrees with.

    Only for a document not committed yet (PLANNED or FAILED -- e.g. a Retry
    from the sap_postings log after a refusal): its vouchers could be
    overridden, given new bill facts, or changed by an accepted change since
    the freeze, and the payload is built from the snapshot. Send-next freezes
    such a unit again before sending it, so this refuses only a send that came
    another way. ``None`` when the snapshot is current.
    """
    from .services import STALE_MESSAGE, stale_snapshot

    changes = stale_snapshot(batch, treatment)
    if not changes:
        return None
    message = STALE_MESSAGE.format(changes="; ".join(changes))
    handler._fail(row, message)
    return Outcome.rejected(message, link=handler.link(row))


class _ImprestHandler:
    """The shared prelude and error handling of every cash book handler."""

    kind = ""
    model = None
    status_field = "status"
    error_field = "error"
    draft_field = ""
    noun = "Record"
    related = ()

    # -- row plumbing --------------------------------------------------------

    def load(self, source_id):
        return self.model.objects.select_related(*self.related).filter(pk=source_id).first()

    def batch_of(self, row) -> ImprestBatch:
        return row.batch

    def code_of(self, row) -> str:
        return row.sap_company_code

    def is_simulated(self, row) -> bool:
        return bool(row.simulated)

    def person(self, row) -> str:
        batch = self.batch_of(row)
        return person_label(getattr(row, "last_sent_by", None) or batch.last_sent_by or batch.requested_by)

    def _set(self, row, **fields):
        type(row).objects.filter(pk=row.pk).update(**fields)
        for name, value in fields.items():
            setattr(row, name, value)

    def _status(self, row):
        return getattr(row, self.status_field)

    def _fail(self, row, message):
        self._set(row, **{self.status_field: SapDocStatus.FAILED, self.error_field: (message or "")[:5000]})

    def _wait(self, row, message):
        self._set(row, **{self.status_field: SapDocStatus.QUEUED, self.error_field: (message or "")[:5000]})

    def link(self, row) -> str:
        return link_for(self.batch_of(row).bunch_id)

    # -- the try -------------------------------------------------------------

    def send(self, posting):
        row = self.load(posting.source_id)
        if row is None:
            return Outcome.rejected(f"{self.noun} {posting.source_id} no longer exists.")
        if self._status(row) in DONE_STATUSES:
            return self.already_done(row)
        if not flags.enabled():
            self._set(row, **{self.status_field: SapDocStatus.QUEUED})
            return Outcome.waiting(DISABLED_MESSAGE)

        bunch = self.batch_of(row).bunch
        changed = drift_mod.state_drift(bunch)
        if changed:
            from .services import record_drift

            record_drift(bunch, changed)
            message = drift_mod.describe(changed, bunch.number)
            self._fail(row, message)
            return Outcome.rejected(message, link=self.link(row))

        try:
            early = self.precheck(row)
            if early is not None:
                return early
            if self.is_simulated(row):
                return self.simulate(row)
            code = self.code_of(row)
            try:
                flags.require_target(code)
            except (ValueError, SAPValidationError) as exc:
                message = str(exc)
                self._fail(row, message)
                return Outcome.rejected(message, context={"error": exc})
            client = PurchasingClient(code)
            return self.body(row, client, self.person(row))
        except (SAPConnectionError, dbapi.Error) as exc:
            message = f"SAP did not answer: {exc}"
            self._wait(row, message)
            return Outcome.waiting(message, context={"error": exc})
        except (ValueError, DRFValidationError, SAPValidationError, SAPDataError) as exc:
            message = _error_text(exc)
            self._fail(row, message)
            return Outcome.rejected(message, context={"error": exc}, link=self.link(row))
        except Exception as exc:
            self._fail(row, f"Unexpected error: {exc}")
            raise

    # -- hooks -----------------------------------------------------------------

    def already_done(self, row):
        return Outcome.posted(f"{self.noun} is already done.", link=self.link(row))

    def precheck(self, row):
        return None

    def simulate(self, row):  # pragma: no cover - every handler overrides it
        raise NotImplementedError

    def body(self, row, client, by):  # pragma: no cover
        raise NotImplementedError

    # -- shared SAP steps --------------------------------------------------------

    def series(self, client, object_code, posting_date, bpl_id, hint):
        if not flags.send_series():
            return None
        return client.resolve_series_for_branch(object_code, posting_date, bpl_id, hint)["series"]

    def settle_draft(self, row, client, object_code, vendor, marker, found, adopt):
        """An ODRF row carries our marker (spec D10)."""
        n = _int(found.get("draft_entry"))
        outcome = found.get("outcome") or classify_draft(found.get("wdd_status"))
        held = {self.status_field: SapDocStatus.IN_APPROVAL, self.draft_field: n, self.error_field: ""}
        if outcome == DRAFT_IN_APPROVAL:
            self._set(row, **held)
            return Outcome.waiting(f"Waiting for approval in SAP (draft {n}).")
        if outcome == DRAFT_APPROVED:
            client.save_purchasing_draft_to_document(object_code, n)
            doc = client.find_document_by_marker(object_code, vendor, marker) or \
                client.find_document_by_draft_key(object_code, n)
            if doc:
                return adopt(doc, " (approved in SAP)")
            self._set(row, **held)
            return Outcome.waiting("Approved draft is being added in SAP")
        if outcome == DRAFT_REJECTED:
            message = f"Rejected in SAP approval (draft {n}). Change it in SAP or leave it out."
            self._set(row, **{self.status_field: SapDocStatus.FAILED, self.draft_field: n,
                              self.error_field: message})
            return Outcome.rejected(message, link=self.link(row))
        self._set(row, **held)
        return Outcome.waiting(f"SAP holds this as draft {n}; add it in SAP or delete it.")

    def ensure_num_at_card(self, row, client, object_code, base, ji_taken):
        """Keep the row's NumAtCard if SAP does not have it; else the next free ``.`` suffix."""
        vendor = row.vendor_code
        current = row.num_at_card or ""
        if current and current.rstrip(".") == base and not client.num_at_card_taken(object_code, vendor, current):
            return current
        for dots in range(MAX_DOT_SUFFIX + 1):
            candidate = base + "." * dots
            if len(candidate) > NUM_AT_CARD_MAX:
                break
            if candidate != current and ji_taken(candidate):
                continue
            if client.num_at_card_taken(object_code, vendor, candidate):
                continue
            try:
                with transaction.atomic():
                    type(row).objects.select_for_update(of=("self",)).filter(pk=row.pk).first()
                    if candidate != current and ji_taken(candidate):
                        continue
                    type(row).objects.filter(pk=row.pk).update(num_at_card=candidate)
            except IntegrityError:
                continue
            row.num_at_card = candidate
            return candidate
        raise ValueError(
            f"The vendor reference {base} is taken {MAX_DOT_SUFFIX + 1} times in SAP or here. "
            f"Re-date or change the voucher, or key it in SAP by hand."
        )


# ---------------------------------------------------------------------------
# The PO
# ---------------------------------------------------------------------------


class ImprestPOHandler(_ImprestHandler):
    kind = KIND_PO
    model = ImprestBatch
    status_field = "po_status"
    error_field = "po_error"
    draft_field = "po_draft_entry"
    noun = "Imprest batch"
    related = ("bunch", "bunch__company", "requested_by", "last_sent_by")

    def batch_of(self, row):
        return row

    def link(self, row):
        return link_for(row.bunch_id)

    def already_done(self, row):
        nums = [str(row.po_doc_num)] if row.po_doc_num else []
        return Outcome.posted(
            f"SAP PO {row.po_doc_num or '(none needed)'} (already done; not posted twice)",
            result={"doc_nums": nums, "batch_id": row.id}, link=self.link(row),
        )

    def precheck(self, row):
        if row.mode != PostingMode.PO_GRPO or not _po_vouchers(row):
            self._set(row, po_status=SapDocStatus.NOT_NEEDED, po_error="")
            return Outcome.posted("No PO is needed for this batch.", link=self.link(row))
        if not row.po_committed and row.po_status in (SapDocStatus.PLANNED, SapDocStatus.FAILED):
            return _refuse_if_stale(self, row, row, VoucherTreatment.ITEM)
        return None

    def simulate(self, row):
        from .services import queue_grpo_units

        payload = build_po_payload(row, _po_vouchers(row), series=None, by=self.person(row),
                                   upper=flags.upper_comments())
        self._set(row, po_payload=payload, po_status=SapDocStatus.POSTED,
                  po_posted_at=timezone.now(), po_error="")
        queue_grpo_units(row)
        return Outcome.posted(SIMULATED_MESSAGE, result={"doc_nums": [], "batch_id": row.id, "simulated": True},
                              link=self.link(row))

    def body(self, row, client, by):
        found = client.find_document_by_marker(OBJ_PO, row.vendor_code, row.marker)
        if found:
            return self._adopt(row, client, found, " (already in SAP; not posted twice)")
        draft = client.find_draft_by_marker(OBJ_PO, row.vendor_code, row.marker)
        if draft:
            return self.settle_draft(row, client, OBJ_PO, row.vendor_code, row.marker, draft,
                                     lambda doc, note: self._adopt(row, client, doc, note))

        series = self.series(client, OBJ_PO, row.po_doc_date, row.bpl_id, row.po_series_hint)
        payload = build_po_payload(row, _po_vouchers(row), series=series, by=by, upper=flags.upper_comments())
        self._set(row, po_payload=payload)
        res = client.create_purchase_order_document(payload)
        if is_pending_approval(res):
            n = _int(res.get("draft_entry"))
            self._set(row, po_status=SapDocStatus.IN_APPROVAL, po_draft_entry=n, po_error="")
            return Outcome.waiting(f"Waiting for approval in SAP (draft {n}).")
        doc = {"doc_entry": res.get("DocEntry"), "doc_num": res.get("DocNum"), "doc_total": res.get("DocTotal")}
        return self._adopt(row, client, doc, "")

    def _adopt(self, row, client, doc, note):
        from .services import queue_grpo_units

        self._set(
            row,
            po_doc_entry=_int(doc.get("doc_entry")),
            po_doc_num=_int(doc.get("doc_num")),
            po_doc_total=doc.get("doc_total"),
            po_status=SapDocStatus.POSTED,
            po_posted_at=timezone.now(),
            po_error="",
        )
        self._map_lines(row, client)
        # GRPOs refused only because this PO had been refused go again by themselves:
        # queue_grpo_units cancels their refused posting and queues a fresh one.
        ImprestGRPO.objects.filter(batch=row, status=SapDocStatus.FAILED, error=PO_REFUSED_MESSAGE).update(
            status=SapDocStatus.PLANNED, error="")
        queue_grpo_units(row)
        return Outcome.posted(
            f"SAP PO {row.po_doc_num}{note}",
            result={"doc_nums": [str(row.po_doc_num)], "batch_id": row.id},
            link=self.link(row),
        )

    def _map_lines(self, row, client):
        """Each voucher's PO LineNum from the line's FreeText; fail-soft (payload order)."""
        vouchers = _po_vouchers(row)
        try:
            lines = client.document_lines(OBJ_PO, row.po_doc_entry)
        except (SAPConnectionError, SAPDataError, dbapi.Error):
            logger.warning("PO %s lines could not be read back; keeping payload order", row.po_doc_entry)
            return
        mapping = map_lines_by_free_text(lines, {v.entry_id: po_line_free_text(v.entry_id) for v in vouchers})
        lost = set()
        for v in vouchers:
            line_num = mapping.get(v.entry_id)
            if line_num is None:
                if v.grpo_id:
                    lost.add(v.grpo_id)
            elif line_num != v.po_line_num:
                ImprestVoucher.objects.filter(pk=v.pk).update(po_line_num=line_num)
        if lost:
            ImprestGRPO.objects.filter(pk__in=lost).exclude(status__in=DONE_STATUSES).update(
                status=SapDocStatus.FAILED,
                error=f"PO line not found on SAP PO {row.po_doc_num}; check the PO in SAP, then Retry.",
            )


# ---------------------------------------------------------------------------
# The GRPO
# ---------------------------------------------------------------------------


def _attachment_files(members):
    """``[(key, file field, name)]``: bills first, approval proof only without a bill, then posting bills."""
    files = []
    for v in members:
        attachments = list(v.entry.attachments.all())
        bills = [a for a in attachments if not a.is_approval_proof]
        proofs = [a for a in attachments if a.is_approval_proof]
        for a in bills or proofs:
            files.append((f"entry_att:{a.pk}", a.file, a.original_filename))
        for b in v.bills.all():
            files.append((b.key, b.file, b.original_filename))
    return files


class ImprestGRPOHandler(_ImprestHandler):
    kind = KIND_GRPO
    model = ImprestGRPO
    draft_field = "grpo_draft_entry"
    noun = "Imprest GRPO"
    related = ("batch", "batch__bunch", "batch__bunch__company", "batch__requested_by",
               "batch__last_sent_by", "last_sent_by")

    def _batch(self, row):
        row.batch = ImprestBatch.objects.select_related("bunch", "bunch__company", "requested_by",
                                                        "last_sent_by").get(pk=row.batch_id)
        return row.batch

    def already_done(self, row):
        nums = [str(row.grpo_doc_num)] if row.grpo_doc_num else []
        return Outcome.posted(f"SAP GRPO {row.grpo_doc_num or '(none needed)'} (already done)",
                              result={"doc_nums": nums, "grpo_id": row.id}, link=self.link(row))

    def precheck(self, row):
        batch = self._batch(row)
        if batch.po_status == SapDocStatus.FAILED:
            message = PO_REFUSED_MESSAGE
            self._fail(row, message)
            return Outcome.rejected(message, link=self.link(row))
        if batch.po_status != SapDocStatus.POSTED:
            self._wait(row, "Waiting for the imprest PO to reach SAP.")
            return Outcome.waiting("Waiting for the imprest PO to reach SAP.")
        if not _grpo_members(row):
            self._set(row, status=SapDocStatus.NOT_NEEDED, error="")
            return Outcome.posted("Every voucher of this GRPO was left out; nothing to post.",
                                  link=self.link(row))
        return None

    def simulate(self, row):
        batch = row.batch
        members = _grpo_members(row)
        payload = build_grpo_payload(row, members, batch, series=None, attachment_entry=None,
                                     by=self.person(row), upper=flags.upper_comments())
        self._set(row, payload=payload, status=SapDocStatus.POSTED, posted_at=timezone.now(), error="",
                  line_map={str(v.id): i for i, v in enumerate(members)})
        self._follow_ups(row, batch)
        return Outcome.posted(SIMULATED_MESSAGE, result={"doc_nums": [], "grpo_id": row.id, "simulated": True},
                              link=self.link(row))

    def body(self, row, client, by):
        batch = row.batch
        vendor = row.vendor_code or batch.vendor_code
        members = _grpo_members(row)
        found = client.find_document_by_marker(OBJ_GRPO, vendor, row.marker)
        if found:
            return self._adopt(row, client, batch, members, found, " (already in SAP; not posted twice)")
        draft = client.find_draft_by_marker(OBJ_GRPO, vendor, row.marker)
        if draft:
            return self.settle_draft(row, client, OBJ_GRPO, vendor, row.marker, draft,
                                     lambda doc, note: self._adopt(row, client, batch, members, doc, note))

        series = self.series(client, OBJ_GRPO, row.posting_date, batch.bpl_id, batch.grpo_series_hint)

        from .services import _grpo_taken

        self.ensure_num_at_card(row, client, OBJ_GRPO, grpo_num_at_card(row.amount, row.bill_date),
                                _grpo_taken(row))
        attachment_entry = self._upload(row, client, members, batch)
        payload = build_grpo_payload(row, members, batch, series=series, attachment_entry=attachment_entry,
                                     by=by, upper=flags.upper_comments())
        self._set(row, payload=payload)
        res = client.create_purchase_delivery_note_document(payload)
        if is_pending_approval(res):
            n = _int(res.get("draft_entry"))
            self._set(row, status=SapDocStatus.IN_APPROVAL, grpo_draft_entry=n, error="")
            return Outcome.waiting(f"Waiting for approval in SAP (draft {n}).")
        doc = {"doc_entry": res.get("DocEntry"), "doc_num": res.get("DocNum"), "doc_total": res.get("DocTotal")}
        return self._adopt(row, client, batch, members, doc, "")

    def _upload(self, row, client, members, batch):
        """Upload the bills not yet in SAP. The attachment entry is saved after the first file."""
        uploaded = list(row.uploaded_files or [])
        absolute_entry = row.sap_attachment_entry
        for key, field_file, name in _attachment_files(members):
            if key in uploaded:
                continue
            path = field_file.path
            if absolute_entry is None:
                res = client.upload_attachment(path, name, allow_metadata_fallback=True)
                absolute_entry = _int(res.get("AbsoluteEntry"))
                if absolute_entry is None:
                    raise SAPDataError("SAP answered the attachment upload without an AbsoluteEntry.")
                uploaded.append(key)
                self._set(row, sap_attachment_entry=absolute_entry, uploaded_files=list(uploaded))
            else:
                client.add_line_to_existing_attachment(absolute_entry, path, name, allow_metadata_fallback=True)
                uploaded.append(key)
                self._set(row, uploaded_files=list(uploaded))
        if absolute_entry is None and batch.grpo_needs_attachment:
            raise ValueError("No bill photo — SAP refuses a GRPO without an attachment. Add one or leave "
                             "the voucher out.")
        return absolute_entry

    def _adopt(self, row, client, batch, members, doc, note):
        doc_entry = _int(doc.get("doc_entry"))
        self._set(row, grpo_doc_entry=doc_entry, grpo_doc_num=_int(doc.get("doc_num")),
                  grpo_doc_total=doc.get("doc_total"), status=SapDocStatus.POSTED,
                  posted_at=timezone.now(), error="")
        mapping = {}
        try:
            lines = client.document_lines(OBJ_GRPO, doc_entry)
            mapping = map_lines_by_base_line(lines, {v.id: v.po_line_num for v in members},
                                             base_entry=batch.po_doc_entry)
        except (SAPConnectionError, SAPDataError, dbapi.Error):
            logger.warning("GRPO %s lines could not be read back; keeping payload order", doc_entry)
        line_map = {str(v.id): mapping.get(v.id, i) for i, v in enumerate(members)}
        self._set(row, line_map=line_map)
        self._follow_ups(row, batch)
        return Outcome.posted(f"SAP GRPO {row.grpo_doc_num}{note}",
                              result={"doc_nums": [str(row.grpo_doc_num)], "grpo_id": row.id},
                              link=self.link(row))

    def _follow_ups(self, row, batch):
        from .services import maybe_queue_item_ap_draft

        maybe_queue_item_ap_draft(batch)
        if ImprestGRPO.objects.filter(batch=batch).exclude(status__in=DONE_STATUSES).exists():
            return
        claimed = ImprestBatch.objects.filter(pk=batch.pk, completion_notified_at__isnull=True).update(
            completion_notified_at=timezone.now())
        if not claimed:
            return
        person = batch.last_sent_by or batch.requested_by
        if person is None:
            return
        try:
            from notifications.models import NotificationType
            from notifications.services import NotificationService

            NotificationService.send_notification_to_user(
                user=person,
                title=f"Bunch {batch.bunch.number}: every GRPO is in SAP"[:255],
                body=f"{batch.sap_company_code}: PO {batch.po_doc_num or '(simulated)'} and its GRPOs are posted.",
                notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
                click_action_url=link_for(batch.bunch_id),
                reference_type="cash_sap_batch",
                reference_id=batch.id,
                company=batch.bunch.company,
            )
        except Exception:  # noqa: BLE001 -- a notification never fails a posting
            logger.exception("Could not notify about imprest batch %s", batch.pk)


# ---------------------------------------------------------------------------
# The A/P draft
# ---------------------------------------------------------------------------


class ImprestAPDraftHandler(_ImprestHandler):
    kind = KIND_AP
    model = ImprestAPDraft
    draft_field = "sap_draft_entry"
    noun = "Imprest A/P draft"
    related = ("batch", "batch__bunch", "batch__bunch__company", "batch__requested_by",
               "batch__last_sent_by", "last_sent_by")

    def already_done(self, row):
        return Outcome.posted(f"A/P draft {row.sap_draft_entry or '(none needed)'} (already done)",
                              result={"draft_entry": row.sap_draft_entry, "ap_draft_id": row.id},
                              link=self.link(row))

    def _grpos(self, row):
        return list(ImprestGRPO.objects.filter(batch_id=row.batch_id).order_by("id"))

    def precheck(self, row):
        batch = row.batch
        on = CashBranchSapRouting.objects.filter(
            branch__company_id=batch.bunch.company_id,
            enabled=True,
            sap_company_code=row.sap_company_code,
            ap_drafts_enabled=True,
        ).exists()
        if not on:
            message = (f"A/P drafts are switched off for {row.sap_company_code} in Cash → SAP settings; "
                       f"nothing was sent.")
            self._fail(row, message)
            return Outcome.rejected(message, link=self.link(row))
        if row.kind == APDraftKind.ITEM:
            grpos = self._grpos(row)
            if any(g.status not in DONE_STATUSES for g in grpos):
                self._wait(row, "Waiting for the voucher GRPOs.")
                return Outcome.waiting("Waiting for the voucher GRPOs.")
            posted = [g for g in grpos if g.status == SapDocStatus.POSTED]
            if not posted:
                self._set(row, status=SapDocStatus.NOT_NEEDED, error="")
                return Outcome.posted("No GRPO reached SAP, so no A/P draft is needed.", link=self.link(row))
            amount = sum((g.amount for g in posted), Decimal("0.00"))
            doc_date = max(g.posting_date for g in posted)
            fields = {}
            if amount != row.amount:
                fields.update(amount=amount, num_at_card="")
            if doc_date != row.doc_date:
                fields["doc_date"] = doc_date
            if fields:
                self._set(row, **fields)
        elif not row.committed and row.status in (SapDocStatus.PLANNED, SapDocStatus.FAILED):
            return _refuse_if_stale(self, row, batch, VoucherTreatment.SERVICE)
        return None

    def _payload(self, row, *, series, by):
        upper = flags.upper_comments()
        if row.kind == APDraftKind.ITEM:
            grpos = [g for g in self._grpos(row) if g.status == SapDocStatus.POSTED]
            counts = {
                g.id: ImprestVoucher.objects.filter(grpo=g, left_out_after_po=False).count() or 1
                for g in grpos
            }
            return build_ap_item_draft_payload(row, grpos, series=series, by=by, upper=upper,
                                               member_counts=counts)
        vouchers = list(
            ImprestVoucher.objects.filter(batch_id=row.batch_id, treatment=VoucherTreatment.SERVICE)
            .select_related("entry").order_by("entry__serial_number", "entry_id")
        )
        return build_ap_service_draft_payload(row, vouchers, series=series, by=by, upper=upper)

    def _base(self, row):
        from .services import bunch_total

        batch = row.batch
        return ap_num_at_card(batch.expense_month, bunch_total(batch.bunch), row.amount)

    def simulate(self, row):
        if not row.num_at_card:
            from .services import _allocate, _ap_taken

            _allocate(row, self._base(row), _ap_taken(row))
        payload = self._payload(row, series=None, by=self.person(row))
        self._set(row, payload=payload, status=SapDocStatus.POSTED, posted_at=timezone.now(), error="")
        return Outcome.posted(SIMULATED_MESSAGE, result={"draft_entry": None, "ap_draft_id": row.id,
                                                         "simulated": True}, link=self.link(row))

    def body(self, row, client, by):
        from .services import _ap_taken

        batch = row.batch
        vendor = row.vendor_code
        draft = client.find_draft_by_marker(OBJ_AP, vendor, row.marker)
        if draft:
            n = _int(draft.get("draft_entry"))
            self._set(row, sap_draft_entry=n, status=SapDocStatus.POSTED, posted_at=timezone.now(), error="")
            return Outcome.posted(f"A/P draft {n} (already in SAP; not posted twice)",
                                  result={"draft_entry": n, "ap_draft_id": row.id}, link=self.link(row))
        doc = client.find_document_by_marker(OBJ_AP, vendor, row.marker)
        if doc:
            self._set(row, sap_doc_entry=_int(doc.get("doc_entry")), sap_doc_num=_int(doc.get("doc_num")),
                      status=SapDocStatus.POSTED, posted_at=timezone.now(), error="")
            return Outcome.posted("A/P already added by head office",
                                  result={"doc_nums": [str(row.sap_doc_num)], "ap_draft_id": row.id},
                                  link=self.link(row))

        series = None
        if batch.bpl_id is not None:
            series = self.series(client, OBJ_AP, row.doc_date, batch.bpl_id, batch.ap_series_hint)
        self.ensure_num_at_card(row, client, OBJ_AP, self._base(row), _ap_taken(row))
        payload = self._payload(row, series=series, by=by)
        self._set(row, payload=payload)
        res = client.create_purchase_invoice_draft(payload)
        n = _int(res.get("DocEntry"))
        self._set(row, sap_draft_entry=n, status=SapDocStatus.POSTED, posted_at=timezone.now(), error="")
        return Outcome.posted(f"A/P draft {n} — head office completes it in JSAP",
                              result={"draft_entry": n, "ap_draft_id": row.id}, link=self.link(row))


# ---------------------------------------------------------------------------
# Closing the PO
# ---------------------------------------------------------------------------


class ImprestPOCloseHandler(_ImprestHandler):
    kind = KIND_PO_CLOSE
    model = ImprestBatch
    status_field = "po_close_status"
    error_field = "po_close_error"
    draft_field = "po_draft_entry"
    noun = "Imprest batch"
    related = ("bunch", "bunch__company", "requested_by", "last_sent_by")

    def batch_of(self, row):
        return row

    def already_done(self, row):
        return Outcome.posted(f"PO {row.po_doc_num} is already closed.", link=self.link(row))

    def simulate(self, row):
        self._set(row, po_close_status=SapDocStatus.POSTED, po_close_error="")
        return Outcome.posted(SIMULATED_MESSAGE, result={"batch_id": row.id, "simulated": True},
                              link=self.link(row))

    def body(self, row, client, by):
        if row.po_doc_entry is None:
            raise ValueError("The PO has no SAP document to close.")
        state = client.document_state(OBJ_PO, row.po_doc_entry)
        if state is not None and state.get("doc_status") == "C":
            self._set(row, po_close_status=SapDocStatus.POSTED, po_close_error="")
            return Outcome.posted(f"PO {row.po_doc_num} was already closed in SAP.",
                                  result={"doc_nums": [str(row.po_doc_num)], "batch_id": row.id},
                                  link=self.link(row))
        client.close_purchase_order(row.po_doc_entry)
        self._set(row, po_close_status=SapDocStatus.POSTED, po_close_error="")
        return Outcome.posted(f"PO {row.po_doc_num} closed in SAP.",
                              result={"doc_nums": [str(row.po_doc_num)], "batch_id": row.id},
                              link=self.link(row))


HANDLERS = {
    KIND_PO: "cash_sap.sap_posting.ImprestPOHandler",
    KIND_GRPO: "cash_sap.sap_posting.ImprestGRPOHandler",
    KIND_AP: "cash_sap.sap_posting.ImprestAPDraftHandler",
    KIND_PO_CLOSE: "cash_sap.sap_posting.ImprestPOCloseHandler",
}
KIND_LABELS = {
    KIND_PO: "Cash book imprest PO",
    KIND_GRPO: "Cash book voucher GRPO",
    KIND_AP: "Cash book A/P draft",
    KIND_PO_CLOSE: "Cash book PO close",
}
KIND_SHORT = {
    KIND_PO: "imprest POs",
    KIND_GRPO: "voucher GRPOs",
    KIND_AP: "cash A/P drafts",
    KIND_PO_CLOSE: "imprest PO closes",
}
