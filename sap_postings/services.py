"""Sending SAP postings: now, and again when SAP is back.

Callers use :func:`post_now`. It records the posting (committed on its own),
tries it, and says what happened. A try that finds SAP not answering leaves the
posting QUEUED; :func:`run_due` -- the worker's loop body -- sends it again once
SAP answers. Every try goes through :func:`attempt`, which writes the log.

A handler (``HANDLERS``) knows one kind of posting. ``send(posting)`` makes one
try and returns an :class:`Outcome`. It has to be safe to call again after a try
whose result never came back -- in practice, read SAP back by the posting's
reference before writing -- because a worker that dies mid-send, or a timeout
after SAP committed, leaves exactly that behind.

Every write here is ``atomic(durable=True)``: the log must commit on its own,
not ride along in the caller's transaction and roll back when SAP fails. Called
inside another transaction it raises rather than quietly losing that guarantee.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.module_loading import import_string

from .models import (
    ACTIVE_STATUSES,
    SapPosting,
    SapPostingAttempt,
    SapPostingOutcome,
    SapPostingStatus,
)

logger = logging.getLogger(__name__)

#: kind -> handler class. A handler is imported only when its kind is sent.
HANDLERS = {
    "goods_return.receive": "goods_return.sap_posting.ReceiveHandler",
    "grpo.material": "grpo.sap_posting.MaterialGRPOHandler",
    "short_dispatch.post": "short_dispatch.sap_posting.ShortDispatchHandler",
    "bill_summary.stamp": "dispatch_plans.sap_posting.BillSummaryStampHandler",
    "dispatch_tracking.receive": "gate_core.services.dispatch_tracking_sap.SapReceiveHandler",
    "production_order.create": "production_orders.sap_posting.PlanHandler",
    "production_order.release": "production_orders.sap_posting.ReleaseHandler",
    "production_order.issue": "production_orders.sap_posting.IssueHandler",
    "production_order.receipt": "production_orders.sap_posting.ReceiptHandler",
    "production_order.close": "production_orders.sap_posting.CloseHandler",
    "production_order.replan": "production_orders.sap_posting.ReplanHandler",
    "production_order.unrelease": "production_orders.sap_posting.UnreleaseHandler",
}

#: kind -> what a person calls that kind of posting, for the log's filter.
KIND_LABELS = {
    "goods_return.receive": "Goods return (A/R Return)",
    "grpo.material": "Material GRPO",
    "short_dispatch.post": "Short dispatch (A/R Return)",
    "bill_summary.stamp": "Bill summary (invoice dispatch stamp)",
    "dispatch_tracking.receive": "Delivery (invoice received stamp)",
    "production_order.create": "Production order (plan)",
    "production_order.release": "Production order release",
    "production_order.issue": "Issue for production",
    "production_order.receipt": "Receipt from production",
    "production_order.close": "Production order close",
    "production_order.replan": "Production order change (planned)",
    "production_order.unrelease": "Production order back to planned",
}

#: kind -> what the "SAP is down" banner calls it, in a list of what waits.
KIND_SHORT = {
    "grpo.material": "GRPOs",
    "goods_return.receive": "goods returns",
    "short_dispatch.post": "short dispatches",
    "bill_summary.stamp": "bill summary stamps",
    "dispatch_tracking.receive": "delivery stamps",
    # One phrase for the five steps of a production order.
    "production_order.create": "production orders",
    "production_order.release": "production orders",
    "production_order.issue": "production orders",
    "production_order.receipt": "production orders",
    "production_order.close": "production orders",
    "production_order.replan": "production orders",
    "production_order.unrelease": "production orders",
}


def waiting_kinds():
    """What waits for SAP and posts by itself, as the banner lists it (each once)."""
    return list(dict.fromkeys(KIND_SHORT.get(kind, kind_label(kind)) for kind in HANDLERS))


def kind_label(kind):
    return KIND_LABELS.get(kind, kind)


#: A posting still SENDING after this lost its process mid-send.
STUCK_AFTER = timedelta(minutes=15)
#: The wait between tries while SAP is down: 30s, 1m, 2m, 4m, then every 5m.
BACKOFF_START_SECONDS = 30
BACKOFF_CAP_SECONDS = 300


@dataclass
class Outcome:
    """What one try came to. ``kind`` is a :class:`SapPostingOutcome`."""

    kind: str
    message: str = ""
    #: Kept on the posting once POSTED: the SAP document numbers.
    result: dict = field(default_factory=dict)
    #: Kept on the attempt: per document, the reference, payload and answer.
    detail: dict = field(default_factory=dict)
    #: For whoever made this try just now (a view's response); never stored.
    context: dict = field(default_factory=dict)
    #: Where the record now lives, when posting moved it (a GRPO draft becomes
    #: a new posted row); replaces the posting's link.
    link: str = ""

    @classmethod
    def posted(cls, message="", **kw):
        return cls(SapPostingOutcome.POSTED, message, **kw)

    @classmethod
    def waiting(cls, message="", **kw):
        return cls(SapPostingOutcome.WAITING, message, **kw)

    @classmethod
    def rejected(cls, message="", **kw):
        return cls(SapPostingOutcome.REJECTED, message, **kw)


class PostingInProgress(Exception):
    """The record is being sent right now -- by the worker, or another tab."""


def handler_for(kind):
    return import_string(HANDLERS[kind])()


# ---------------------------------------------------------------------------
# a person's try
# ---------------------------------------------------------------------------

def post_now(*, kind, company, source_id, title, link="", params=None, user=None):
    """Record the posting and try it now. Returns ``(posting, outcome)``.

    A record already waiting for SAP is tried again rather than queued twice,
    with the choices made this time. Raises :class:`PostingInProgress` if it is
    being sent at this moment.
    """
    posting = _open(
        kind=kind,
        company=company,
        source_id=source_id,
        title=title,
        link=link,
        params=params or {},
        user=user,
    )
    return posting, attempt(posting, by_worker=False)


def queue(*, kind, company, source_id, title, link="", params=None, user=None, reason=""):
    """Record the posting as waiting for SAP, without trying it now.

    For a caller that already knows SAP is not answering -- an earlier try in
    the same request got nothing -- and should not make the person wait out the
    same timeout again. The worker sends it as for any wait. A posting already
    being sent is left alone.
    """
    with transaction.atomic(durable=True):
        live = (
            SapPosting.objects.select_for_update()
            .filter(kind=kind, source_id=source_id, status__in=ACTIVE_STATUSES)
            .first()
        )
        if live is None:
            return SapPosting.objects.create(
                company=company,
                kind=kind,
                source_id=source_id,
                title=title[:255],
                link=link[:255],
                params=params or {},
                status=SapPostingStatus.QUEUED,
                last_error=reason[:5000],
                next_attempt_at=timezone.now() + _backoff(1),
                created_by=user,
            )
        if live.status == SapPostingStatus.QUEUED:
            live.params = params or {}
            live.title = title[:255]
            live.link = link[:255]
            live.save(update_fields=["params", "title", "link", "updated_at"])
        return live


def _open(*, kind, company, source_id, title, link, params, user):
    with transaction.atomic(durable=True):
        live = (
            SapPosting.objects.select_for_update()
            .filter(kind=kind, source_id=source_id, status__in=ACTIVE_STATUSES)
            .first()
        )
        if live is None:
            return SapPosting.objects.create(
                company=company,
                kind=kind,
                source_id=source_id,
                title=title[:255],
                link=link[:255],
                params=params,
                status=SapPostingStatus.SENDING,
                created_by=user,
            )
        if live.status == SapPostingStatus.SENDING:
            raise PostingInProgress(f"{live.title} is being sent to SAP right now.")
        live.status = SapPostingStatus.SENDING
        live.params = params
        live.title = title[:255]
        live.link = link[:255]
        live.save(update_fields=["status", "params", "title", "link", "updated_at"])
        return live


def retry(posting_id):
    """Send a waiting or refused posting now. Returns ``(posting, outcome)``."""
    with transaction.atomic(durable=True):
        posting = SapPosting.objects.select_for_update().get(pk=posting_id)
        if posting.status == SapPostingStatus.SENDING:
            raise PostingInProgress(f"{posting.title} is being sent to SAP right now.")
        if posting.status not in (SapPostingStatus.QUEUED, SapPostingStatus.REJECTED):
            raise ValueError(
                f"Only a posting that is waiting for SAP or was refused can be sent "
                f"again; this one is {posting.get_status_display().lower()}."
            )
        posting.status = SapPostingStatus.SENDING
        posting.save(update_fields=["status", "updated_at"])
    return posting, attempt(posting, by_worker=False)


def cancel(posting_id, user, reason):
    """Stop sending a posting -- it was posted by hand, or is not wanted.

    The record it would have posted is left as it is: cancelling says nothing
    about SAP, only that the app stops trying.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Say why the posting is being cancelled.")
    with transaction.atomic(durable=True):
        posting = SapPosting.objects.select_for_update().get(pk=posting_id)
        if posting.status not in (SapPostingStatus.QUEUED, SapPostingStatus.REJECTED):
            raise ValueError(
                f"Only a posting that is waiting for SAP or was refused can be "
                f"cancelled; this one is {posting.get_status_display().lower()}."
            )
        posting.status = SapPostingStatus.CANCELLED
        posting.cancelled_by = user
        posting.cancel_reason = reason
        posting.next_attempt_at = None
        posting.save(
            update_fields=[
                "status", "cancelled_by", "cancel_reason", "next_attempt_at", "updated_at",
            ]
        )
    return posting


# ---------------------------------------------------------------------------
# one try, logged
# ---------------------------------------------------------------------------

def attempt(posting, *, by_worker):
    """One try at ``posting``, which the caller has made SENDING. Logged."""
    with transaction.atomic(durable=True):
        SapPosting.objects.filter(pk=posting.pk).update(attempts=F("attempts") + 1)
        posting.refresh_from_db(fields=["attempts"])
        log = SapPostingAttempt.objects.create(
            posting=posting,
            number=posting.attempts,
            by_worker=by_worker,
            started_at=timezone.now(),
        )
    try:
        outcome = handler_for(posting.kind).send(posting)
    except Exception as exc:  # noqa: BLE001 -- a bug is logged, never lost
        logger.exception("SAP posting %s (%s) failed unexpectedly", posting.pk, posting.kind)
        outcome = Outcome.rejected(f"Unexpected error: {exc}")
    _finish(posting, log, outcome)
    return outcome


def _backoff(attempts):
    return timedelta(
        seconds=min(BACKOFF_START_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS)
    )


def _finish(posting, log, outcome):
    now = timezone.now()
    with transaction.atomic(durable=True):
        log.finished_at = now
        log.outcome = outcome.kind
        log.message = (outcome.message or "")[:5000]
        log.detail = outcome.detail or {}
        log.save(update_fields=["finished_at", "outcome", "message", "detail"])

        if outcome.link:
            posting.link = outcome.link[:255]
        if outcome.kind == SapPostingOutcome.POSTED:
            posting.status = SapPostingStatus.POSTED
            posting.posted_at = now
            posting.result = outcome.result or {}
            posting.last_error = ""
            posting.next_attempt_at = None
        elif outcome.kind == SapPostingOutcome.WAITING:
            posting.status = SapPostingStatus.QUEUED
            posting.last_error = (outcome.message or "")[:5000]
            posting.next_attempt_at = now + _backoff(posting.attempts)
        else:
            posting.status = SapPostingStatus.REJECTED
            posting.last_error = (outcome.message or "")[:5000]
            posting.next_attempt_at = None
        posting.save(
            update_fields=[
                "status", "posted_at", "result", "last_error", "next_attempt_at", "link",
                "updated_at",
            ]
        )


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------

def run_due(*, limit=20):
    """Send what is due, oldest first. The worker's loop body; returns how many.

    Nothing is claimed while the Service Layer is known down: each try would
    only fail fast and push its next try further off.
    """
    from sap_client import health

    release_stuck()
    if health.failing_fast(health.state_for_call(health.SERVICE_LAYER)):
        return 0
    sent = 0
    while sent < limit:
        posting = _claim_due()
        if posting is None:
            break
        outcome = attempt(posting, by_worker=True)
        _notify(posting, outcome)
        sent += 1
    return sent


def _claim_due():
    with transaction.atomic(durable=True):
        posting = (
            SapPosting.objects.select_for_update(skip_locked=True)
            .filter(status=SapPostingStatus.QUEUED, next_attempt_at__lte=timezone.now())
            .order_by("created_at")
            .first()
        )
        if posting is None:
            return None
        posting.status = SapPostingStatus.SENDING
        posting.save(update_fields=["status", "updated_at"])
        return posting


def release_stuck():
    """Put back a posting whose process died mid-send. Handlers read SAP back first."""
    now = timezone.now()
    released = SapPosting.objects.filter(
        status=SapPostingStatus.SENDING, updated_at__lt=now - STUCK_AFTER
    ).update(status=SapPostingStatus.QUEUED, next_attempt_at=now, updated_at=now)
    if released:
        logger.warning("Released %s SAP posting(s) stuck mid-send", released)
    return released


def bring_forward():
    """SAP is back: everything waiting goes now, not at the end of its backoff."""
    return SapPosting.objects.filter(status=SapPostingStatus.QUEUED).update(
        next_attempt_at=timezone.now()
    )


def _notify(posting, outcome):
    """Tell whoever made it how a posting the worker sent came out.

    Not for a try a person made -- they are looking at the answer -- and not for
    another wait, which would be one message per retry.
    """
    if outcome.kind == SapPostingOutcome.WAITING or posting.created_by_id is None:
        return
    if outcome.kind == SapPostingOutcome.POSTED:
        title = f"Posted to SAP: {posting.title}"
    else:
        title = f"SAP refused: {posting.title}"
    try:
        from notifications.services import NotificationService

        NotificationService.send_notification_to_user(
            user=posting.created_by,
            title=title[:255],
            body=(outcome.message or "")[:1000],
            click_action_url=posting.link,
            reference_type="sap_posting",
            reference_id=posting.pk,
            company=posting.company,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not notify about SAP posting %s", posting.pk)
