"""
The public registration forms' API — no login (decision D2).

SAP Portal served ``/register`` and ``/vendor-register`` to anyone and took
their submissions without a token; JI keeps that, because the people filling
these in are outsiders. What is different:

* ``AllowAny`` with no authentication at all (``authentication_classes = []``),
  so a stale token in the browser cannot turn a submission into a 401, and
  every request is counted by the anonymous throttle (``throttles.py``).
* Uploads are files (15 MB each, PDF/JPG/PNG checked by their bytes), not
  base64 in a JSON body of up to 50 MB.
* The company must be one of the three SAP companies, and exist here.
* The reads the form needs — the company list and SAP's states — reveal names
  and codes only, are throttled, and the states are cached so strangers cannot
  make this server query SAP on every keystroke.
"""

import json
import logging

from django.core.cache import cache
from django.db.models import Case, IntegerField, Value, When
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from company.models import Company
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.registry import COMPANY_SAP_REGISTRY

from .constants import PUBLIC_COMPANY_CODES
from .families import FAMILIES
from .serializers import CustomerSubmitSerializer, VendorSubmitSerializer
from .services.submission import collect_files, submit
from .throttles import PublicReadThrottle, PublicSubmitThrottle

logger = logging.getLogger(__name__)

#: How long SAP's state list is served from memory. States change about never.
STATES_CACHE_SECONDS = 6 * 60 * 60


def public_companies():
    """The companies a stranger may register with: the three SAP companies that
    exist and are active here, in the order the portal listed them."""
    codes = [code for code in PUBLIC_COMPANY_CODES if code in COMPANY_SAP_REGISTRY]
    order = Case(
        *[When(code=code, then=Value(index)) for index, code in enumerate(codes)],
        output_field=IntegerField(),
    )
    return Company.objects.filter(code__in=codes, is_active=True).annotate(_order=order).order_by("_order")


class _PublicView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PublicReadThrottle]


class PublicCompaniesAPI(_PublicView):
    """GET — ``[{code, name}]`` of the companies the forms may be sent to."""

    def get(self, request):
        return Response([{"code": c.code, "name": c.name} for c in public_companies()])


class PublicStatesAPI(_PublicView):
    """GET ?company= — ``[{code, name}]``: SAP's Indian states for that company."""

    def get(self, request):
        code = (request.query_params.get("company") or "").strip().upper()
        if not public_companies().filter(code=code).exists():
            return Response({"detail": "Choose one of the listed companies."}, status=status.HTTP_400_BAD_REQUEST)
        key = f"partner_onboarding:states:{code}"
        states = cache.get(key)
        if states is None:
            try:
                states = SAPClient(company_code=code).lookup_states("IN")
            except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
                logger.error("Public state list for %s could not be read: %s", code, exc)
                return Response(
                    {"detail": "The state list is unavailable right now. Please try again shortly."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            states = [{"code": row["code"], "name": row["name"]} for row in states]
            cache.set(key, states, STATES_CACHE_SECONDS)
        return Response(states)


class PublicSubmitAPI(_PublicView):
    """POST — one registration, as ``multipart/form-data``.

    The fields travel as JSON in a ``payload`` part (addresses and bank accounts
    are lists); each document is a file part named after its slot: ``pan``,
    ``aadhaar``, ``cheque``, ``gst``, ``msme``, ``fssai``, ``other`` (the last
    two may repeat). Answers ``{reference, message}``.
    """

    throttle_classes = [PublicSubmitThrottle]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    family = ""
    serializer_classes = {"customer": CustomerSubmitSerializer, "vendor": VendorSubmitSerializer}

    def post(self, request):
        family = FAMILIES[self.family]
        payload = request.data.get("payload") if hasattr(request.data, "get") else None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return Response({"detail": "The form data could not be read."}, status=status.HTTP_400_BAD_REQUEST)
        elif payload is None:
            payload = request.data
        if not isinstance(payload, dict):
            return Response({"detail": "The form data could not be read."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.serializer_classes[self.family](data=payload)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        files = collect_files(request.FILES, family, validated.get("has_msme", False))
        company = public_companies().filter(code=validated["company"]).first()
        if company is None:
            return Response(
                {"company": ["This company is not taking registrations."]}, status=status.HTTP_400_BAD_REQUEST
            )
        registration = submit(family, validated, files, company)
        return Response(
            {
                "reference": registration.reference,
                "message": (
                    f"Thank you. Your {family.label} registration {registration.reference} has been "
                    f"received by {company.name} and will be reviewed."
                ),
            },
            status=status.HTTP_201_CREATED,
        )
