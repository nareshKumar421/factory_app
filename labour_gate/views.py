import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from accounts.models import Department
from person_gatein.models import Contractor

from .models import LabourGateEntry, LabourGateOutBatch
from .serializers import (
    LabourGateEntrySerializer,
    LabourInSerializer,
    UpdateInSerializer,
    OutBatchSerializer,
)
from .permissions import CanViewLabourGate, CanRecordLabourIn, CanRecordLabourOut

logger = logging.getLogger(__name__)


def _entry_for_company(pk, request):
    # No prefetch here: these views mutate out_batches and then serialize the
    # result, so the count_out/remaining must be read fresh (a prefetched cache
    # would be stale after add/undo). The list view does its own prefetch.
    return get_object_or_404(
        LabourGateEntry, id=pk, company=request.company.company
    )


class LabourGateDayAPI(APIView):
    """
    GET ?date=YYYY-MM-DD : every contractor's labour in/out tally for the day.
    Feeds both the Labour In and Labour Out screens.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewLabourGate]

    def get(self, request):
        company = request.company.company
        work_date = parse_date(request.query_params.get("date", "") or "")
        if not work_date:
            return Response(
                {"detail": "A valid ?date=YYYY-MM-DD is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = (
            LabourGateEntry.objects.filter(company=company, work_date=work_date)
            .select_related("contractor")
            .prefetch_related("out_batches")
        )
        return Response(LabourGateEntrySerializer(qs, many=True).data)


class LabourInAPI(APIView):
    """POST {department, contractor, work_date, count_in} : record/update how many
    labourers a contractor brought into a department on a day (one row per
    department+contractor+day)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourIn]

    @transaction.atomic
    def post(self, request):
        serializer = LabourInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        company = request.company.company
        department = get_object_or_404(Department, id=data["department"])
        contractor = get_object_or_404(
            Contractor, id=data["contractor"], is_active=True
        )

        entry, created = LabourGateEntry.objects.get_or_create(
            company=company,
            department=department,
            contractor=contractor,
            work_date=data["work_date"],
            defaults={"count_in": data["count_in"], "created_by": request.user},
        )
        if not created:
            if data["count_in"] < entry.total_out:
                return Response(
                    {"detail": f"Labour in cannot be less than the {entry.total_out} already marked out."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            entry.count_in = data["count_in"]
            entry.updated_by = request.user
            entry.save(update_fields=["count_in", "updated_at", "updated_by"])

        logger.info(
            f"Labour in {'created' if created else 'updated'}: contractor={contractor.id} "
            f"date={data['work_date']} count_in={data['count_in']} by {request.user}"
        )
        return Response(
            LabourGateEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class LabourEntryDetailAPI(APIView):
    """PATCH {count_in} edit, DELETE remove a labour-in entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourIn]

    def patch(self, request, pk):
        entry = _entry_for_company(pk, request)
        serializer = UpdateInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        count_in = serializer.validated_data["count_in"]

        if count_in < entry.total_out:
            return Response(
                {"detail": f"Labour in cannot be less than the {entry.total_out} already marked out."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        entry.count_in = count_in
        entry.updated_by = request.user
        entry.save(update_fields=["count_in", "updated_at", "updated_by"])
        return Response(LabourGateEntrySerializer(entry).data)

    def delete(self, request, pk):
        entry = _entry_for_company(pk, request)
        if entry.out_batches.exists():
            return Response(
                {"detail": "Cannot delete: labour has already been marked out for this contractor."},
                status=status.HTTP_409_CONFLICT,
            )
        entry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LabourOutAPI(APIView):
    """POST {count} : mark one batch of people OUT against an entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourOut]

    @transaction.atomic
    def post(self, request, pk):
        entry = _entry_for_company(pk, request)
        serializer = OutBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        count = serializer.validated_data["count"]

        if count > entry.remaining:
            return Response(
                {"detail": f"Only {entry.remaining} labour remaining inside for this contractor."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        LabourGateOutBatch.objects.create(entry=entry, count=count, created_by=request.user)
        entry.updated_by = request.user
        entry.save(update_fields=["updated_at", "updated_by"])
        logger.info(
            f"Labour out: entry={entry.id} +{count} (remaining {entry.remaining}) by {request.user}"
        )
        return Response(LabourGateEntrySerializer(entry).data)


class LabourOutUndoAPI(APIView):
    """POST : remove the most recent out batch for an entry (fix a mis-count)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourOut]

    @transaction.atomic
    def post(self, request, pk):
        entry = _entry_for_company(pk, request)
        last = entry.out_batches.order_by("-created_at", "-id").first()
        if not last:
            return Response(
                {"detail": "No out batches to undo."}, status=status.HTTP_400_BAD_REQUEST
            )
        last.delete()
        entry.updated_by = request.user
        entry.save(update_fields=["updated_at", "updated_by"])
        return Response(LabourGateEntrySerializer(entry).data)
