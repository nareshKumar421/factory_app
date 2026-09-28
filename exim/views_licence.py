"""Export licence endpoints.

Thin on purpose: check the right, find the licence in the caller's company,
call one function in ``exim.services_licence``, serialise the result.

The right each call needs depends on the licence's kind and, for a line, its
direction (``exim.permissions.licence_right``), so it is checked in the handler
once the kind is known rather than by a permission class.
"""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import services_licence as services
from .models_licence import Licence, LicenceKind, LicenceLine
from .permissions import licence_right, require
from .serializers_licence import (
    LicenceCreateSerializer,
    LicenceDetailSerializer,
    LicenceListSerializer,
    LicenceUpdateSerializer,
    LineCreateSerializer,
    LineWriteSerializer,
)


def _licences(request):
    return Licence.objects.filter(company=request.company.company)


def _licence_or_404(request, pk):
    """A licence of the caller's company, or 404 - never 403, so one in another
    company cannot be told apart from one that does not exist."""
    return get_object_or_404(_licences(request), pk=pk)


def _detail(licence):
    licence = (
        Licence.objects.select_related("created_by", "updated_by")
        .prefetch_related("lines__linked_line")
        .get(pk=licence.pk)
    )
    return LicenceDetailSerializer(licence).data


class LicenceListCreateAPI(APIView):
    """GET  ?kind=ADVANCE|DFIA : that kind's register, newest issue first.
    POST                       : put a licence on the register.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext()]

    def get(self, request):
        kind = request.query_params.get("kind", "")
        if kind not in LicenceKind.values:
            raise ValidationError({"kind": f"Pass ?kind= one of {', '.join(LicenceKind.values)}."})
        require(request.user, licence_right("view", kind))

        licences = _licences(request).filter(kind=kind)
        status_filter = request.query_params.get("status")
        if status_filter:
            licences = licences.filter(status=status_filter)
        return Response(LicenceListSerializer(licences, many=True).data)

    def post(self, request):
        serializer = LicenceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        require(request.user, licence_right("add", data["kind"]))
        licence = services.create_licence(
            company=request.company.company, user=request.user, **data
        )
        return Response(_detail(licence), status=status.HTTP_201_CREATED)


class LicenceDetailAPI(APIView):
    """GET    : a licence with its lines.
    PATCH  : change what was entered on it (not its kind or number).
    DELETE : take it off the register, with its lines.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext()]

    def get(self, request, pk):
        licence = _licence_or_404(request, pk)
        require(request.user, licence_right("view", licence.kind))
        return Response(_detail(licence))

    def patch(self, request, pk):
        licence = _licence_or_404(request, pk)
        require(request.user, licence_right("change", licence.kind))
        serializer = LicenceUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        licence = services.update_licence(licence, user=request.user, **serializer.validated_data)
        return Response(_detail(licence))

    def delete(self, request, pk):
        licence = _licence_or_404(request, pk)
        require(request.user, licence_right("delete", licence.kind))
        services.delete_licence(licence)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LicenceLineCreateAPI(APIView):
    """POST : add a bill of entry or shipping bill to a licence. Returns the
    licence, since the line moves its totals."""

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext()]

    def post(self, request, pk):
        licence = _licence_or_404(request, pk)
        serializer = LineCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        require(request.user, licence_right("add", licence.kind, data["direction"]))
        services.add_line(licence, user=request.user, **data)
        return Response(_detail(licence), status=status.HTTP_201_CREATED)


class LicenceLineDetailAPI(APIView):
    """PATCH  : change a line (not its direction).
    DELETE : remove it.
    Both return the licence, since either moves its totals."""

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext()]

    def _line(self, request, pk):
        return get_object_or_404(
            LicenceLine.objects.select_related("licence").filter(
                licence__company=request.company.company
            ),
            pk=pk,
        )

    def patch(self, request, pk):
        line = self._line(request, pk)
        require(request.user, licence_right("change", line.licence.kind, line.direction))
        serializer = LineWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_line(line, user=request.user, **serializer.validated_data)
        return Response(_detail(line.licence))

    def delete(self, request, pk):
        line = self._line(request, pk)
        require(request.user, licence_right("delete", line.licence.kind, line.direction))
        licence = services.delete_line(line, user=request.user)
        return Response(_detail(licence))
