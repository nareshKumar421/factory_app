"""Every SAP posting the app makes, and every attempt at it.

A posting is one action that has to end up in SAP -- a goods return's A/R
Returns, a GRPO. It is written down before SAP is asked, in a transaction of its
own, so it outlives whatever the action's own transaction does when SAP fails.

SAP not answering leaves it QUEUED, and the worker (``manage.py
run_sap_postings``) sends it again once SAP is back. SAP refusing it leaves it
REJECTED, for a person: a refusal repeats until someone fixes the reason. Each
try is a :class:`SapPostingAttempt`, which is the posting log.
"""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q


class SapPostingStatus(models.TextChoices):
    SENDING = "SENDING", "Sending to SAP"
    QUEUED = "QUEUED", "Waiting for SAP"
    POSTED = "POSTED", "Posted to SAP"
    REJECTED = "REJECTED", "Refused by SAP"
    CANCELLED = "CANCELLED", "Cancelled"


#: A posting in one of these is still going to be sent; at most one per record.
ACTIVE_STATUSES = (SapPostingStatus.SENDING, SapPostingStatus.QUEUED)


class SapPosting(models.Model):
    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="sap_postings"
    )
    #: Which handler sends it, e.g. ``goods_return.receive``; see services.HANDLERS.
    kind = models.CharField(max_length=60)
    #: The record it posts, in the handler's own model.
    source_id = models.PositiveBigIntegerField()
    #: What a person calls it: "Goods return GR-20260928-0012 (invoice 626090482)".
    title = models.CharField(max_length=255)
    #: Where it lives in FactoryFlow, for the log and the notifications.
    link = models.CharField(max_length=255, blank=True)
    #: What the handler needs to send it again: the choices the user made.
    params = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    status = models.CharField(
        max_length=20,
        choices=SapPostingStatus.choices,
        default=SapPostingStatus.SENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    #: What SAP made of it once posted: document numbers, per handler.
    result = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    posted_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sap_postings_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sap_postings_cancelled",
    )
    cancel_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["kind", "source_id"]),
            # The log page: a company's postings, newest first, by tab. It grows
            # by every posting the app makes, so a page must never scan it.
            models.Index(fields=["company", "status", "-created_at"], name="sap_posting_tab_idx"),
            models.Index(fields=["company", "-created_at"], name="sap_posting_all_idx"),
        ]
        constraints = [
            # Two live postings of one record would be two tries at the same SAP
            # document racing each other.
            models.UniqueConstraint(
                fields=["kind", "source_id"],
                condition=Q(status__in=["SENDING", "QUEUED"]),
                name="one_live_sap_posting_per_record",
            ),
        ]

    def __str__(self):
        return f"{self.title} [{self.status}]"


class SapPostingOutcome(models.TextChoices):
    POSTED = "POSTED", "Posted"
    WAITING = "WAITING", "SAP not answering"
    REJECTED = "REJECTED", "Refused"


class SapPostingAttempt(models.Model):
    posting = models.ForeignKey(
        SapPosting, on_delete=models.CASCADE, related_name="attempt_log"
    )
    number = models.PositiveIntegerField()
    #: Sent by the worker, or by a person pressing the button.
    by_worker = models.BooleanField(default=False)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    #: Blank while the attempt is in flight (or its process died in it).
    outcome = models.CharField(
        max_length=20, choices=SapPostingOutcome.choices, blank=True
    )
    message = models.TextField(blank=True)
    #: Per SAP document: the reference, the payload, what SAP answered.
    detail = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(
                fields=["posting", "number"], name="one_attempt_number_per_posting"
            ),
        ]

    def __str__(self):
        return f"{self.posting_id} #{self.number} {self.outcome or 'in flight'}"
