"""
API for the department ownership chart.

One endpoint, because it is one screen:

* ``GET  /api/v1/org-chart/chart/`` — the whole chart.
* ``PUT  /api/v1/org-chart/chart/`` — replace it (needs ``can_manage_org_chart``).

Both are scoped to the company in the ``Company-Code`` header: Oil, Mart and
Beverages are separate plants and keep separate charts.

The GET also reports ``can_manage`` so the page knows whether to offer the Edit
button at all, rather than letting a viewer edit into a 403.
"""

from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import services
from .permissions import MANAGE_PERMISSION, OrgChartPermission
from .serializers import (
    ChartSaveSerializer,
    OrgChartSettingsSerializer,
    OrgDepartmentSerializer,
)


class OrgChartAPI(APIView):
    permission_classes = [OrgChartPermission, HasCompanyContext]

    @staticmethod
    def _company(request):
        """The company the header named — set by :class:`HasCompanyContext`."""
        return request.company.company

    def _payload(self, request, departments):
        return {
            **OrgChartSettingsSerializer(
                services.get_settings(self._company(request))
            ).data,
            "departments": OrgDepartmentSerializer(departments, many=True).data,
            "can_manage": bool(request.user.has_perm(MANAGE_PERMISSION)),
        }

    def get(self, request):
        return Response(
            self._payload(request, services.get_chart(self._company(request)))
        )

    def put(self, request):
        serializer = ChartSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        departments = services.save_chart(
            serializer.validated_data, self._company(request), user=request.user
        )
        return Response(self._payload(request, departments))
