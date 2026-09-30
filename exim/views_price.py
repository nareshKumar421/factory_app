"""Oil price endpoints: a day's prices and pack rates, a range of them, and the
price sheet itself - read as it stands, or saved as today's.

Thin on purpose: check the right, work inside the caller's company, call
``exim.services_price`` or ``exim.price_sheet``. Figures go out as plain
numbers (a read-out).
"""

from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from . import price_sheet, services_price
from .permissions import Rights
from .services_licence import EximError
from .services_tank import SapUnavailable
from .views_tank import _Base, _company


def _date(params, name, required=False):
    value = params.get(name)
    if not value:
        if required:
            raise ValidationError({name: "Pass a date as YYYY-MM-DD."})
        return None
    try:
        day = parse_date(value)
    except ValueError:
        day = None
    if day is None:
        raise ValidationError({name: "Pass a date as YYYY-MM-DD."})
    return day


def _sheet():
    """The sheet as it stands, or the reason it cannot be read (503)."""
    try:
        return price_sheet.read_sheet()
    except price_sheet.PriceSheetError as exc:
        raise SapUnavailable(str(exc), "price_sheet_unavailable", {}) from exc


class PriceDayAPI(_Base):
    """GET [?date=] : the commodity prices of a day (the latest on or before it),
    each beside the previous day's."""

    rights = {"GET": (Rights.PRICE_VIEW, Rights.PRICE_GRAPH)}

    def get(self, request):
        return Response(services_price.price_day(_company(request), _date(request.query_params, "date")))


class PriceRangeAPI(_Base):
    """GET ?from=&to= : every commodity price in the range, day by day."""

    rights = {"GET": (Rights.PRICE_VIEW, Rights.PRICE_GRAPH)}

    def get(self, request):
        params = request.query_params
        return Response(services_price.price_range(
            _company(request), _date(params, "from", True), _date(params, "to", True),
        ))


class RateDayAPI(_Base):
    """GET [?date=] : Jivo's pack rates of a day, each beside the previous day's."""

    rights = {"GET": (Rights.RATE_VIEW,)}

    def get(self, request):
        return Response(services_price.rate_day(_company(request), _date(request.query_params, "date")))


class RateRangeAPI(_Base):
    """GET ?from=&to= : every pack rate in the range, day by day."""

    rights = {"GET": (Rights.RATE_VIEW,)}

    def get(self, request):
        params = request.query_params
        return Response(services_price.rate_range(
            _company(request), _date(params, "from", True), _date(params, "to", True),
        ))


class PriceSheetAPI(_Base):
    """GET  : the commodity prices the sheet shows right now, not saved.
    POST : save them as today's (replacing today's if already read)."""

    rights = {"GET": (Rights.PRICE_FETCH, Rights.PRICE_ADD), "POST": (Rights.PRICE_ADD,)}

    def get(self, request):
        return Response({"prices": _sheet()["prices"]})

    def post(self, request):
        sheet = _sheet()
        counts = services_price.save_prices(_company(request), sheet["prices"])
        return Response(
            {**counts, **services_price.price_day(_company(request))}, status=status.HTTP_200_OK,
        )


class RateSheetAPI(_Base):
    """GET  : Jivo's pack rates the sheet shows right now, not saved.
    POST : save them as today's."""

    rights = {"GET": (Rights.RATE_FETCH, Rights.RATE_ADD), "POST": (Rights.RATE_ADD,)}

    def get(self, request):
        return Response({"rates": _sheet()["rates"]})

    def post(self, request):
        sheet = _sheet()
        if not sheet["rates"]:
            raise EximError('The sheet\'s "JIVO RATE" table has no rates.', "no_rates", {})
        counts = services_price.save_rates(_company(request), sheet["rates"])
        return Response({**counts, **services_price.rate_day(_company(request))})
