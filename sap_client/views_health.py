"""GET /api/v1/sap-health/ -- is SAP answering, as the app last found it.

Polled by every open FactoryFlow tab for the "SAP is down" banner. Any logged-in
user may ask: whether SAP is up is not a secret, and the people most in need of
knowing are the ones at the gate and the dock, not the admins.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import health


class SAPHealthView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(health.snapshot())
