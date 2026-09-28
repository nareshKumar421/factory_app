"""The customs exchange rates endpoint. See ``exim.customs_rates``."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import customs_rates
from .permissions import CanViewCustomsRates


class CustomsRatesAPI(APIView):
    """GET [?refresh=1] : the latest notified rates for every currency.

    Not company-scoped: customs notifies one set of rates for everybody.
    """

    permission_classes = [IsAuthenticated, CanViewCustomsRates]

    def get(self, request):
        refresh = request.query_params.get("refresh") in ("1", "true")
        return Response(customs_rates.latest(refresh=refresh))
