import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .client import SAPClient
from .exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .serializers import POSerializer, GRPORequestSerializer, GRPOResponseSerializer, WarehouseSerializer, VendorSerializer

logger = logging.getLogger(__name__)


class OpenPOListAPI(APIView):
    """
    Returns list of open POs for a supplier
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        supplier_code = request.GET.get("supplier_code")
        if not supplier_code:
            return Response(
                {"detail": "supplier_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            client = SAPClient(company_code=request.company.company.code)
            po_list = client.get_open_pos(supplier_code)
        except SAPConnectionError as e:
            logger.error(f"SAP connection error in OpenPOListAPI: {e}")
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            logger.error(f"SAP data error in OpenPOListAPI: {e}")
            return Response(
                {"detail": "Failed to retrieve PO data from SAP."},
                status=status.HTTP_502_BAD_GATEWAY
            )

        serializer = POSerializer(po_list, many=True)
        return Response(serializer.data)


class OpenFinishedGoodsPOListAPI(APIView):
    """
    Returns open POs for a supplier, restricted to FINISHED-GOODS items
    (SAP item group 102, ``FG`` code prefix). Used by the FG gate-in flow.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        supplier_code = request.GET.get("supplier_code")
        if not supplier_code:
            return Response(
                {"detail": "supplier_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            client = SAPClient(company_code=request.company.company.code)
            po_list = client.get_open_finished_goods_pos(supplier_code)
        except SAPConnectionError as e:
            logger.error(f"SAP connection error in OpenFinishedGoodsPOListAPI: {e}")
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            logger.error(f"SAP data error in OpenFinishedGoodsPOListAPI: {e}")
            return Response(
                {"detail": "Failed to retrieve PO data from SAP."},
                status=status.HTTP_502_BAD_GATEWAY
            )

        serializer = POSerializer(po_list, many=True)
        return Response(serializer.data)


class POItemListAPI(APIView):
    """
    Returns an open PO with items for an exact PO number
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, po_number):
        if not po_number.strip():
            return Response(
                {"detail": "PO number is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            client = SAPClient(company_code=request.company.company.code)
            po = client.get_open_po_by_number(po_number.strip())
        except SAPConnectionError as e:
            logger.error(f"SAP connection error in POItemListAPI: {e}")
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            logger.error(f"SAP data error in POItemListAPI: {e}")
            return Response(
                {"detail": "Failed to retrieve PO data from SAP."},
                status=status.HTTP_502_BAD_GATEWAY
            )

        if po:
            return Response(POSerializer(po).data)

        return Response(
            {"detail": "Open PO not found"},
            status=status.HTTP_404_NOT_FOUND
        )


class CreateGRPOAPI(APIView):
    """
    Create Goods Receipt PO (Purchase Delivery Note) in SAP B1

    POST payload example:
    {
        "CardCode": "c001",
        "DocumentLines": [
            {
                "ItemCode": "c001",
                "Quantity": "100",
                "TaxCode": "T1",
                "UnitPrice": "50"
            }
        ]
    }
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request):
        # Validate request payload
        serializer = GRPORequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Get SAP client for the user's company
            client = SAPClient(company_code=request.company.company.code)

            # Create GRPO in SAP
            result = client.create_grpo(serializer.validated_data)

            # Return response
            response_serializer = GRPOResponseSerializer(data=result)
            if response_serializer.is_valid():
                return Response(response_serializer.data, status=status.HTTP_201_CREATED)
            return Response(result, status=status.HTTP_201_CREATED)

        except SAPValidationError as e:
            logger.error(f"SAP validation error in CreateGRPOAPI: {e}")
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        except SAPConnectionError as e:
            logger.error(f"SAP connection error in CreateGRPOAPI: {e}")
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            logger.error(f"SAP data error in CreateGRPOAPI: {e}")
            return Response(
                {"detail": str(e)},
                status=status.HTTP_502_BAD_GATEWAY
            )


class ActiveWarehouseListAPI(APIView):
    """
    Returns list of all active warehouses from SAP
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            client = SAPClient(company_code=request.company.company.code)
            warehouses = client.get_active_warehouses()
        except SAPConnectionError as e:
            logger.error(f"SAP connection error in ActiveWarehouseListAPI: {e}")
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            logger.error(f"SAP data error in ActiveWarehouseListAPI: {e}")
            return Response(
                {"detail": "Failed to retrieve warehouse data from SAP."},
                status=status.HTTP_502_BAD_GATEWAY
            )

        serializer = WarehouseSerializer(warehouses, many=True)
        return Response(serializer.data)


class ActiveVendorListAPI(APIView):
    """
    Returns list of all active vendors from SAP
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            client = SAPClient(company_code=request.company.company.code)
            vendors = client.get_active_vendors()
        except SAPConnectionError as e:
            logger.error(f"SAP connection error in ActiveVendorListAPI: {e}")
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            logger.error(f"SAP data error in ActiveVendorListAPI: {e}")
            return Response(
                {"detail": "Failed to retrieve vendor data from SAP."},
                status=status.HTTP_502_BAD_GATEWAY
            )

        serializer = VendorSerializer(vendors, many=True)
        return Response(serializer.data)
