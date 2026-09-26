from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanManageProductionSettings, CanViewProductionSettings
from .serializers_settings import (
    ProductionSettingsSerializer,
    ProductionSettingsUpdateSerializer,
)
from .services import settings_service


class ProductionSettingsAPI(APIView):
    """GET / PATCH the active company's production settings."""

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionSettings()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageProductionSettings()]

    def get(self, request):
        row = settings_service.get_settings(request.company.company)
        return Response(ProductionSettingsSerializer(row).data)

    def patch(self, request):
        serializer = ProductionSettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            row = settings_service.update_settings(
                request.company.company, serializer.validated_data, request.user,
            )
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except (SAPConnectionError, SAPDataError):
            return Response(
                {'detail': "Could not check the warehouses against SAP. Try again "
                           "once SAP is reachable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(ProductionSettingsSerializer(row).data)

    put = patch
