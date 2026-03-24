from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .services import OutboundService, SAPSyncService
from .serializers import (
    ShipmentOrderListSerializer, ShipmentOrderDetailSerializer,
    PickTaskSerializer, OutboundLoadRecordSerializer,
    GoodsIssuePostingSerializer,
    AssignBaySerializer, PickTaskUpdateSerializer, ScanSerializer,
    LinkVehicleSerializer, InspectTrailerSerializer,
    LoadSerializer, DispatchSerializer, SyncFiltersSerializer,
)
from .permissions import (
    CanViewShipments, CanSyncShipments, CanAssignDockBay,
    CanExecutePickTask, CanInspectTrailer, CanLoadTruck,
    CanConfirmLoad, CanDispatchShipment, CanPostGoodsIssue,
    CanViewDashboard,
)


class ShipmentListAPI(APIView):
    """GET /api/v1/outbound/shipments/ - List shipment orders"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewShipments]

    def get(self, request):
        service = OutboundService(company_code=request.company.company.code)
        filters = {
            "status": request.GET.get("status"),
            "scheduled_date": request.GET.get("scheduled_date"),
            "customer_code": request.GET.get("customer_code"),
        }
        shipments = service.get_shipments(filters)
        serializer = ShipmentOrderListSerializer(shipments, many=True)
        return Response(serializer.data)


class ShipmentDetailAPI(APIView):
    """GET /api/v1/outbound/shipments/<id>/ - Get shipment detail"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewShipments]

    def get(self, request, shipment_id):
        service = OutboundService(company_code=request.company.company.code)
        try:
            shipment = service.get_shipment_detail(shipment_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        serializer = ShipmentOrderDetailSerializer(shipment)
        return Response(serializer.data)


class ShipmentSyncAPI(APIView):
    """POST /api/v1/outbound/shipments/sync/ - Sync Sales Orders from SAP"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSyncShipments]

    def post(self, request):
        filter_serializer = SyncFiltersSerializer(data=request.data)
        if not filter_serializer.is_valid():
            return Response(filter_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        sync_service = SAPSyncService(company_code=request.company.company.code)
        try:
            result = sync_service.sync_sales_orders(
                user=request.user,
                filters=filter_serializer.validated_data or None,
            )
            return Response(result)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY
            )


class AssignBayAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/assign-bay/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanAssignDockBay]

    def post(self, request, shipment_id):
        serializer = AssignBaySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        service = OutboundService(company_code=request.company.company.code)
        try:
            shipment = service.assign_dock_bay(
                shipment_id,
                dock_bay=serializer.validated_data["dock_bay"],
                slot_start=serializer.validated_data.get("dock_slot_start"),
                slot_end=serializer.validated_data.get("dock_slot_end"),
            )
            return Response(ShipmentOrderDetailSerializer(shipment).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PickTaskListAPI(APIView):
    """GET /api/v1/outbound/shipments/<id>/pick-tasks/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanExecutePickTask]

    def get(self, request, shipment_id):
        from .models import PickTask
        tasks = PickTask.objects.filter(
            shipment_item__shipment_order_id=shipment_id,
            shipment_item__shipment_order__company__code=request.company.company.code,
        ).select_related("shipment_item", "assigned_to")
        serializer = PickTaskSerializer(tasks, many=True)
        return Response(serializer.data)


class GeneratePicksAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/generate-picks/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewShipments]

    def post(self, request, shipment_id):
        service = OutboundService(company_code=request.company.company.code)
        try:
            tasks = service.generate_pick_tasks(shipment_id)
            return Response(
                PickTaskSerializer(tasks, many=True).data,
                status=status.HTTP_201_CREATED
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PickTaskUpdateAPI(APIView):
    """PATCH /api/v1/outbound/pick-tasks/<id>/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanExecutePickTask]

    def patch(self, request, task_id):
        serializer = PickTaskUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        service = OutboundService(company_code=request.company.company.code)
        try:
            task = service.update_pick_task(
                task_id, serializer.validated_data, user=request.user
            )
            return Response(PickTaskSerializer(task).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PickTaskScanAPI(APIView):
    """POST /api/v1/outbound/pick-tasks/<id>/scan/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanExecutePickTask]

    def post(self, request, task_id):
        serializer = ScanSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        service = OutboundService(company_code=request.company.company.code)
        try:
            task = service.record_scan(task_id, serializer.validated_data["barcode"])
            return Response(PickTaskSerializer(task).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ConfirmPackAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/confirm-pack/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanExecutePickTask]

    def post(self, request, shipment_id):
        service = OutboundService(company_code=request.company.company.code)
        try:
            shipment = service.confirm_pack(shipment_id)
            return Response(ShipmentOrderDetailSerializer(shipment).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class StageShipmentAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/stage/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanExecutePickTask]

    def post(self, request, shipment_id):
        service = OutboundService(company_code=request.company.company.code)
        try:
            shipment = service.stage_shipment(shipment_id)
            return Response(ShipmentOrderDetailSerializer(shipment).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class LinkVehicleAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/link-vehicle/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewShipments]

    def post(self, request, shipment_id):
        serializer = LinkVehicleSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        service = OutboundService(company_code=request.company.company.code)
        try:
            shipment = service.link_vehicle(
                shipment_id, serializer.validated_data["vehicle_entry_id"]
            )
            return Response(ShipmentOrderDetailSerializer(shipment).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class InspectTrailerAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/inspect-trailer/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanInspectTrailer]

    def post(self, request, shipment_id):
        serializer = InspectTrailerSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        service = OutboundService(company_code=request.company.company.code)
        try:
            record = service.inspect_trailer(
                shipment_id, serializer.validated_data, user=request.user
            )
            return Response(OutboundLoadRecordSerializer(record).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class LoadShipmentAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/load/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanLoadTruck]

    def post(self, request, shipment_id):
        serializer = LoadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        service = OutboundService(company_code=request.company.company.code)
        try:
            shipment = service.record_loading(
                shipment_id,
                serializer.validated_data["items"],
                user=request.user,
            )
            return Response(ShipmentOrderDetailSerializer(shipment).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class SupervisorConfirmAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/supervisor-confirm/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanConfirmLoad]

    def post(self, request, shipment_id):
        service = OutboundService(company_code=request.company.company.code)
        try:
            record = service.supervisor_confirm(shipment_id, user=request.user)
            return Response(OutboundLoadRecordSerializer(record).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class GenerateBOLAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/generate-bol/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanConfirmLoad]

    def post(self, request, shipment_id):
        service = OutboundService(company_code=request.company.company.code)
        try:
            bol_data = service.generate_bol(shipment_id)
            return Response(bol_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class DispatchShipmentAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/dispatch/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanDispatchShipment]

    def post(self, request, shipment_id):
        serializer = DispatchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        service = OutboundService(company_code=request.company.company.code)
        try:
            shipment = service.dispatch(
                shipment_id,
                seal_number=serializer.validated_data["seal_number"],
                user=request.user,
                branch_id=serializer.validated_data.get("branch_id"),
            )
            return Response(ShipmentOrderDetailSerializer(shipment).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPValidationError as e:
            return Response(
                {"detail": f"SAP validation error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY
            )


class GoodsIssueStatusAPI(APIView):
    """GET /api/v1/outbound/shipments/<id>/goods-issue/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewShipments]

    def get(self, request, shipment_id):
        from .models import GoodsIssuePosting
        try:
            posting = GoodsIssuePosting.objects.get(
                shipment_order_id=shipment_id,
                shipment_order__company__code=request.company.company.code,
            )
            return Response(GoodsIssuePostingSerializer(posting).data)
        except GoodsIssuePosting.DoesNotExist:
            return Response(
                {"detail": "No Goods Issue posting found"},
                status=status.HTTP_404_NOT_FOUND
            )


class GoodsIssueRetryAPI(APIView):
    """POST /api/v1/outbound/shipments/<id>/goods-issue/retry/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostGoodsIssue]

    def post(self, request, shipment_id):
        service = OutboundService(company_code=request.company.company.code)
        try:
            posting = service.retry_goods_issue(
                shipment_id,
                user=request.user,
                branch_id=request.data.get("branch_id"),
            )
            return Response(GoodsIssuePostingSerializer(posting).data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except (SAPValidationError, SAPConnectionError, SAPDataError) as e:
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY
            )


class OutboundDashboardAPI(APIView):
    """GET /api/v1/outbound/dashboard/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDashboard]

    def get(self, request):
        service = OutboundService(company_code=request.company.company.code)
        dashboard = service.get_dashboard()
        return Response(dashboard)
