"""
Views for the construction module.

Thin on purpose: authenticate, deserialise, call one function in
``services.py``, serialise the result. Every rule lives in the service layer.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Case, F, IntegerField, Q, Sum, Value, When
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import services
from .constants import (
    AttachmentKind,
    ExpenseBatchStatus,
    ProjectStatus,
    RevisionStatus,
)
from .models import (
    DailyLog,
    Expense,
    ExpenseBatch,
    Project,
    ProjectAttachment,
    ProjectRevision,
)
from .permissions import (
    CanApproveExpense,
    CanApproveProject,
    CanCloseProject,
    CanCreateProject,
    CanEditProject,
    CanLogDailyWork,
    CanRecordExpense,
    CanReviewAnything,
    CanViewProject,
)
from .serializers import (
    CompleteProjectSerializer,
    DailyLogPhotoSerializer,
    DailyLogPhotoWriteSerializer,
    DailyLogSerializer,
    DailyLogWriteSerializer,
    DecisionSerializer,
    EstimateLineSerializer,
    EstimateWriteSerializer,
    BatchDecisionSerializer,
    ExpenseBatchDetailSerializer,
    ExpenseBatchSerializer,
    SubmitBatchSerializer,
    ExpenseSerializer,
    ExpenseWriteSerializer,
    ProjectAttachmentSerializer,
    ProjectAttachmentWriteSerializer,
    ProjectDetailSerializer,
    ProjectListSerializer,
    ProjectPatchSerializer,
    ProjectRevisionSerializer,
    ProjectWriteSerializer,
    RevisionWriteSerializer,
)


def _project_or_404(pk, request):
    """A project this caller may see, or 404.

    Never 403: a project the caller cannot see should not be distinguishable
    from one that does not exist.
    """
    return get_object_or_404(
        services.visible_projects(request.user, request.company.company), pk=pk
    )


def _required_date(request, param="date"):
    value = parse_date(request.query_params.get(param, "") or "")
    if not value:
        raise services.ConstructionError(
            f"A valid ?{param}=YYYY-MM-DD is required.", "date_required", {}
        )
    return value


def _resolve_people(data):
    """Turn the ``manager`` / ``site_incharge`` ids into users, in place."""
    users = get_user_model().objects.filter(is_active=True)
    if "manager" in data:
        # A draft may not have named one yet; pk=None would 404 rather than
        # leaving the field empty.
        manager = data["manager"]
        data["manager"] = get_object_or_404(users, pk=manager) if manager else None
    if "site_incharge" in data:
        incharge = data["site_incharge"]
        data["site_incharge"] = (
            get_object_or_404(users, pk=incharge) if incharge else None
        )
    return data


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class ProjectListCreateAPI(APIView):
    """GET  : the project register, filtered.
    POST : raise a project.
    """

    def get_permissions(self):
        gate = CanCreateProject if self.request.method == "POST" else CanViewProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def get(self, request):
        qs = services.visible_projects(request.user, request.company.company)
        qs = qs.select_related("manager", "site_incharge")

        params = request.query_params
        if params.get("status"):
            # The register's tabs send a comma-separated list -- "Live" is
            # APPROVED,IN_PROGRESS,ON_HOLD. A value that is not a status
            # matches nothing rather than being dropped, because a filter
            # that silently returns everything is worse than one that
            # returns nothing: the first looks like it worked.
            qs = qs.filter(
                status__in=[
                    value.strip()
                    for value in params["status"].split(",")
                    if value.strip()
                ]
            )
        if params.get("manager"):
            qs = qs.filter(manager_id=params["manager"])
        if params.get("search"):
            term = params["search"]
            qs = qs.filter(Q(code__icontains=term) | Q(name__icontains=term))
        if params.get("mine") == "true":
            qs = qs.filter(Q(manager=request.user) | Q(site_incharge=request.user))
        if params.get("over_budget") == "true":
            qs = qs.filter(spent_amount__gt=F("sanctioned_budget"))
        if params.get("overdue") == "true":
            qs = qs.filter(
                expected_end_date__lt=timezone.localdate(),
                actual_end_date__isnull=True,
            ).exclude(status__in=[ProjectStatus.COMPLETED, ProjectStatus.CANCELLED])

        return Response(
            ProjectListSerializer(qs, many=True, context={"request": request}).data
        )

    def post(self, request):
        serializer = ProjectWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = services.create_project(
            company=request.company.company,
            user=request.user,
            **_resolve_people(dict(serializer.validated_data)),
        )
        return Response(
            ProjectDetailSerializer(project, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ProjectDetailAPI(APIView):
    """GET : one project. PATCH : edit it, while it is still a draft."""

    def get_permissions(self):
        gate = CanEditProject if self.request.method == "PATCH" else CanViewProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        return Response(ProjectDetailSerializer(project, context={"request": request}).data)

    def patch(self, request, pk):
        project = _project_or_404(pk, request)
        serializer = ProjectPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        project = services.update_project(
            project,
            user=request.user,
            **_resolve_people(dict(serializer.validated_data)),
        )
        return Response(ProjectDetailSerializer(project, context={"request": request}).data)


class ProjectSummaryAPI(APIView):
    """GET : the header every screen shows."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProject]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        return Response(services.project_summary(project))


class _ProjectActionAPI(APIView):
    """One POST that moves a project's status. Subclasses name the service call."""

    gate = CanCloseProject
    serializer_class = DecisionSerializer

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), self.gate()]

    def act(self, project, request, data):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request, pk):
        project = _project_or_404(pk, request)
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = self.act(project, request, serializer.validated_data)
        return Response(ProjectDetailSerializer(project, context={"request": request}).data)


class ProjectSubmitAPI(_ProjectActionAPI):
    gate = CanEditProject

    def act(self, project, request, data):
        return services.submit_project(project, user=request.user)


class ProjectApproveAPI(_ProjectActionAPI):
    gate = CanApproveProject

    def act(self, project, request, data):
        return services.approve_project(
            project, user=request.user, note=data.get("note", "")
        )


class ProjectRejectAPI(_ProjectActionAPI):
    gate = CanApproveProject

    def act(self, project, request, data):
        return services.reject_project(
            project, user=request.user, note=data.get("note", "")
        )


class ProjectHoldAPI(_ProjectActionAPI):
    def act(self, project, request, data):
        return services.hold_project(
            project, user=request.user, note=data.get("note", "")
        )


class ProjectResumeAPI(_ProjectActionAPI):
    def act(self, project, request, data):
        return services.resume_project(project, user=request.user)


class ProjectCompleteAPI(_ProjectActionAPI):
    serializer_class = CompleteProjectSerializer

    def act(self, project, request, data):
        return services.complete_project(
            project,
            user=request.user,
            actual_end_date=data.get("actual_end_date"),
            note=data.get("note", ""),
        )


class ProjectCancelAPI(_ProjectActionAPI):
    def act(self, project, request, data):
        return services.cancel_project(
            project, user=request.user, note=data.get("note", "")
        )


class ProjectEstimateAPI(APIView):
    """GET : the estimate's breakdown. PUT : replace it.

    Replaced wholesale rather than line by line because it is a table --
    somebody pastes twenty rows, renumbers two and deletes one, then saves once.

    When any line exists the lines ARE the estimate, so a save here re-derives
    ``estimated_cost`` from their sum. Sending an empty list clears the
    breakdown and hands the figure back to being typed.
    """

    def get_permissions(self):
        gate = CanEditProject if self.request.method == "PUT" else CanViewProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        lines = project.estimate_lines.filter(is_active=True)
        return Response(
            {
                "lines": EstimateLineSerializer(lines, many=True).data,
                "total": services.estimate_total(project) or 0,
                "estimated_cost": project.estimated_cost,
                "is_editable": project.is_editable,
            }
        )

    def put(self, request, pk):
        project = _project_or_404(pk, request)
        serializer = EstimateWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.save_estimate_lines(
            project, user=request.user, lines=serializer.validated_data["lines"]
        )
        project.refresh_from_db()
        lines = project.estimate_lines.filter(is_active=True)
        return Response(
            {
                "lines": EstimateLineSerializer(lines, many=True).data,
                "total": services.estimate_total(project) or 0,
                "estimated_cost": project.estimated_cost,
                "is_editable": project.is_editable,
            }
        )


class ProjectAttachmentAPI(APIView):
    """The papers behind a project: the quotation it was costed from, drawings,
    the sanction letter.

    Uploadable at any status, including a draft and a finished project -- the
    sanction letter usually arrives after approval and the completion
    certificate after the work. This is paperwork, not a write against a live
    project, so it is not gated the way the daily log is.
    """

    def get_permissions(self):
        gate = CanViewProject if self.request.method == "GET" else CanEditProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        # Maps first, then newest paper first. Ordering on ``kind`` itself
        # would sort the stored strings, and "DOCUMENT" sorts above "MAP".
        rows = (
            project.attachments.filter(is_active=True)
            .select_related("created_by")
            .annotate(
                map_first=Case(
                    When(kind=AttachmentKind.MAP, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .order_by("map_first", "-created_at", "-id")
        )
        return Response(
            ProjectAttachmentSerializer(
                rows, many=True, context={"request": request}
            ).data
        )

    def post(self, request, pk):
        project = _project_or_404(pk, request)
        serializer = ProjectAttachmentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attachment = project.attachments.create(
            created_by=request.user,
            updated_by=request.user,
            **serializer.validated_data,
        )
        return Response(
            ProjectAttachmentSerializer(
                attachment, context={"request": request}
            ).data,
            status=status.HTTP_201_CREATED,
        )


class ProjectAttachmentDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditProject]

    def delete(self, request, pk):
        attachment = get_object_or_404(
            ProjectAttachment.objects.filter(
                project__in=services.visible_projects(
                    request.user, request.company.company
                )
            ),
            pk=pk,
            is_active=True,
        )
        attachment.is_active = False
        attachment.updated_by = request.user
        attachment.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# The daily loop
# ---------------------------------------------------------------------------


class DailyLogListCreateAPI(APIView):
    """GET  : the project's days, newest first.
    POST : the day's log AND its expenses, in one transaction.
    """

    def get_permissions(self):
        gate = CanLogDailyWork if self.request.method == "POST" else CanViewProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        qs = project.daily_logs.filter(is_active=True).prefetch_related("photos", "stop_reasons")
        if request.query_params.get("from"):
            qs = qs.filter(log_date__gte=parse_date(request.query_params["from"]))
        if request.query_params.get("to"):
            qs = qs.filter(log_date__lte=parse_date(request.query_params["to"]))

        # One query for the whole page's day-spend rather than one per row.
        spend_by_date = {
            row["spend_date"]: row["total"]
            for row in project.expenses.filter(is_active=True)
            .values("spend_date")
            .annotate(total=Sum("amount"))
        }
        logs = list(qs)
        for log in logs:
            log.day_spend = spend_by_date.get(log.log_date, Decimal("0.00"))
        return Response(
            DailyLogSerializer(logs, many=True, context={"request": request}).data
        )

    def post(self, request, pk):
        project = _project_or_404(pk, request)
        serializer = DailyLogWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        log, warning = services.save_daily_log(
            project, user=request.user, log_data=dict(serializer.validated_data)
        )
        return Response(
            {
                "log": DailyLogSerializer(log, context={"request": request}).data,
                "warning": warning,
            },
            status=status.HTTP_201_CREATED,
        )


class DailyLogDetailAPI(APIView):
    def get_permissions(self):
        gate = CanLogDailyWork if self.request.method == "PATCH" else CanViewProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def _log(self, pk, request):
        return get_object_or_404(
            DailyLog.objects.filter(
                project__in=services.visible_projects(
                    request.user, request.company.company
                )
            ).prefetch_related("photos", "stop_reasons"),
            pk=pk,
            is_active=True,
        )

    def get(self, request, pk):
        return Response(
            DailyLogSerializer(
                self._log(pk, request), context={"request": request}
            ).data
        )

    def patch(self, request, pk):
        log = self._log(pk, request)
        serializer = DailyLogWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        # Editing a log may not silently move it to another date.
        data["log_date"] = log.log_date
        updated, warning = services.save_daily_log(
            log.project, user=request.user, log_data=data
        )
        return Response(
            {
                "log": DailyLogSerializer(
                    updated, context={"request": request}
                ).data,
                "warning": warning,
            }
        )


class DailyLogPhotoAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanLogDailyWork]

    def _log(self, pk, request):
        return get_object_or_404(
            DailyLog.objects.filter(
                project__in=services.visible_projects(
                    request.user, request.company.company
                )
            ),
            pk=pk,
            is_active=True,
        )

    def post(self, request, pk):
        log = self._log(pk, request)
        serializer = DailyLogPhotoWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        photo = log.photos.create(
            created_by=request.user,
            updated_by=request.user,
            **serializer.validated_data,
        )
        return Response(
            DailyLogPhotoSerializer(photo, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request, pk, photo_id):
        log = self._log(pk, request)
        photo = get_object_or_404(log.photos, pk=photo_id)
        photo.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ExpenseListCreateAPI(APIView):
    """GET : the project's payments, filtered and ordered. POST : record one."""

    #: Columns the table may sort on, with an optional "-" for descending.
    #: A whitelist rather than passing the parameter through: ``order_by`` on
    #: arbitrary user input walks relations and is an easy way to sort by
    #: something that should not be readable.
    ORDERING_FIELDS = {
        "spend_date",
        "amount",
        "category",
        "description",
        "paid_to",
        "created_at",
    }
    DEFAULT_ORDERING = ("-spend_date", "-id")

    def get_permissions(self):
        gate = CanRecordExpense if self.request.method == "POST" else CanViewProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        qs = project.expenses.filter(is_active=True).select_related(
            "created_by", "batch", "project"
        )
        params = request.query_params

        if params.get("category"):
            qs = qs.filter(category__in=params["category"].split(","))
        if params.get("batch_status"):
            qs = qs.filter(batch__status__in=params["batch_status"].split(","))
        if params.get("batch"):
            qs = qs.filter(batch_id=params["batch"])
        if params.get("payment_mode"):
            qs = qs.filter(payment_mode__in=params["payment_mode"].split(","))
        if params.get("from"):
            qs = qs.filter(spend_date__gte=parse_date(params["from"]))
        if params.get("to"):
            qs = qs.filter(spend_date__lte=parse_date(params["to"]))
        if params.get("search"):
            term = params["search"]
            qs = qs.filter(
                Q(description__icontains=term)
                | Q(paid_to__icontains=term)
                | Q(reference_no__icontains=term)
            )

        ordering = params.get("ordering") or ""
        field = ordering.lstrip("-")
        if field in self.ORDERING_FIELDS:
            # Tie-break on id so a page of same-day rows keeps a stable order
            # between requests rather than shuffling under the reader.
            qs = qs.order_by(ordering, "-id")
        else:
            qs = qs.order_by(*self.DEFAULT_ORDERING)

        rows = list(qs)
        return Response(
            {
                "results": ExpenseSerializer(
                    rows, many=True, context={"request": request}
                ).data,
                # The filtered total, so the table can foot itself without the
                # client re-adding what it was just sent.
                "total": str(
                    sum((row.amount for row in rows), Decimal("0.00")).quantize(
                        Decimal("0.01")
                    )
                ),
                "count": len(rows),
            }
        )

    def post(self, request, pk):
        project = _project_or_404(pk, request)
        serializer = ExpenseWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expense, warning = services.record_expense(
            project, user=request.user, **serializer.validated_data
        )
        return Response(
            {
                "expense": ExpenseSerializer(
                    expense, context={"request": request}
                ).data,
                "warning": warning,
            },
            status=status.HTTP_201_CREATED,
        )


class ExpenseDetailAPI(APIView):
    def get_permissions(self):
        gate = CanViewProject if self.request.method == "GET" else CanRecordExpense
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def _expense(self, pk, request):
        return get_object_or_404(
            Expense.objects.filter(
                project__in=services.visible_projects(
                    request.user, request.company.company
                )
            ),
            pk=pk,
            is_active=True,
        )

    def get(self, request, pk):
        return Response(
            ExpenseSerializer(
                self._expense(pk, request), context={"request": request}
            ).data
        )

    def patch(self, request, pk):
        expense = self._expense(pk, request)
        serializer = ExpenseWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        expense, warning = services.update_expense(
            expense, user=request.user, **serializer.validated_data
        )
        return Response(
            {
                "expense": ExpenseSerializer(
                    expense, context={"request": request}
                ).data,
                "warning": warning,
            }
        )

    def delete(self, request, pk):
        expense = self._expense(pk, request)
        services.delete_expense(expense, user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ExpenseBatchAPI(APIView):
    """GET : this project's batches, newest first, the open one detailed.
    POST : send the open batch for approval.
    """

    def get_permissions(self):
        gate = CanRecordExpense if self.request.method == "POST" else CanViewProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        batches = project.expense_batches.filter(is_active=True).select_related(
            "project", "submitted_by", "decided_by"
        )
        open_batch = services.current_batch(project, create=False)
        return Response(
            {
                "batches": ExpenseBatchSerializer(
                    batches, many=True, context={"request": request}
                ).data,
                "open": (
                    ExpenseBatchDetailSerializer(
                        open_batch, context={"request": request}
                    ).data
                    if open_batch
                    else None
                ),
            }
        )

    def post(self, request, pk):
        project = _project_or_404(pk, request)
        serializer = SubmitBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = services.submit_expense_batch(
            project,
            user=request.user,
            expense_ids=serializer.validated_data.get("expense_ids"),
        )
        return Response(
            ExpenseBatchDetailSerializer(batch, context={"request": request}).data
        )


class ExpenseBatchDecisionAPI(APIView):
    """POST {decision, note} : approve the whole batch, or send it back.

    Approving approves every line in it -- that is the point of the batch.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveExpense]

    def post(self, request, pk):
        batch = get_object_or_404(
            ExpenseBatch.objects.filter(
                project__in=services.visible_projects(
                    request.user, request.company.company
                )
            ).select_related("project"),
            pk=pk,
            is_active=True,
        )
        serializer = BatchDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        batch = services.decide_expense_batch(
            batch,
            user=request.user,
            decision=data["decision"],
            note=data.get("note", ""),
        )
        return Response(
            {
                "batch": ExpenseBatchDetailSerializer(
                    batch, context={"request": request}
                ).data,
                "summary": services.project_summary(batch.project),
            }
        )


class ProjectDayAPI(APIView):
    """GET ?date=YYYY-MM-DD : the screen this whole module is for."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProject]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        day = _required_date(request)
        data = services.day_view(project, day)
        return Response(
            {
                "date": data["date"],
                "log": (
                    DailyLogSerializer(
                        data["log"], context={"request": request}
                    ).data
                    if data["log"]
                    else None
                ),
                "expenses": ExpenseSerializer(
                    data["expenses"], many=True, context={"request": request}
                ).data,
                "spent_today": data["spent_today"],
                "spent_to_date": data["spent_to_date"],
                "budget_remaining": data["budget_remaining"],
            }
        )


class ProjectSpendSummaryAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProject]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        return Response(services.spend_summary(project))


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


class RevisionListCreateAPI(APIView):
    def get_permissions(self):
        gate = CanCreateProject if self.request.method == "POST" else CanViewProject
        return [IsAuthenticated(), HasCompanyContext(), gate()]

    def get(self, request, pk):
        project = _project_or_404(pk, request)
        qs = project.revisions.filter(is_active=True).select_related(
            "requested_by", "decided_by", "project"
        )
        return Response(
            ProjectRevisionSerializer(qs, many=True, context={"request": request}).data
        )

    def post(self, request, pk):
        project = _project_or_404(pk, request)
        serializer = RevisionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        revision = services.request_revision(
            project, user=request.user, **serializer.validated_data
        )
        return Response(
            ProjectRevisionSerializer(revision, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class _RevisionActionAPI(APIView):
    gate = CanApproveProject

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), self.gate()]

    def _revision(self, pk, request):
        return get_object_or_404(
            ProjectRevision.objects.filter(
                project__in=services.visible_projects(
                    request.user, request.company.company
                )
            ).select_related("project"),
            pk=pk,
            is_active=True,
        )

    def act(self, revision, request, data):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request, pk):
        revision = self._revision(pk, request)
        serializer = DecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        revision = self.act(revision, request, serializer.validated_data)
        return Response(
            ProjectRevisionSerializer(revision, context={"request": request}).data
        )


class RevisionApproveAPI(_RevisionActionAPI):
    def act(self, revision, request, data):
        return services.approve_revision(
            revision, user=request.user, note=data.get("note", "")
        )


class RevisionRejectAPI(_RevisionActionAPI):
    def act(self, revision, request, data):
        return services.reject_revision(
            revision, user=request.user, note=data.get("note", "")
        )


class RevisionWithdrawAPI(_RevisionActionAPI):
    gate = CanCreateProject

    def act(self, revision, request, data):
        if revision.requested_by_id != request.user.id:
            raise services.ConstructionError(
                "Only the person who raised a revision can withdraw it.",
                "not_requester",
                {},
            )
        return services.withdraw_revision(revision, user=request.user)


class ApprovalQueueAPI(APIView):
    """GET : everything waiting on a decision, oldest first.

    Each section is gated on its own right rather than the endpoint being gated
    on one of them: sanctioning a budget and checking a day's cement bill are
    different jobs, usually held by different people.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanReviewAnything]

    def get(self, request):
        company = request.company.company
        visible = services.visible_projects(request.user, company)
        payload = {"projects": [], "revisions": [], "batches": []}

        if request.user.has_perm("construction_projects.can_approve_project"):
            projects = (
                Project.objects.filter(
                    company=company,
                    is_active=True,
                    status=ProjectStatus.PENDING_APPROVAL,
                )
                .select_related("manager")
                .order_by("submitted_at")
            )
            revisions = (
                ProjectRevision.objects.filter(
                    project__company=company,
                    is_active=True,
                    status=RevisionStatus.PENDING,
                )
                .select_related("project", "requested_by")
                .order_by("requested_at")
            )
            payload["projects"] = ProjectListSerializer(
                projects, many=True, context={"request": request}
            ).data
            payload["revisions"] = ProjectRevisionSerializer(
                revisions, many=True, context={"request": request}
            ).data

        if request.user.has_perm("construction_projects.can_approve_expense"):
            batches = (
                ExpenseBatch.objects.filter(
                    project__in=visible,
                    is_active=True,
                    status=ExpenseBatchStatus.SUBMITTED,
                )
                .select_related("project", "submitted_by")
                .order_by("submitted_at")
            )
            payload["batches"] = ExpenseBatchDetailSerializer(
                batches, many=True, context={"request": request}
            ).data

        return Response(payload)
