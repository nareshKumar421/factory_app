"""Oil lot endpoints: the lots, how they move, and what is read off them.

Thin on purpose: check the right, work inside the caller's company, call one
function in ``exim.services_lot``, serialise the result. Filters take repeated
parameters (``?status=IN_TANK&status=OUT_SIDE_FACTORY``), as EXIM's did.
"""

import logging

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import services_lot
from .models_lot import ContractHistory, LotChange, LotShortage, LotStatus, OilLot, TemporaryVendor
from .permissions import Rights
from .serializers_lot import (
    ArriveSerializer,
    BulkSerializer,
    ContractHistorySerializer,
    DashboardOrderSerializer,
    DispatchSerializer,
    IntoTankSerializer,
    LotChangeSerializer,
    LotCreateSerializer,
    LotDetailSerializer,
    LotSerializer,
    LotUpdateSerializer,
    MoveSerializer,
    ShortageSerializer,
    TemporaryVendorSerializer,
)
from .views_tank import _Base, _company

logger = logging.getLogger(__name__)


def _lots(request):
    return OilLot.objects.filter(company=_company(request)).select_related("item", "created_by")


def _live_lot(request, pk):
    return get_object_or_404(_lots(request).filter(deleted=False), pk=pk)


def _filtered(request):
    params = request.query_params
    statuses = [s for s in params.getlist("status") if s]
    unknown = [s for s in statuses if s not in LotStatus.values]
    if unknown:
        raise ValidationError({"status": f"Unknown status: {', '.join(unknown)}."})
    try:
        items = [int(i) for i in params.getlist("item") if i]
    except ValueError as exc:
        raise ValidationError({"item": "Pass oil ids."}) from exc
    return services_lot.lot_filters(
        _lots(request).filter(deleted=False),
        statuses=statuses,
        vendors=[v for v in params.getlist("vendor") if v],
        items=items,
    )


def _detail(lot):
    lot = OilLot.objects.select_related("item", "created_by", "parent").get(pk=lot.pk)
    return LotDetailSerializer(lot).data


class LotListAPI(_Base):
    """GET  [?status=&vendor=&item=] : the lots in play, completed ones only when asked.
    POST : enter a lot."""

    rights = {"GET": (Rights.LOT_VIEW,), "POST": (Rights.LOT_ADD,)}

    def get(self, request):
        return Response(LotSerializer(_filtered(request), many=True).data)

    def post(self, request):
        serializer = LotCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        if data["item"].company_id != _company(request).id:
            raise ValidationError({"item": "That oil belongs to another company."})
        lot = services_lot.create_lot(company=_company(request), user=request.user, **data)
        return Response(_detail(lot), status=status.HTTP_201_CREATED)


class LotInsightsAPI(_Base):
    """GET [same filters] : count, value, quantity and average prices of those lots."""

    rights = {"GET": (Rights.LOT_VIEW,)}

    def get(self, request):
        return Response(services_lot.insights(_filtered(request)))


class LotDetailAPI(_Base):
    """GET    : a lot with its splits and its history.
    PATCH  : correct what was entered on it (not its status: see the moves).
    DELETE : remove it from every list; its history stays."""

    rights = {"GET": (Rights.LOT_VIEW,), "PATCH": (Rights.LOT_CHANGE,), "DELETE": (Rights.LOT_DELETE,)}

    def get(self, request, pk):
        return Response(_detail(get_object_or_404(_lots(request), pk=pk)))

    def patch(self, request, pk):
        lot = _live_lot(request, pk)
        serializer = LotUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        lot = services_lot.update_lot(lot, user=request.user, **serializer.validated_data)
        return Response(_detail(lot))

    def delete(self, request, pk):
        services_lot.delete_lot(_live_lot(request, pk), user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class _LotAction(_Base):
    rights = {"POST": (Rights.LOT_CHANGE,)}
    serializer_class = None

    def post(self, request, pk):
        lot = _live_lot(request, pk)
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = self.run(lot, request.user, dict(serializer.validated_data))
        return Response(_detail(result))


class LotMoveAPI(_LotAction):
    """POST : the whole lot changes status."""

    serializer_class = MoveSerializer

    def run(self, lot, user, data):
        return services_lot.move_lot(lot, user=user, **data)


class LotDispatchAPI(_LotAction):
    """POST : part of the lot leaves as a new lot. Returns the new lot."""

    serializer_class = DispatchSerializer

    def run(self, lot, user, data):
        return services_lot.dispatch_lot(lot, user=user, **data)


class LotArriveAPI(_LotAction):
    """POST : the lot reaches a refinery. Returns the lot that collects it there."""

    serializer_class = ArriveSerializer

    def run(self, lot, user, data):
        return services_lot.arrive_lot(lot, user=user, **data)


class LotIntoTankAPI(_LotAction):
    """POST : the lot is weighed into the tanks (or a warehouse)."""

    serializer_class = IntoTankSerializer

    def run(self, lot, user, data):
        return services_lot.into_tank(lot, user=user, **data)


class LotBulkAPI(_Base):
    """POST {action, lots} : arrive several at a refinery, put completed ones back
    in the tanks, or remove several. All or nothing."""

    rights = {"POST": (Rights.LOT_CHANGE, Rights.LOT_DELETE)}

    def post(self, request):
        serializer = BulkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]
        needed = Rights.LOT_DELETE if action == "delete" else Rights.LOT_CHANGE
        if not request.user.has_perm(needed):
            raise PermissionDenied("You do not have permission to do that.")
        ids = serializer.validated_data["lots"]
        lots = list(_lots(request).filter(deleted=False, pk__in=ids).order_by("id"))
        if len(lots) != len(set(ids)):
            raise ValidationError({"lots": "Some of those lots are not open lots of this company."})
        done = services_lot.bulk(lots, user=request.user, action=action)
        return Response({"action": action, "lots": done})


class LotChangeLogAPI(_Base):
    """GET [?lot=&action=&changed_by=&since=&until=&page=&page_size=] : every
    change to every lot. ``changed_by`` matches the person's name, email or the
    label a copied EXIM row carries; ``since``/``until`` are dates, inclusive."""

    rights = {"GET": (Rights.CHANGE_LOG_VIEW,)}

    def get(self, request):
        params = request.query_params
        changes = (
            LotChange.objects.filter(lot__company=_company(request))
            .select_related("changed_by")
            .prefetch_related("field_changes")
        )
        if params.get("lot"):
            if not params["lot"].isdigit():
                raise ValidationError({"lot": "Pass a lot id."})
            changes = changes.filter(lot_id=params["lot"])
        if params.get("action"):
            changes = changes.filter(action=params["action"])
        if params.get("changed_by"):
            who = params["changed_by"].strip()
            changes = changes.filter(
                Q(changed_by_label__icontains=who)
                | Q(changed_by__full_name__icontains=who)
                | Q(changed_by__email__icontains=who)
            )
        for name, lookup in (("since", "timestamp__date__gte"), ("until", "timestamp__date__lte")):
            if params.get(name):
                try:
                    day = parse_date(params[name])
                except ValueError:  # well formed, but no such day
                    day = None
                if day is None:
                    raise ValidationError({name: "Pass a date as YYYY-MM-DD."})
                changes = changes.filter(**{lookup: day})
        try:
            page = max(1, int(params.get("page", 1)))
            size = min(200, max(1, int(params.get("page_size", 50))))
        except ValueError as exc:
            raise ValidationError({"page": "Pass whole numbers."}) from exc
        count = changes.count()
        rows = changes[(page - 1) * size: page * size]
        return Response({"count": count, "page": page, "page_size": size,
                         "results": LotChangeSerializer(rows, many=True).data})


class StockDashboardAPI(_Base):
    """GET [?item=&vendor=&status=] : kilograms of each oil in each status, by vendor."""

    rights = {"GET": (Rights.LOT_VIEW,)}

    def get(self, request):
        params = request.query_params
        item = params.get("item") or None
        if item is not None and not item.isdigit():
            raise ValidationError({"item": "Pass an oil id."})
        return Response(
            services_lot.stock_dashboard(
                _company(request), item_id=item, vendor_code=params.get("vendor") or None,
                status=params.get("status") or None,
            )
        )


class StockDashboardOrderAPI(_Base):
    """PUT {items: [oil ids]} : the dashboard's row order, for everybody."""

    rights = {"PUT": (Rights.DASHBOARD_ORDER_CHANGE,)}

    def put(self, request):
        serializer = DashboardOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services_lot.reorder_dashboard(_company(request), serializer.validated_data["items"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class VehicleReportAPI(_Base):
    """GET ?status= : the lots in a status, truck by truck."""

    rights = {"GET": (Rights.VEHICLE_REPORT,)}

    def get(self, request):
        wanted = request.query_params.get("status", "")
        if wanted not in LotStatus.values:
            raise ValidationError({"status": "Pass ?status= a lot status."})
        return Response(services_lot.vehicle_report(_company(request), wanted))


class ShortageListAPI(_Base):
    """GET : every shortage recorded as a lot went into the tanks, with the totals."""

    rights = {"GET": (Rights.SHORTAGE_VIEW,)}

    def get(self, request):
        company = _company(request)
        rows = LotShortage.objects.filter(company=company).select_related("created_by")
        return Response(
            {"totals": services_lot.shortage_insights(company), "results": ShortageSerializer(rows, many=True).data}
        )


class ContractHistoryAPI(_Base):
    """GET : the contract rates EXIM recorded."""

    rights = {"GET": (Rights.CONTRACT_HISTORY_VIEW,)}

    def get(self, request):
        rows = ContractHistory.objects.filter(company=_company(request))
        return Response(ContractHistorySerializer(rows, many=True).data)


class VendorListAPI(_Base):
    """GET  : the vendors a lot can be bought from - SAP's active vendors, then
           the temporary ones. SAP not answering still returns the temporary
           ones, with ``sap_unavailable`` set.
    POST {name} : a temporary vendor, for one SAP does not have yet."""

    rights = {
        "GET": (Rights.LOT_VIEW, Rights.LOT_ADD, Rights.LOT_CHANGE),
        "POST": (Rights.TEMP_VENDOR_ADD,),
    }

    def get(self, request):
        company = _company(request)
        vendors, sap_unavailable = [], False
        try:
            vendors = [
                {"code": v.vendor_code, "name": v.vendor_name, "temporary": False}
                for v in SAPClient(company_code=company.code).get_active_vendors()
            ]
        except (SAPConnectionError, SAPDataError) as exc:
            logger.warning("exim: vendor list from SAP failed: %s", exc)
            sap_unavailable = True
        temporary = [
            {"code": v.code, "name": v.name, "temporary": True}
            for v in TemporaryVendor.objects.filter(company=company)
        ]
        return Response({"vendors": vendors + temporary, "sap_unavailable": sap_unavailable})

    def post(self, request):
        serializer = TemporaryVendorSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        vendor = services_lot.create_temporary_vendor(
            company=_company(request), user=request.user, name=serializer.validated_data["name"],
        )
        return Response({"code": vendor.code, "name": vendor.name, "temporary": True},
                        status=status.HTTP_201_CREATED)


class DirectorInventoryAPI(_Base):
    """GET : oil at every stage against the oil already packed."""

    rights = {"GET": (Rights.DIRECTOR_REPORT,)}

    def get(self, request):
        return Response(services_lot.director_inventory(_company(request)))
