"""GET /api/v1/sap-health/ -- is SAP answering, as the app last found it.

Polled by every open FactoryFlow tab for the "SAP is down" banner. Any logged-in
user may ask: whether SAP is up is not a secret, and the people most in need of
knowing are the ones at the gate and the dock, not the admins.

Besides each component's state it says what the banner needs to be truthful
about an outage now that the app works through one:

* ``waits_for_sap`` -- the postings that wait and post by themselves once SAP
  is back (the SAP posting queue), rather than failing;
* ``copy`` -- with HANA down, how old the copy of SAP is that the screens of the
  company on the request (``Company-Code``) are working from.
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import health

logger = logging.getLogger(__name__)


class SAPHealthView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = dict(health.snapshot())
        try:
            from sap_postings.services import waiting_kinds

            data["waits_for_sap"] = waiting_kinds()
        except Exception:  # noqa: BLE001 -- the banner must never be what fails
            logger.exception("Could not list the postings that wait for SAP")
        hana = (data.get("components") or {}).get(health.HANA) or {}
        company_code = request.headers.get("Company-Code")
        if hana.get("status") == health.DOWN and company_code:
            try:
                from sap_mirror.monitor import freshness

                data["copy"] = freshness(company_code)
            except Exception:  # noqa: BLE001
                logger.exception("Could not read how fresh the SAP copy is")
        return Response(data)
