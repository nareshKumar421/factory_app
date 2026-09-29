"""Tank farm endpoints: the oils, the tanks, their read-outs and the tank log.

Thin on purpose: check the right, work inside the caller's company, call one
function in ``exim.services_tank``, serialise the result.
"""

import logging

from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import services_lot, services_tank
from .models_tank import Tank, TankItem, TankLog
from .permissions import Rights, any_of
from .serializers_lot import LotSerializer
from .serializers_tank import (
    OilSerializer,
    OilWriteSerializer,
    OpeningStockSerializer,
    TankCreateSerializer,
    TankLogSerializer,
    TankSerializer,
    TankUpdateSerializer,
)

logger = logging.getLogger(__name__)

#: Who an opening stock is bought "from": the company's own Delhi branch, the
#: vendor EXIM hard-coded for it.
OPENING_STOCK_VENDOR = ("VENDA000004", "JIVO WELLNESS PVT LTD - DL")


def _company(request):
    return request.company.company


def _own(request, instance, field="company_id"):
    """A referenced row (an oil, say) must be the caller's company's."""
    if instance is not None and getattr(instance, field) != _company(request).id:
        raise ValidationError({"item": "That oil belongs to another company."})
    return instance


class _Base(APIView):
    #: Rights per method; any one of a method's rights is enough.
    rights = {}

    def get_permissions(self):
        needed = self.rights.get(self.request.method, ())
        return [IsAuthenticated(), HasCompanyContext(), any_of(*needed)()]


# ---------------------------------------------------------------------------
# Oils
# ---------------------------------------------------------------------------

class OilListAPI(_Base):
    """GET  : the oils. Lot and tank screens pick from this list too.
    POST : add an oil."""

    rights = {
        "GET": (Rights.OIL_VIEW, Rights.TANK_VIEW, Rights.LOT_VIEW),
        "POST": (Rights.OIL_ADD,),
    }

    def get(self, request):
        oils = TankItem.objects.filter(company=_company(request)).annotate(
            tank_count=Count("tanks", distinct=True),
            lot_count=Count("lots", filter=Q(lots__deleted=False), distinct=True),
        )
        if request.query_params.get("active") == "1":
            oils = oils.filter(is_active=True)
        return Response(OilSerializer(oils, many=True).data)

    def post(self, request):
        serializer = OilWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        oil = services_tank.create_oil(company=_company(request), user=request.user, **serializer.validated_data)
        return Response(OilSerializer(oil).data, status=status.HTTP_201_CREATED)


class SapOilListAPI(_Base):
    """GET : SAP's raw-material oils, the list an oil is picked from. Each says
    which oil here already is it. SAP not answering returns an empty list with
    ``sap_unavailable`` set, so the screen can say why."""

    rights = {"GET": (Rights.OIL_ADD, Rights.OIL_CHANGE)}

    def get(self, request):
        from sap_client.exceptions import SAPConnectionError, SAPDataError

        from .hana_reader import raw_material_oils

        company = _company(request)
        try:
            found = raw_material_oils(company.code)
        except (SAPConnectionError, SAPDataError) as exc:
            logger.warning("exim: raw-material oils from SAP failed: %s", exc)
            return Response({"oils": [], "sap_unavailable": True})
        here = {code.upper(): pk for pk, code in TankItem.objects.filter(company=company).values_list("pk", "code")}
        return Response({
            "oils": [{**oil, "oil": here.get(oil["code"].upper())} for oil in found],
            "sap_unavailable": False,
        })


class OilDetailAPI(_Base):
    """PATCH : change an oil, its code included.  DELETE : remove an unused oil."""

    rights = {"PATCH": (Rights.OIL_CHANGE,), "DELETE": (Rights.OIL_DELETE,)}

    def _oil(self, request, pk):
        return get_object_or_404(TankItem.objects.filter(company=_company(request)), pk=pk)

    def patch(self, request, pk):
        oil = self._oil(request, pk)
        serializer = OilWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        oil = services_tank.update_oil(oil, **serializer.validated_data)
        return Response(OilSerializer(oil).data)

    def delete(self, request, pk):
        services_tank.delete_oil(self._oil(request, pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Tanks
# ---------------------------------------------------------------------------

def _tanks(request):
    return Tank.objects.filter(company=_company(request)).select_related("item", "updated_by")


class TankListAPI(_Base):
    """GET  : every tank and tote.  POST : add one (numbered TNK0001 / TOT001)."""

    rights = {"GET": (Rights.TANK_VIEW,), "POST": (Rights.TANK_ADD,)}

    def get(self, request):
        return Response(TankSerializer(_tanks(request), many=True).data)

    def post(self, request):
        serializer = TankCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        _own(request, data.get("item"))
        tank = services_tank.create_tank(company=_company(request), user=request.user, **data)
        return Response(TankSerializer(tank).data, status=status.HTTP_201_CREATED)


class TankDetailAPI(_Base):
    """PATCH : record a dip (level and oil) or correct a tank.  DELETE : remove it."""

    rights = {"PATCH": (Rights.TANK_CHANGE,), "DELETE": (Rights.TANK_DELETE,)}

    def patch(self, request, pk):
        tank = get_object_or_404(_tanks(request), pk=pk)
        serializer = TankUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        _own(request, data.get("item"))
        tank = services_tank.update_tank(tank, user=request.user, **data)
        return Response(TankSerializer(tank).data)

    def delete(self, request, pk):
        services_tank.delete_tank(get_object_or_404(_tanks(request), pk=pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


class TankEmptyAPI(_Base):
    """POST : the tank is empty - no oil, no level."""

    rights = {"POST": (Rights.TANK_CHANGE,)}

    def post(self, request, pk):
        tank = services_tank.empty_tank(get_object_or_404(_tanks(request), pk=pk), user=request.user)
        return Response(TankSerializer(tank).data)


class TankSummaryAPI(_Base):
    """GET : the farm in totals, and each oil across its tanks."""

    rights = {"GET": (Rights.TANK_VIEW,)}

    def get(self, request):
        company = _company(request)
        return Response(
            {"farm": services_tank.tank_summary(company), "oils": services_tank.oil_summary(company)}
        )


class TankAverageAPI(_Base):
    """GET [?item=<id>] : what the oil in the tanks cost, lot by lot. Every oil
    the farm holds, or the one asked for."""

    rights = {"GET": (Rights.TANK_AVERAGE,)}

    def get(self, request):
        company = _company(request)
        item = request.query_params.get("item")
        if item:
            if not item.isdigit():
                raise ValidationError({"item": "Pass an oil id."})
            oil = get_object_or_404(TankItem.objects.filter(company=company), pk=item)
            return Response(services_tank.average_cost(company, oil))
        return Response([services_tank.average_cost(company, oil) for oil in services_tank.in_tank_oils(company)])


class TankLogAPI(_Base):
    """GET : every arrival into the tank farm, newest first."""

    rights = {"GET": (Rights.TANK_LOG_VIEW,)}

    def get(self, request):
        logs = TankLog.objects.filter(company=_company(request)).select_related("created_by")
        return Response(TankLogSerializer(logs, many=True).data)


class OpeningStockAPI(_Base):
    """POST : an oil's opening stock, entered as a lot already in the tanks."""

    rights = {"POST": (Rights.OPENING_STOCK,)}

    def post(self, request):
        serializer = OpeningStockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        _own(request, data["item"])
        lot = services_lot.opening_stock(
            company=_company(request),
            user=request.user,
            vendor_code=getattr(settings, "EXIM_OPENING_STOCK_VENDOR_CODE", OPENING_STOCK_VENDOR[0]),
            vendor_name=getattr(settings, "EXIM_OPENING_STOCK_VENDOR_NAME", OPENING_STOCK_VENDOR[1]),
            **data,
        )
        return Response(LotSerializer(lot).data, status=status.HTTP_201_CREATED)
