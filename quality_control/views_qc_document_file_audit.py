# quality_control/views_qc_document_file_audit.py
"""Read APIs for the QA Procedures audit log.

Read-only from the rest of the app's point of view: the rows are appended by
``views_qc_document_file`` as changes happen, and nothing here can create,
edit or remove one. Two readers are served:

* the manager's log page -- everything, filtered by user / action / document /
  date, with a CSV export for an auditor who wants it offline;
* the History panel inside the PDF viewer -- the same rows narrowed to one
  document.

Both run off the same queryset, so the two views can never disagree.
"""

import csv

from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import serializers
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import QCDocumentFileAction, QCDocumentFileAuditLog

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# A CSV is pulled for an auditor, not as a data dump; past this the export is
# capped rather than timing the request out.
MAX_CSV_ROWS = 20000

# What each logged field is called on screen. Anything not listed falls back to
# the raw field name, so a newly logged field shows up in the log the day it is
# logged rather than the day someone remembers to add it here.
FIELD_LABELS = {
    "document_code": "Document code",
    "title": "Title",
    "revision": "Revision",
    "procedure_type": "Type",
    "file": "File",
    "is_active": "Status",
}

VALUE_LABELS = {
    "INHOUSE": "In-house",
    "STANDARD": "Standard",
    True: "Active",
    False: "Retired",
}


class CanViewDocumentFileAudit(BasePermission):
    """Deliberately its own permission, not the manage one.

    The people being logged should not be the people who decide what the log
    says, so the right to upload a procedure does not carry the right to read
    the trail of who changed it.
    """

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_view_document_file_audit")


def _render_value(value):
    """One side of a change, as a manager would read it."""
    if value is None:
        return "-"
    if value == "":
        return "(blank)"
    # `True`/`False` hash equal to `1`/`0`, but no logged field uses those as
    # values, so the lookup is unambiguous.
    return str(VALUE_LABELS.get(value, value))


def _summarise(changes):
    """The whole diff as one line: ``Title: a > b | Revision: - > 01``."""
    if not isinstance(changes, dict):
        return ""
    parts = []
    for field, move in changes.items():
        if not isinstance(move, dict):
            continue
        label = FIELD_LABELS.get(field, field.replace("_", " ").capitalize())
        old = _render_value(move.get("old"))
        new = _render_value(move.get("new"))
        parts.append(f"{label}: {old} → {new}")
    return " · ".join(parts)


class QCDocumentFileAuditLogSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    user_name = serializers.CharField(
        source="user.full_name", read_only=True, allow_null=True, default=None
    )
    user_email = serializers.CharField(
        source="user.email", read_only=True, allow_null=True, default=None
    )
    company_name = serializers.CharField(
        source="company.name", read_only=True, allow_null=True, default=None
    )
    changes_summary = serializers.SerializerMethodField()
    # True once the underlying record is gone: the row still reads, but there
    # is nothing left to open.
    document_missing = serializers.SerializerMethodField()

    class Meta:
        model = QCDocumentFileAuditLog
        fields = [
            "id",
            "document",
            "document_code",
            "title",
            "document_missing",
            "action",
            "action_label",
            "changes",
            "changes_summary",
            "user",
            "user_name",
            "user_email",
            "company_name",
            "ip_address",
            "created_at",
        ]
        read_only_fields = fields

    def get_changes_summary(self, obj):
        return _summarise(obj.changes)

    def get_document_missing(self, obj):
        return obj.document_id is None


def _company(request):
    return request.company.company


def _visible(company):
    """Every event this company is entitled to read.

    Scoped by the *document*, not by who was acting: the library is shared, so
    an edit made from another company is still an edit to a procedure this
    company follows, and hiding it would make the trail lie. Documents private
    to another company stay hidden, and rows whose document has been erased
    outright are kept -- losing the record of a deletion is exactly what an
    audit log exists to prevent.

    ``is_active`` is intentionally not filtered: the RETIRED row of a retired
    document is the row a manager most wants to see.
    """
    return QCDocumentFileAuditLog.objects.filter(
        Q(document__isnull=True)
        | Q(document__company=company)
        | Q(document__company__isnull=True)
    ).select_related("user", "company", "document")


def _parse_positive_int(raw, default):
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _filtered(request, document_id=None):
    """Apply the query-string filters to the visible rows."""
    queryset = _visible(_company(request))

    if document_id is not None:
        queryset = queryset.filter(document_id=document_id)
    else:
        wanted = _parse_positive_int(request.query_params.get("document"), 0)
        if wanted:
            queryset = queryset.filter(document_id=wanted)

    action = (request.query_params.get("action") or "").strip().upper()
    if action in QCDocumentFileAction.values:
        queryset = queryset.filter(action=action)

    user_id = _parse_positive_int(request.query_params.get("user"), 0)
    if user_id:
        queryset = queryset.filter(user_id=user_id)

    # Parsed rather than passed straight through: an unparseable date would
    # otherwise raise a 500 out of the ORM instead of being ignored. `__date`
    # compares in the project timezone (Asia/Kolkata), so "3 Sep" means the
    # Indian day an operator would name, not a UTC window.
    date_from = parse_date((request.query_params.get("date_from") or "").strip())
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    date_to = parse_date((request.query_params.get("date_to") or "").strip())
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)

    search = (request.query_params.get("search") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(document_code__icontains=search)
            | Q(title__icontains=search)
            | Q(user__full_name__icontains=search)
            | Q(user__email__icontains=search)
        )
    return queryset


class QCDocumentFileAuditLogAPI(APIView):
    """The manager's view of the trail -- filtered, paginated, exportable.

    Also serves the per-document History panel, when the URL carries a
    ``document_id``.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDocumentFileAudit]

    def get(self, request, document_id=None):
        queryset = _filtered(request, document_id)

        # `export`, not `format`: DRF reserves `?format=` for content
        # negotiation and answers 404 for a renderer it does not have, so the
        # request would never reach this method.
        if (request.query_params.get("export") or "").lower() == "csv":
            return self._csv(queryset)

        page = _parse_positive_int(request.query_params.get("page"), 1)
        page_size = min(
            _parse_positive_int(
                request.query_params.get("page_size"), DEFAULT_PAGE_SIZE
            ),
            MAX_PAGE_SIZE,
        )
        total = queryset.count()
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        start = (page - 1) * page_size

        # One extra GROUP BY over the same filtered set, so the header can say
        # "3 uploads, 11 edits, 1 retired" for what is on screen rather than
        # for the whole table.
        counts = {action: 0 for action in QCDocumentFileAction.values}
        # `.order_by()` clears the model's default ordering. Without it Django
        # folds `created_at` into the GROUP BY and the aggregate degenerates
        # into one group per row, i.e. every count comes back as 1.
        for row in queryset.order_by().values("action").annotate(n=Count("id")):
            counts[row["action"]] = row["n"]

        return Response(
            {
                "results": QCDocumentFileAuditLogSerializer(
                    queryset[start : start + page_size], many=True
                ).data,
                "count": total,
                "action_counts": counts,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "next": page < total_pages,
                "previous": page > 1,
            }
        )

    def _csv(self, queryset):
        """The same rows an auditor is looking at, as a file they can keep."""
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="qa-procedures-audit-{stamp}.csv"'
        )

        writer = csv.writer(response)
        writer.writerow(
            [
                "When",
                "User",
                "Email",
                "Action",
                "Document code",
                "Title",
                "Change",
                "Company",
                "IP address",
            ]
        )
        for row in queryset[:MAX_CSV_ROWS]:
            writer.writerow(
                [
                    timezone.localtime(row.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                    row.user.full_name if row.user else "",
                    row.user.email if row.user else "",
                    row.get_action_display(),
                    row.document_code,
                    row.title,
                    _summarise(row.changes),
                    row.company.name if row.company else "",
                    row.ip_address or "",
                ]
            )
        return response


class QCDocumentFileAuditFilterOptionsAPI(APIView):
    """The values worth offering in the log page's filter dropdowns.

    Built from the rows themselves rather than from the user directory, so the
    list is exactly the people who have touched a procedure -- and reading the
    log does not require the right to list every user in the company.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDocumentFileAudit]

    def get(self, request):
        rows = _visible(_company(request))

        users = (
            rows.exclude(user__isnull=True)
            .values("user_id", "user__full_name", "user__email")
            .distinct()
            .order_by("user__full_name")
        )
        documents = (
            rows.exclude(document__isnull=True)
            .values("document_id", "document__document_code", "document__title")
            .distinct()
            .order_by("document__document_code", "document__title")
        )

        return Response(
            {
                "users": [
                    {
                        "id": row["user_id"],
                        "name": row["user__full_name"],
                        "email": row["user__email"],
                    }
                    for row in users
                ],
                "documents": [
                    {
                        "id": row["document_id"],
                        "document_code": row["document__document_code"],
                        "title": row["document__title"],
                    }
                    for row in documents
                ],
                "actions": [
                    {"value": value, "label": label}
                    for value, label in QCDocumentFileAction.choices
                ],
            }
        )
