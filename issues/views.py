"""
API for the issue tracker.

Endpoints, all under ``/api/v1/issues/``::

    GET    meta/                      labels, areas, assignable people, my rights
    GET    issues/                     the list  (?q=&state=&page=&page_size=&sort=)
    POST   issues/                     file a new issue
    GET    issues/<number>/            one issue
    PATCH  issues/<number>/            edit it (partial)
    DELETE issues/<number>/            delete it (triage only)
    GET    issues/<number>/timeline/   comments + events, merged
    POST   issues/<number>/comments/   add a comment
    PATCH  comments/<id>/              edit a comment
    DELETE comments/<id>/              delete a comment
    POST   issues/<number>/state/      close / reopen
    POST   uploads/                    upload a screenshot, get a URL back
    GET/POST         labels/           the label master
    PATCH/DELETE     labels/<id>/
    GET/POST         areas/            the area master
    PATCH/DELETE     areas/<id>/

Issues are addressed by **number**, not by primary key: ``#41`` is what people
write in a chat message, and a URL that matches what they say is worth the
lookup. The list envelope is the repo's usual
``{results, count, page, page_size, total_pages, next, previous}`` so the
frontend's ``PaginationControls`` works without a special case.
"""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from company.models import Company
from grpo.pagination import build_page, get_page_params, paginate_queryset

from . import services
from .constants import IssuePriority, IssueState, StateReason
from .models import Issue, IssueArea, IssueComment, IssueLabel
from .permissions import (
    CanCreateIssues,
    CanManageIssueSettings,
    CanTriageIssues,
    CanViewIssues,
    can_edit_comment,
    can_edit_issue,
    can_triage,
    permission_flags,
)
from .search import SORT_ALIASES, parse_query
from .serializers import (
    CommentWriteSerializer,
    IssueAreaSerializer,
    IssueAttachmentSerializer,
    IssueCommentSerializer,
    IssueCreateSerializer,
    IssueDetailSerializer,
    IssueLabelSerializer,
    IssueListSerializer,
    IssueStateSerializer,
    IssueUpdateSerializer,
    TimelineEntrySerializer,
    UserBriefSerializer,
)


def _bad_request(message):
    return Response({"detail": str(message)}, status=status.HTTP_400_BAD_REQUEST)


def _get_issue(number, *, detail=False):
    queryset = (
        Issue.objects.select_related("author", "area", "company", "closed_by", "duplicate_of")
        .prefetch_related("labels", "assignees")
    )
    if detail:
        queryset = queryset.prefetch_related("attachments")
    return get_object_or_404(queryset, number=number)


class IssueMetaAPI(APIView):
    """Everything the screens need to render their pickers, in one call.

    The list page, the new-issue form and the sidebar all want the same four
    lists; fetching them together keeps a page load at two requests instead of
    five.
    """

    permission_classes = [CanViewIssues]

    def get(self, request):
        labels = IssueLabel.objects.filter(is_active=True).annotate(
            open_issues=Count("issues", filter=Q(issues__state=IssueState.OPEN))
        )
        areas = IssueArea.objects.filter(is_active=True).prefetch_related("owners")
        # Only people who could plausibly be assigned or searched for.
        people = User.objects.filter(is_active=True).order_by("full_name", "email")
        return Response(
            {
                "labels": IssueLabelSerializer(labels, many=True).data,
                "areas": IssueAreaSerializer(
                    areas, many=True, context={"request": request}
                ).data,
                "users": UserBriefSerializer(people, many=True).data,
                "companies": [
                    {"id": row.id, "code": row.code, "name": row.name}
                    for row in Company.objects.filter(is_active=True).order_by("name")
                ],
                "priorities": [
                    {"value": value, "label": label}
                    for value, label in IssuePriority.choices
                ],
                "close_reasons": [
                    {"value": value, "label": label}
                    for value, label in StateReason.choices
                ],
                "sorts": sorted(set(SORT_ALIASES)),
                "permissions": permission_flags(request.user),
                "me": UserBriefSerializer(request.user).data
                if request.user.is_authenticated
                else None,
            }
        )


class IssueListAPI(APIView):
    """The list, and filing a new one."""

    permission_classes = [CanCreateIssues]

    def get(self, request):
        parsed = parse_query(request.query_params.get("q", ""))

        # Explicit params win over the query string's qualifiers: the tab is a
        # control the user clicked, the qualifier is text they may have left
        # behind. `state=ALL` clears it entirely; anything unrecognised is
        # treated as absent, so a garbled param falls back to Open rather than
        # to whatever the box happened to say.
        state = (request.query_params.get("state") or "").upper()
        if state == "ALL":
            parsed.state = ""
        elif state in IssueState.values:
            parsed.state = state
        elif not parsed.state:
            parsed.state = IssueState.OPEN

        sort = request.query_params.get("sort") or ""
        resolved_sort = SORT_ALIASES.get(sort.lower())
        if resolved_sort:
            parsed.sort = resolved_sort

        base = services.list_queryset()
        queryset = services.order_issues(
            services.search_issues(parsed, request.user, base=base), parsed.sort
        )

        page, page_size = get_page_params(request)
        rows, _total, meta = paginate_queryset(queryset, page, page_size)
        payload = build_page(
            IssueListSerializer(rows, many=True, context={"request": request}).data,
            meta,
        )
        payload["state_counts"] = services.state_counts(parsed, request.user, base=base)
        payload["permissions"] = permission_flags(request.user)
        # A mistyped qualifier is reported rather than silently ignored, so
        # "labels:bug" does not look like it filtered when it did not.
        payload["unknown_qualifiers"] = parsed.unknown
        return Response(payload)

    def post(self, request):
        serializer = IssueCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            issue = services.create_issue(
                author=request.user,
                title=data["title"],
                body=data.get("body", ""),
                labels=data.get("label_ids", []),
                assignees=data.get("assignee_ids", []),
                area=data.get("area"),
                company=data.get("company"),
                priority=data.get("priority", IssuePriority.MEDIUM),
                page_url=data.get("page_url", ""),
                attachment_ids=data.get("attachment_ids", []),
            )
        except services.IssueError as error:
            return _bad_request(error)
        return Response(
            IssueDetailSerializer(issue, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class IssueDetailAPI(APIView):
    """One issue: read, edit, delete."""

    permission_classes = [CanCreateIssues]

    def get(self, request, number):
        issue = _get_issue(number, detail=True)
        payload = IssueDetailSerializer(issue, context={"request": request}).data
        payload["permissions"] = {
            **permission_flags(request.user),
            "can_edit": can_edit_issue(request.user, issue),
        }
        return Response(payload)

    def patch(self, request, number):
        issue = _get_issue(number, detail=True)
        if not can_edit_issue(request.user, issue):
            return Response(
                {"detail": "You can only edit your own issues."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Triage-only fields: an author may fix their own title and body, but
        # not label, assign or pin their own issue to the top of the board.
        if not can_triage(request.user):
            restricted = {"label_ids", "assignee_ids", "pinned", "locked"} & set(
                request.data
            )
            if restricted:
                return Response(
                    {
                        "detail": "Labels, assignees, pinning and locking need the "
                        "triage permission."
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        serializer = IssueUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            services.update_issue(issue, request.user, **serializer.to_changes())
        except services.IssueError as error:
            return _bad_request(error)
        issue = _get_issue(number, detail=True)
        return Response(IssueDetailSerializer(issue, context={"request": request}).data)

    def delete(self, request, number):
        if not can_triage(request.user):
            return Response(
                {"detail": "Deleting an issue needs the triage permission."},
                status=status.HTTP_403_FORBIDDEN,
            )
        issue = _get_issue(number)
        issue.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class IssueStateAPI(APIView):
    """Close or reopen. The author may close their own; triage may close any."""

    permission_classes = [CanCreateIssues]

    def post(self, request, number):
        issue = _get_issue(number)
        if not can_edit_issue(request.user, issue):
            return Response(
                {"detail": "You can only close your own issues."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = IssueStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if data["state"] == IssueState.CLOSED:
                services.close_issue(
                    issue,
                    request.user,
                    reason=data.get("reason", StateReason.COMPLETED),
                    duplicate_of=data.get("duplicate_of"),
                )
            else:
                services.reopen_issue(issue, request.user)
        except services.IssueError as error:
            return _bad_request(error)
        issue = _get_issue(number, detail=True)
        return Response(IssueDetailSerializer(issue, context={"request": request}).data)


class IssueTimelineAPI(APIView):
    """The merged comment + event timeline for one issue."""

    permission_classes = [CanViewIssues]

    def get(self, request, number):
        issue = get_object_or_404(Issue, number=number)
        entries = services.timeline(issue)
        return Response(
            TimelineEntrySerializer(
                entries, many=True, context={"request": request}
            ).data
        )


class IssueCommentListAPI(APIView):
    """Add a comment to an issue."""

    permission_classes = [CanCreateIssues]

    def post(self, request, number):
        issue = _get_issue(number)
        serializer = CommentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            comment = services.add_comment(
                issue,
                request.user,
                serializer.validated_data["body"],
                attachment_ids=serializer.validated_data.get("attachment_ids", []),
            )
        except services.IssueError as error:
            return _bad_request(error)
        return Response(
            IssueCommentSerializer(comment, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class IssueCommentDetailAPI(APIView):
    """Edit or delete one comment. Authors own their own; triage owns all."""

    permission_classes = [CanCreateIssues]

    def patch(self, request, comment_id):
        comment = get_object_or_404(
            IssueComment.objects.select_related("issue", "author"), pk=comment_id
        )
        if not can_edit_comment(request.user, comment):
            return Response(
                {"detail": "You can only edit your own comments."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = CommentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            comment = services.update_comment(
                comment, request.user, serializer.validated_data["body"]
            )
        except services.IssueError as error:
            return _bad_request(error)
        return Response(
            IssueCommentSerializer(comment, context={"request": request}).data
        )

    def delete(self, request, comment_id):
        comment = get_object_or_404(
            IssueComment.objects.select_related("issue", "author"), pk=comment_id
        )
        if not can_edit_comment(request.user, comment):
            return Response(
                {"detail": "You can only delete your own comments."},
                status=status.HTTP_403_FORBIDDEN,
            )
        services.delete_comment(comment)
        return Response(status=status.HTTP_204_NO_CONTENT)


class IssueUploadAPI(APIView):
    """Upload a screenshot and get a URL back to embed in the markdown.

    The upload happens before the issue exists -- the reporter is still typing --
    so the row is stored unclaimed and the id comes back with it. Submitting the
    issue or comment sends those ids as ``attachment_ids`` and claims them.
    """

    permission_classes = [CanCreateIssues]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        uploads = request.FILES.getlist("files") or request.FILES.getlist("file")
        if not uploads:
            return _bad_request("No file was sent.")
        stored, errors = [], []
        for upload in uploads:
            try:
                stored.append(services.store_upload(upload, request.user))
            except services.IssueError as error:
                errors.append({"filename": upload.name, "detail": str(error)})
        if not stored:
            return Response(
                {"detail": errors[0]["detail"], "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "results": IssueAttachmentSerializer(
                    stored, many=True, context={"request": request}
                ).data,
                "errors": errors,
            },
            status=status.HTTP_201_CREATED,
        )


class IssueLabelListAPI(APIView):
    """The label master."""

    permission_classes = [CanManageIssueSettings]

    def get(self, request):
        labels = IssueLabel.objects.filter(is_active=True).annotate(
            open_issues=Count("issues", filter=Q(issues__state=IssueState.OPEN))
        )
        return Response(IssueLabelSerializer(labels, many=True).data)

    def post(self, request):
        serializer = IssueLabelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(created_by=request.user, updated_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class IssueLabelDetailAPI(APIView):
    permission_classes = [CanManageIssueSettings]

    def patch(self, request, label_id):
        label = get_object_or_404(IssueLabel, pk=label_id)
        serializer = IssueLabelSerializer(label, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)

    def delete(self, request, label_id):
        label = get_object_or_404(IssueLabel, pk=label_id)
        # Deactivated rather than deleted: the label is frozen into every
        # LABELED event that ever used it, and those must keep reading correctly.
        label.is_active = False
        label.updated_by = request.user
        label.save(update_fields=["is_active", "updated_by", "updated_at"])
        label.issues.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)


class IssueAreaListAPI(APIView):
    """The area master -- which part of the software an issue belongs to."""

    permission_classes = [CanManageIssueSettings]

    def get(self, request):
        areas = IssueArea.objects.filter(is_active=True).prefetch_related("owners")
        return Response(
            IssueAreaSerializer(areas, many=True, context={"request": request}).data
        )

    def post(self, request):
        serializer = IssueAreaSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save(created_by=request.user, updated_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class IssueAreaDetailAPI(APIView):
    permission_classes = [CanManageIssueSettings]

    def patch(self, request, area_id):
        area = get_object_or_404(IssueArea, pk=area_id)
        serializer = IssueAreaSerializer(
            area, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)

    def delete(self, request, area_id):
        area = get_object_or_404(IssueArea, pk=area_id)
        area.is_active = False
        area.updated_by = request.user
        area.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class IssueBulkStateAPI(APIView):
    """Close or reopen several issues at once, from the list's checkboxes."""

    permission_classes = [CanTriageIssues]

    def post(self, request):
        numbers = request.data.get("numbers") or []
        target = (request.data.get("state") or "").upper()
        if target not in IssueState.values:
            return _bad_request("state must be OPEN or CLOSED.")
        reason = request.data.get("reason") or StateReason.COMPLETED
        issues = Issue.objects.filter(number__in=numbers)
        changed = 0
        for issue in issues:
            try:
                if target == IssueState.CLOSED:
                    services.close_issue(issue, request.user, reason=reason)
                else:
                    services.reopen_issue(issue, request.user)
                changed += 1
            except services.IssueError:
                continue
        return Response({"changed": changed})
