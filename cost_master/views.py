from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import (
    CostTypeSerializer, CostTypeCreateSerializer, CostTypeUpdateSerializer,
    CostRateSerializer, CostRateUpsertSerializer,
)
from .permissions import CanManageCostMaster, CanViewOrManageCostMaster


def _validation_error(serializer):
    return Response({"detail": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


class CostTypeListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), CanViewOrManageCostMaster()]
        return [IsAuthenticated(), CanManageCostMaster()]

    def get(self, request):
        cost_types = services.list_cost_types(
            include_inactive=request.GET.get('include_inactive') in ('1', 'true', 'True'),
        )
        return Response(CostTypeSerializer(cost_types, many=True).data)

    def post(self, request):
        serializer = CostTypeCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            cost_type = services.create_cost_type(
                serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CostTypeSerializer(cost_type).data,
                        status=status.HTTP_201_CREATED)


class CostTypeDetailAPI(APIView):
    permission_classes = [IsAuthenticated, CanManageCostMaster]

    def patch(self, request, cost_type_id):
        serializer = CostTypeUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            cost_type = services.update_cost_type(
                cost_type_id, serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(CostTypeSerializer(cost_type).data)

    def delete(self, request, cost_type_id):
        try:
            services.delete_cost_type(cost_type_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CostRateListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), CanViewOrManageCostMaster()]
        return [IsAuthenticated(), CanManageCostMaster()]

    def get(self, request):
        def _int(name):
            raw = request.GET.get(name)
            return int(raw) if raw else None

        rates = services.list_rates(
            cost_type_id=_int('cost_type_id'),
            scope=request.GET.get('scope') or None,
            company_id=_int('company_id'),
            department_id=_int('department_id'),
            value_key=request.GET.get('value_key') or None,
            as_of=request.GET.get('as_of') or None,
            history=request.GET.get('history') in ('1', 'true', 'True'),
        )
        return Response(CostRateSerializer(rates, many=True).data)

    def post(self, request):
        serializer = CostRateUpsertSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            rate = services.upsert_rate(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CostRateSerializer(rate).data, status=status.HTTP_201_CREATED)


class CostRateDetailAPI(APIView):
    permission_classes = [IsAuthenticated, CanManageCostMaster]

    def delete(self, request, rate_id):
        try:
            services.delete_rate(rate_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
