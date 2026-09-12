"""
stock_dashboard/views.py

API views for the Stock Dashboard.

All endpoints are read-only and require:
  - JWT authentication (Authorization: Bearer <token>)
  - Company context header (Company-Code: <company_code>)
  - CanViewStockDashboard permission
"""

import logging

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .models import LogisticsBoardSettings, WarehouseBoardSettings
from .permissions import CanViewStockDashboard
from .serializers import (
    ItemBatchFilterSerializer,
    ItemBatchResponseSerializer,
    ItemDetailFilterSerializer,
    ItemDetailResponseSerializer,
    StockDashboardAsOfFilterSerializer,
    StockDashboardExportFilterSerializer,
    StockDashboardFilterSerializer,
    LogisticsBoardSettingsSerializer,
    StockDashboardResponseSerializer,
    WarehouseBoardSettingsSerializer,
    WarehouseOccupancyFilterSerializer,
    WarehouseOccupancyResponseSerializer,
)
from .services import StockDashboardService

logger = logging.getLogger(__name__)


class StockDashboardAPI(APIView):
    """
    Stock level dashboard showing items against benchmark levels.

    Returns one row per item-warehouse or grouped item with current on-hand
    qty, health ratio, stock status, and movement status.

    GET /api/v1/dashboards/stock/

    Query parameters:
        warehouse - comma-separated warehouse codes
        item_group - SAP item group name
        status - comma-separated healthy, low, critical, unset
        movement_status - comma-separated recent, slow
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def get(self, request):
        filter_serializer = StockDashboardFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = StockDashboardService(company_code=request.company.company.code)

        try:
            result = service.get_stock_levels(filters)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(StockDashboardResponseSerializer(result).data)


class StockDashboardAsOfAPI(APIView):
    """
    Experimental SAP reconstruction endpoint for Stock Benchmark.

    GET /api/v1/dashboards/stock/as-of/?as_of_date=YYYY-MM-DD
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def get(self, request):
        filter_serializer = StockDashboardAsOfFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = StockDashboardService(company_code=request.company.company.code)

        try:
            result = service.get_as_of_stock_levels(filters)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(StockDashboardResponseSerializer(result).data)


class StockDashboardExportAPI(APIView):
    """
    Excel export of the Stock Benchmark table.

    Accepts the same filters as the table (search, warehouse, item_group,
    status, movement_status, sort_by, sort_dir, optional as_of_date) and
    returns ALL matching rows as an .xlsx attachment (not just one page).

    GET /api/v1/dashboards/stock/export/
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    MOVEMENT_LABELS = {"recent": "Recently Used", "slow": "Slow Moving"}
    STATUS_LABELS = {
        "healthy": "Healthy",
        "low": "Low",
        "critical": "Critical",
        "unset": "Unset",
        "none": "",
    }

    def get(self, request):
        filter_serializer = StockDashboardExportFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = StockDashboardService(company_code=request.company.company.code)

        try:
            rows = service.get_stock_levels_for_export(filters)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return self._build_workbook_response(rows, filters)

    def _build_workbook_response(self, rows, filters):
        import openpyxl
        from openpyxl.styles import Font

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Stock Benchmark"

        headers = [
            "Item Code",
            "Item Name",
            "Warehouse",
            "On Hand",
            "Benchmark",
            "Difference",
            "UOM",
            "Health %",
            "Status",
            "Movement",
            "Last Used",
            "Days Since Use",
        ]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        for row in rows:
            on_hand = row.get("on_hand", 0) or 0
            min_stock = row.get("min_stock", 0) or 0
            ws.append([
                row.get("item_code", ""),
                row.get("item_name", ""),
                row.get("warehouse", ""),
                on_hand,
                min_stock,
                on_hand - min_stock,
                row.get("uom", ""),
                round(row.get("health_ratio", 0) * 100),
                self.STATUS_LABELS.get(row.get("stock_status", ""), row.get("stock_status", "")),
                self.MOVEMENT_LABELS.get(
                    row.get("movement_status", ""), row.get("movement_status", "")
                ),
                row.get("last_consumption_date") or "",
                row.get("days_since_last_consumption"),
            ])

        for column_cells in ws.columns:
            width = max((len(str(c.value)) for c in column_cells if c.value is not None), default=10)
            ws.column_dimensions[column_cells[0].column_letter].width = min(width + 2, 50)

        as_of_date = filters.get("as_of_date")
        stamp = as_of_date.isoformat() if as_of_date else timezone.localdate().isoformat()
        filename = f"stock_benchmark_{stamp}.xlsx"

        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        wb.save(response)
        return response


class StockItemDetailAPI(APIView):
    """
    Per-warehouse breakdown for a single item (used by row expand).

    GET /api/v1/dashboards/stock/<item_code>/warehouses/?warehouse=WH-01,WH-02
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def get(self, request, item_code: str):
        filter_serializer = ItemDetailFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        warehouses = filter_serializer.validated_data["warehouse"]
        service = StockDashboardService(company_code=request.company.company.code)

        try:
            result = service.get_item_detail(item_code, warehouses)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(ItemDetailResponseSerializer(result).data)


class WarehouseOccupancyAPI(APIView):
    """
    One warehouse's stock with the SAP pack fields needed to count pallets.

    Feeds the Production Control board, which has to express BH-PF's contents in
    pallets against a floor capacity. No pallet arithmetic happens here -- the
    pieces-per-pallet figures are board policy rather than SAP fact, so they live
    in the frontend where they are named and unit-tested. This endpoint hands
    over `pieces_per_box` (OITM.SalFactor2), `litres_per_piece`
    (OITM.SalPackUn) and `gross_weight_per_case` (OITM.U_Gross_Weight) and lets
    the caller convert.

    The weight field is what lets a board report a warehouse in tonnes, and it
    comes with two obligations the caller cannot skip: apply it only where `uom`
    is a piece unit, and read `meta.unweighed_items` / `meta.non_piece_items`
    before trusting the total. Both are counts of stock the tonnage cannot see.

    GET /api/v1/dashboards/stock/occupancy/?warehouse=BH-PF

    Query parameters:
        warehouse - one SAP warehouse code (required)
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def get(self, request):
        filter_serializer = WarehouseOccupancyFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = StockDashboardService(company_code=request.company.company.code)

        try:
            result = service.get_warehouse_occupancy(
                filter_serializer.validated_data["warehouse"],
                item_groups=filter_serializer.validated_data.get("item_groups") or None,
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(WarehouseOccupancyResponseSerializer(result).data)


class ItemBatchAPI(APIView):
    """
    One item's batches in one warehouse, with manufacturing and expiry dates.

    The drill-down behind a non-moving or occupancy row: how old this stock is,
    which batches it sits in, and when they expire.

    GET /api/v1/dashboards/stock/<item_code>/batches/?warehouse=BH-PF
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def get(self, request, item_code):
        filter_serializer = ItemBatchFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = StockDashboardService(company_code=request.company.company.code)

        try:
            result = service.get_item_batches(
                item_code, filter_serializer.validated_data["warehouse"]
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(ItemBatchResponseSerializer(result).data)


class WarehouseBoardSettingsAPI(APIView):
    """The two facts about a warehouse that SAP does not hold.

    GET  /api/v1/dashboards/stock/warehouse-settings/?warehouse=BH-BT
    PUT  /api/v1/dashboards/stock/warehouse-settings/?warehouse=BH-BT

    Rated tonnage capacity and the date stock was last physically verified.
    Neither exists in SAP or WMS -- see `WarehouseBoardSettings` for why the
    movement log cannot stand in for the audit date -- so both are typed in by
    an operator and stored per (company, warehouse).

    GET creates the row on first read and answers nulls, so the board renders
    "not configured" rather than 404ing on a warehouse nobody has set up.

    Reading is allowed to anyone who can read the stock dashboard, since the
    board already shows both figures. Writing is gated on the same right: the
    capacity is a property of the building, not a financial control, and a
    separate permission would have to be created on the live database and
    granted before anybody could use this screen.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def _warehouse(self, request):
        return (request.query_params.get("warehouse") or "").strip().upper()

    def _row(self, request, warehouse):
        row, _created = WarehouseBoardSettings.objects.get_or_create(
            company_code=request.company.company.code,
            warehouse=warehouse,
        )
        return row

    def get(self, request):
        warehouse = self._warehouse(request)
        if not warehouse:
            return Response(
                {"detail": "`warehouse` is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        row = self._row(request, warehouse)
        return Response(WarehouseBoardSettingsSerializer(row).data)

    def put(self, request):
        warehouse = self._warehouse(request)
        if not warehouse:
            return Response(
                {"detail": "`warehouse` is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        row = self._row(request, warehouse)
        serializer = WarehouseBoardSettingsSerializer(row, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid settings.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer.save(updated_by=request.user)
        return Response(serializer.data)


class LogisticsBoardSettingsAPI(APIView):
    """Company-level figures the operations board cannot derive.

    GET  /api/v1/dashboards/stock/board-settings/
    PUT  /api/v1/dashboards/stock/board-settings/

    Owned vehicle count and the per-section employee headcount and salary. See
    `LogisticsBoardSettings` for why none of it is derivable -- ownership is not
    a field on the vehicle master, and the two department masters this system
    runs on do not carry the board's section names.

    GET creates the row on first read and answers nulls, so the board renders
    "not configured" rather than 404ing on a company nobody has set up.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def _row(self, request):
        row, _created = LogisticsBoardSettings.objects.get_or_create(
            company_code=request.company.company.code,
        )
        return row

    def get(self, request):
        return Response(LogisticsBoardSettingsSerializer(self._row(request)).data)

    def put(self, request):
        row = self._row(request)
        serializer = LogisticsBoardSettingsSerializer(row, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid settings.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer.save(updated_by=request.user)
        return Response(serializer.data)


class OwnedVehicleStatusAPI(APIView):
    """The company's own trucks, and what each one is doing today.

    GET /api/v1/dashboards/stock/owned-vehicles/

    Ownership is not a field on the vehicle master, so the fleet is the
    registration list typed on the board settings screen. Each plate is then
    resolved against three sources, highest priority first:

      - ``OUT_OF_SERVICE``  listed as off the road. Nothing records this -- a
                            damaged truck looks exactly like an idle one -- so
                            it is configured, and it wins over any activity: a
                            transfer still open against a truck in the workshop
                            is paperwork, not work.
      - ``ON_BST``          carrying an open branch stock transfer. This is the
                            correction that matters: these trucks run godown to
                            godown and never touch the factory gate, so gate
                            data alone reported a working fleet as free.
      - ``ON_DISPATCH``     a sales dispatch: either a plan with this truck
                            assigned that has not gone yet, or a gate-out today.
      - ``AT_PLANT``        a gate arrival today, still INSIDE or LOADING.
      - ``OUT``             a gate arrival today that has DEPARTED.
      - ``FREE``            none of the above.

    BST outranks dispatch only because a truck mid-transfer cannot also be
    loading a sales bill; where both somehow appear, the transfer is the one
    holding the vehicle.

    Only ``IN_TRANSIT`` and ``SCANNING`` count as the truck being engaged.
    ``RECEIVING``, ``PARTIALLY_RECEIVED`` and ``RECEIVED`` are destination-side
    work on a load the truck may have carried days earlier, and counting them
    would show a parked truck as busy.

    "Free" still means only "no activity these registers can see" -- a truck on
    a run that never enters either system reads as free.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    #: BST statuses where the truck itself is still committed to the load.
    ENGAGED_BST_STATUSES = ("IN_TRANSIT", "SCANNING")

    def get(self, request):
        from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
        from gate_core.models.sales_dispatch import SalesDispatchGateOut
        from gate_core.models.vehicle_arrival import VehicleArrival, VehicleArrivalStatus
        from warehouse.models_bst import BSTTransfer

        settings_row, _created = LogisticsBoardSettings.objects.get_or_create(
            company_code=request.company.company.code,
        )
        plates = settings_row.owned_vehicle_list
        off_road = set(settings_row.out_of_service_list)

        if not plates:
            return Response(
                {
                    "configured": False,
                    "vehicles": [],
                    "summary": {
                        "owned": 0,
                        "on_bst": 0,
                        "on_dispatch": 0,
                        "at_plant": 0,
                        "out": 0,
                        "free": 0,
                        "out_of_service": 0,
                    },
                }
            )

        today = timezone.localdate()

        def normalise(text):
            return (text or "").strip().upper().replace(" ", "").replace("-", "")

        # Trucks carrying an open transfer. Not company-filtered: a transfer
        # moves between godowns that may sit in different companies, and the
        # fleet list is what scopes this read.
        in_transit = {
            normalise(getattr(row.vehicle, "vehicle_number", ""))
            for row in BSTTransfer.objects.filter(
                status__in=self.ENGAGED_BST_STATUSES
            ).select_related("vehicle")
        }

        # On a sales dispatch: a plan with this truck assigned that has not gone
        # yet, or one that cleared the gate today.
        on_dispatch = {
            normalise(getattr(row.vehicle, "vehicle_number", ""))
            for row in DispatchPlan.objects.filter(is_active=True, vehicle__isnull=False)
            .exclude(booking_status=DispatchPlanStatus.DISPATCHED)
            .select_related("vehicle")
        }
        on_dispatch |= {
            normalise(
                getattr(getattr(row.arrival, "vehicle", None), "vehicle_number", "")
            )
            for row in SalesDispatchGateOut.objects.filter(
                gate_out_date=today
            ).select_related("arrival__vehicle")
        }
        on_dispatch.discard("")

        # Arrivals are NOT company-scoped either -- one truck can carry bills
        # for several companies.
        gate_state = {}
        for arrival in (
            VehicleArrival.objects.filter(gate_in_date=today)
            .exclude(status=VehicleArrivalStatus.CANCELLED)
            .select_related("vehicle")
        ):
            plate = normalise(getattr(arrival.vehicle, "vehicle_number", ""))
            if not plate:
                continue
            # A truck can arrive twice in a day. Being on site now outranks a
            # completed trip, so AT_PLANT never loses to a later DEPARTED row.
            at_plant = arrival.status in (
                VehicleArrivalStatus.INSIDE,
                VehicleArrivalStatus.LOADING,
            )
            if at_plant or gate_state.get(plate) != "AT_PLANT":
                gate_state[plate] = "AT_PLANT" if at_plant else "OUT"

        def state_for(plate):
            if plate in off_road:
                return "OUT_OF_SERVICE"
            if plate in in_transit:
                return "ON_BST"
            if plate in on_dispatch:
                return "ON_DISPATCH"
            return gate_state.get(plate, "FREE")

        vehicles = [{"vehicle_no": plate, "state": state_for(plate)} for plate in plates]

        def count(state):
            return sum(1 for vehicle in vehicles if vehicle["state"] == state)

        return Response(
            {
                "configured": True,
                "vehicles": vehicles,
                "summary": {
                    "owned": len(vehicles),
                    "on_bst": count("ON_BST"),
                    "on_dispatch": count("ON_DISPATCH"),
                    "at_plant": count("AT_PLANT"),
                    "out": count("OUT"),
                    "free": count("FREE"),
                    "out_of_service": count("OUT_OF_SERVICE"),
                },
            }
        )


class StockInTransitAPI(APIView):
    """Stock dispatched to a sister company that SAP has not booked in, by age.

    GET /api/v1/dashboards/stock/stock-in-transit/

    A load is in transit when SAP says it left and does not yet say it arrived.
    The sending company raises an A/R invoice; the receiving company answers it
    with a Goods Receipt PO carrying the invoice number in ``NumAtCard``. No
    receipt, still on the road.

    This replaced the branch-transfer register's ``IN_TRANSIT`` status, which
    drifts: on 12 September 2026 the register held 16 transfers as in transit
    and SAP had already received 14 of them. The status is set by hand at this
    end and nobody closes it when the far end books the stock, so it only ever
    over-reports. SAP's receipt is the fact.

    Both legs are read regardless of which company the caller is pinned to, for
    the same reason the dispatch tiles sum Oil and Mart: the board reports one
    plant's traffic, not one ledger's.

    Reported in tonnes only, on the board's usual weight chain -- the line's own
    ``Weight1`` where SAP recorded one, otherwise gross case weight over the
    pack factor. Lines that chain cannot weigh are counted in
    ``unweighed_lines``, because a tonnage over a partly-weighed set is a floor
    and has to say so. When SAP cannot be reached the endpoint answers
    ``weights_available: false`` and the board says so rather than showing a
    figure it could not compute.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    #: The intercompany legs this plant ships on, as
    #: ``(sending company, customer codes, receiving company)``.
    #:
    #: Only the sister-company customer belongs here. The wider intercompany
    #: list covers every group ledger, and matching on it would look for a Mart
    #: receipt against invoices that were never sent to Mart.
    ROUTES = (
        ("JIVO_OIL", ("CUSTA000606",), "JIVO_MART"),
    )

    #: Floors that actually ship stock.
    #:
    #: Not cosmetic. Without it the read picks up rate-difference debit notes
    #: raised against other plants' warehouses -- three in August 2026 came to
    #: 533 tonnes between them, four times the genuine figure, and being purely
    #: financial documents they can never be received.
    SHIPPING_WAREHOUSES = ("BH-PF", "BH-BT")

    #: How far back to look for an unmatched invoice.
    #:
    #: A receipt nobody ever keyed in leaves its invoice unmatched forever, so
    #: an unbounded read would accumulate every clerical miss since go-live and
    #: report it as traffic on the road.
    LOOKBACK_DAYS = 60

    FRESH_DAYS = 3
    AGEING_DAYS = 7

    def get(self, request):
        bands = self._empty_bands()
        unweighed = 0

        # A HANA outage costs the whole tile, and the board reports it as
        # unavailable rather than inventing a figure. `Exception` deliberately:
        # the driver raises its own connection errors that do not always arrive
        # wrapped as SAPConnectionError, and an unreachable SAP must degrade
        # this tile rather than 500 the request.
        try:
            dispatches = self._read_routes()
        except Exception:  # noqa: BLE001
            logger.warning("stock-in-transit: SAP unavailable", exc_info=True)
            return Response(
                {
                    "bands": bands,
                    "totals": {"loads": 0, "tonnes": 0},
                    "loads": [],
                    "unweighed_lines": 0,
                    "weights_available": False,
                }
            )

        loads = []
        for row in dispatches:
            band_key = self._band_for(row["days_out"])
            band = bands[band_key]
            band["loads"] += 1
            band["tonnes"] += row["kilograms"] / 1000
            unweighed += row["unweighed_lines"]
            loads.append(
                {
                    "doc_num": row["doc_num"],
                    "doc_date": row["doc_date"],
                    "days_out": row["days_out"],
                    "tonnes": round(row["kilograms"] / 1000, 3),
                    "band": band_key,
                    "unweighed_lines": row["unweighed_lines"],
                }
            )

        for band in bands.values():
            band["tonnes"] = round(band["tonnes"], 2)

        return Response(
            {
                "bands": bands,
                "totals": {
                    "loads": sum(b["loads"] for b in bands.values()),
                    "tonnes": round(sum(b["tonnes"] for b in bands.values()), 2),
                },
                # The loads themselves, newest first. Small by nature -- a week
                # of traffic is a couple of dozen rows -- and the board's
                # drill-down needs the documents, not another round trip to
                # recompute the same query.
                "loads": loads,
                "unweighed_lines": unweighed,
                "weights_available": True,
            }
        )

    def _read_routes(self):
        """Every unreceived dispatch across the configured legs."""
        rows = []
        for sender, customers, receiver in self.ROUTES:
            sending = StockDashboardService(company_code=sender)
            receiving = StockDashboardService(company_code=receiver)
            rows.extend(
                sending.reader.get_unreceived_intercompany_dispatches(
                    receiving_schema=receiving.reader.connection.schema,
                    customer_codes=customers,
                    warehouses=self.SHIPPING_WAREHOUSES,
                    lookback_days=self.LOOKBACK_DAYS,
                )
            )
        return rows

    def _band_for(self, days_out: int) -> str:
        if days_out <= self.FRESH_DAYS:
            return "fresh"
        if days_out <= self.AGEING_DAYS:
            return "ageing"
        return "stale"

    def _empty_bands(self):
        return {
            "fresh": {"label": "Up to 3 days", "loads": 0, "tonnes": 0},
            "ageing": {"label": "4 - 7 days", "loads": 0, "tonnes": 0},
            "stale": {"label": "Over 7 days", "loads": 0, "tonnes": 0},
        }
