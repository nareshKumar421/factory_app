"""/api/v1/sap-postings/ -- the SAP posting log, and sending a posting again.

Scoped to the company in the request, like every other list here.
"""
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import services
from .models import SapPosting, SapPostingStatus
from .permissions import CanActOnSapPostings, CanViewSapPostings
from .serializers import SapPostingDetailSerializer, SapPostingSerializer

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def _scoped(request):
    return SapPosting.objects.filter(company=request.company.company).select_related(
        "company", "created_by", "cancelled_by"
    )


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _day_start(value):
    """A ``YYYY-MM-DD`` as the start of that day here, or None."""
    day = parse_date(value or "")
    if day is None:
        return None
    return timezone.make_aware(datetime.combine(day, time.min))


class SapPostingListView(APIView):
    """GET, one page at a time: ?status= &kind= &q= &date_from= &date_to= &page= &page_size=

    The log gains a row for every posting the app makes, so it is never sent
    whole. Dates are compared as a range on ``created_at``, not ``__date``, so
    the company/status/created_at index serves them. ``q`` matches the title
    (document and invoice numbers) or an SAP document number exactly.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapPostings]

    def get(self, request):
        postings = _scoped(request)
        params = request.query_params
        if params.get("status") in SapPostingStatus.values:
            postings = postings.filter(status=params["status"])
        if params.get("kind"):
            postings = postings.filter(kind=params["kind"])
        query = (params.get("q") or "").strip()
        if query:
            postings = postings.filter(
                Q(title__icontains=query) | Q(result__doc_nums__contains=[query])
            )
        since = _day_start(params.get("date_from"))
        if since:
            postings = postings.filter(created_at__gte=since)
        until = _day_start(params.get("date_to"))
        if until:
            postings = postings.filter(created_at__lt=until + timedelta(days=1))
        postings = postings.order_by("-created_at", "-id")

        page_size = min(_positive_int(params.get("page_size"), DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
        total = postings.count()
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(_positive_int(params.get("page"), 1), total_pages)
        start = (page - 1) * page_size
        return Response(
            {
                "results": SapPostingSerializer(postings[start:start + page_size], many=True).data,
                "count": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "next": page < total_pages,
                "previous": page > 1,
            }
        )


class SapPostingCountsView(APIView):
    """GET -- how many wait for SAP and how many SAP refused; the kinds to filter by.

    What the tabs and the sidebar badge need, and nothing else: the badge polls
    this, so it must stay two indexed counts however long the log gets.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapPostings]

    def get(self, request):
        company = request.company.company
        counts = {
            code: SapPosting.objects.filter(company=company, status=code).count()
            for code in (SapPostingStatus.QUEUED, SapPostingStatus.REJECTED)
        }
        kinds = [{"value": kind, "label": label} for kind, label in services.KIND_LABELS.items()]
        return Response({"counts": counts, "kinds": kinds})


class SapPostingDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapPostings]

    def get(self, request, pk):
        posting = get_object_or_404(_scoped(request).prefetch_related("attempt_log"), pk=pk)
        return Response(SapPostingDetailSerializer(posting).data)


class _ActionView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanActOnSapPostings]

    def respond(self, request, pk):
        posting = get_object_or_404(_scoped(request).prefetch_related("attempt_log"), pk=pk)
        return Response(SapPostingDetailSerializer(posting).data)


class SapPostingRetryView(_ActionView):
    """POST -- send it now: one try, logged, answered with the posting as it stands."""

    def post(self, request, pk):
        get_object_or_404(_scoped(request), pk=pk)
        try:
            services.retry(pk)
        except services.PostingInProgress as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return self.respond(request, pk)


class SapPostingCancelView(_ActionView):
    """POST {reason} -- stop sending it. The record it would have posted is left alone."""

    def post(self, request, pk):
        get_object_or_404(_scoped(request), pk=pk)
        try:
            services.cancel(pk, request.user, request.data.get("reason"))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return self.respond(request, pk)
