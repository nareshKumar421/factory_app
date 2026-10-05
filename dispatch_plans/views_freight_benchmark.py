"""
dispatch_plans/views_freight_benchmark.py

The freight benchmark page's API: one read of the whole table, and writes for
destinations (with their rates) and slabs.

Not company-scoped. The benchmarks are what a truck from the plant costs to a
place, and Oil, Mart and Beverages all load at the same docks, so one table
serves all three and no `Company-Code` is required.
"""

from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .freight_benchmark_service import (
    FreightBenchmarkError,
    benchmark_table,
    delete_destination,
    delete_slab,
    save_destination,
    save_slab,
)
from .models_freight_benchmark import FreightDestination, FreightSlab
from .permissions import CanManageFreightBenchmarks, CanViewFreightBenchmarks
from .serializers_freight_benchmark import (
    FreightDestinationSerializer,
    FreightDestinationWriteSerializer,
    FreightSlabSerializer,
    FreightSlabWriteSerializer,
)


def _refused(error: FreightBenchmarkError) -> Response:
    return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)


def _destination_payload(destination: FreightDestination) -> dict:
    fresh = (
        FreightDestination.objects.select_related("updated_by")
        .prefetch_related("benchmarks")
        .get(pk=destination.pk)
    )
    return FreightDestinationSerializer(fresh).data


def _slab_payload(slab: FreightSlab) -> dict:
    fresh = FreightSlab.objects.annotate(destination_count=Count("benchmarks")).get(
        pk=slab.pk
    )
    return FreightSlabSerializer(fresh).data


class FreightBenchmarkTableAPI(APIView):
    """GET /api/v1/dispatch/freight-benchmarks/ -- every slab and destination."""

    permission_classes = [IsAuthenticated, CanViewFreightBenchmarks]

    def get(self, request):
        table = benchmark_table()
        return Response(
            {
                "slabs": FreightSlabSerializer(table["slabs"], many=True).data,
                "destinations": FreightDestinationSerializer(
                    table["destinations"], many=True
                ).data,
            }
        )


class FreightDestinationCreateAPI(APIView):
    """POST /api/v1/dispatch/freight-benchmarks/destinations/"""

    permission_classes = [IsAuthenticated, CanManageFreightBenchmarks]

    def post(self, request):
        serializer = FreightDestinationWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            destination = save_destination(
                data=serializer.validated_data, user=request.user
            )
        except FreightBenchmarkError as error:
            return _refused(error)
        return Response(
            _destination_payload(destination), status=status.HTTP_201_CREATED
        )


class FreightDestinationDetailAPI(APIView):
    """PUT / DELETE /api/v1/dispatch/freight-benchmarks/destinations/<id>/

    PUT replaces the destination and its whole rate list.
    """

    permission_classes = [IsAuthenticated, CanManageFreightBenchmarks]

    def put(self, request, pk):
        destination = get_object_or_404(FreightDestination, pk=pk)
        serializer = FreightDestinationWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            destination = save_destination(
                data=serializer.validated_data,
                user=request.user,
                destination=destination,
            )
        except FreightBenchmarkError as error:
            return _refused(error)
        return Response(_destination_payload(destination))

    def delete(self, request, pk):
        destination = get_object_or_404(FreightDestination, pk=pk)
        delete_destination(destination)
        return Response(status=status.HTTP_204_NO_CONTENT)


class FreightSlabCreateAPI(APIView):
    """POST /api/v1/dispatch/freight-benchmarks/slabs/"""

    permission_classes = [IsAuthenticated, CanManageFreightBenchmarks]

    def post(self, request):
        serializer = FreightSlabWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            slab = save_slab(data=serializer.validated_data)
        except FreightBenchmarkError as error:
            return _refused(error)
        return Response(_slab_payload(slab), status=status.HTTP_201_CREATED)


class FreightSlabDetailAPI(APIView):
    """PUT / DELETE /api/v1/dispatch/freight-benchmarks/slabs/<id>/"""

    permission_classes = [IsAuthenticated, CanManageFreightBenchmarks]

    def put(self, request, pk):
        slab = get_object_or_404(FreightSlab, pk=pk)
        serializer = FreightSlabWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            slab = save_slab(data=serializer.validated_data, slab=slab)
        except FreightBenchmarkError as error:
            return _refused(error)
        return Response(_slab_payload(slab))

    def delete(self, request, pk):
        slab = get_object_or_404(FreightSlab, pk=pk)
        try:
            delete_slab(slab)
        except FreightBenchmarkError as error:
            return _refused(error)
        return Response(status=status.HTTP_204_NO_CONTENT)
