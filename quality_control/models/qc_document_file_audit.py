# quality_control/models/qc_document_file_audit.py
"""The change trail behind the QA Procedures library.

A controlled document is only worth as much as the answer to "who changed
it, and when". ``QCDocumentFile`` alone cannot answer that: it carries a
single ``updated_by``, so the moment a second person edits a sheet the
previous editor is gone, and a retire overwrites the trail of every edit
before it.

This model is the append-only record of that history -- one row per
UPLOADED / EDITED / RETIRED event, with the before/after of each field that
moved. Reads are deliberately *not* logged: the library is meant to be read,
and a row per open would bury the handful of events that actually matter.

Two details are worth knowing:

* ``document`` is ``SET_NULL``, and the code and title are **snapshotted**
  onto every row. A trail that disappears with the record it describes is
  not a trail, and the snapshot is also the only honest answer to "what was
  this document called at the time" once someone renames it.
* Nothing here is written by a signal. Every event is recorded explicitly by
  the view that caused it, so the row carries the acting user, their company
  context and their IP -- none of which a model signal can see.
"""

from django.conf import settings
from django.db import models

from company.models import Company


class QCDocumentFileAction(models.TextChoices):
    """What happened to the document."""

    UPLOADED = "UPLOADED", "Uploaded"
    EDITED = "EDITED", "Edited"
    RETIRED = "RETIRED", "Retired"


class QCDocumentFileAuditLog(models.Model):
    """One thing that happened to one controlled PDF."""

    document = models.ForeignKey(
        "quality_control.QCDocumentFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
        help_text="The document this happened to. Null once a row is hard-"
        "deleted from the database; the snapshot below still identifies it.",
    )

    # ---- snapshot: what the document was called when this happened ----
    document_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Document code at the moment of the event, not today's.",
    )
    title = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Title at the moment of the event, not today's.",
    )

    action = models.CharField(max_length=12, choices=QCDocumentFileAction.choices)
    changes = models.JSONField(
        default=dict,
        blank=True,
        help_text="{field: {'old': ..., 'new': ...}} for an edit; the filed "
        "values for an upload; empty for a retire.",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qc_document_file_audit_logs",
        help_text="Who did it. Null only if the account was later deleted.",
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qc_document_file_audit_logs",
        help_text="Company the user was working in. The documents themselves "
        "are shared, so this is the acting context, not the document's owner.",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # `-id` breaks the tie: two events in the same millisecond would
        # otherwise come back in an arbitrary order and paginate unstably.
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at", "-id"]),
            models.Index(fields=["document", "-created_at"]),
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
        ]
        permissions = [
            (
                "can_view_document_file_audit",
                "Can view the QA Procedures audit log",
            ),
        ]
        verbose_name = "QA Procedure Audit Log"
        verbose_name_plural = "QA Procedure Audit Log"

    def __str__(self):
        who = self.user.full_name if self.user else "unknown"
        return f"{self.action} {self.document_code or self.title} by {who}"


def _client_ip(request):
    """Best guess at the caller's address, proxy header first."""
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return forwarded or request.META.get("REMOTE_ADDR") or None


def record_document_file_event(request, document, action, changes=None):
    """Append one row to the trail.

    Called from the view that performed the change, after it succeeded.
    Deliberately never raises: a controlled document must not fail to save
    because its audit row could not be written -- a missing row is visible
    in the log, a lost upload is not.
    """
    try:
        return QCDocumentFileAuditLog.objects.create(
            document=document,
            document_code=document.document_code or "",
            title=document.title or "",
            action=action,
            changes=changes or {},
            user=request.user if request.user.is_authenticated else None,
            company=getattr(getattr(request, "company", None), "company", None),
            ip_address=_client_ip(request),
        )
    except Exception:  # pragma: no cover - defensive
        import logging

        logging.getLogger(__name__).exception(
            "Could not write QA Procedures audit row for document %s", document.pk
        )
        return None
