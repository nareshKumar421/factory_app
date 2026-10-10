"""Production order entries API, one endpoint per SAP step.

Every view: login, the ``Company-Code`` context, then a production-order right.
Entries are this company's only (another company's is a 404).

* ``POST entries/`` — the Plan step of a new entry (save, and post if ``post``).
* ``GET entries/<id>/<step>/`` — what that step's page shows, read from SAP.
* ``PUT entries/<id>/<step>/`` — save that step's fields; post it if ``post``.
* ``POST entries/<id>/receipt/preview/`` — the batch number as it is being typed.
* ``GET sap-orders/`` — SAP's own production orders, read-only, each marked
  with the entry here that made it.

A request refused before SAP → 400/403/404/409 with the reason; SAP's refusal
→ 400 with SAP's words; SAP unreachable → 503; SAP broken → 502. A post answers
200 whatever SAP did: ``result`` says how the step came out (posted, waiting
for SAP, refused), beside the entry as it now stands.
"""

import logging

from django.db.models import Count
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from . import identity, services
from .constants import FG_ITEM_GROUP, LINE_CODES, SUPPORTED_COMPANIES
from .models import STEPS, EntryStatus, Step
from .permissions import (
    STEP_PERMISSIONS,
    VIEW_PERMISSION,
    CanCreateProductionOrders,
    CanViewProductionOrders,
    can_take,
)
from .serializers import (
    STEP_INPUTS,
    EntryDetailSerializer,
    EntryListSerializer,
    PlanInputSerializer,
    ReceiptInputSerializer,
)
from .services import EntryError

logger = logging.getLogger(__name__)

#: URL segment -> step.
STEP_SLUGS = {step.lower(): step for step in STEPS}


def _rights(*classes):
    return [IsAuthenticated(), HasCompanyContext(), *(cls() for cls in classes)]


def _jsonable(value):
    """Decimals and dates as strings, for the previews."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is not None and value.__class__.__name__ == "Decimal":
        return format(value, "f")
    return value


class _View(APIView):
    def handle_exception(self, exc):
        if isinstance(exc, EntryError):
            return Response({"detail": str(exc), **exc.extra}, status=exc.status)
        if isinstance(exc, SAPValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPConnectionError):
            logger.error("SAP unreachable in %s: %s", type(self).__name__, exc)
            return Response(
                {"detail": str(exc) or "SAP is not answering. Try again shortly.", "code": "SAP_UNAVAILABLE"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, SAPDataError):
            logger.error("SAP data error in %s: %s", type(self).__name__, exc)
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    @property
    def company(self):
        return self.request.company.company

    def entry(self, pk):
        entry = services.entries_for(self.company).filter(pk=pk).first()
        if entry is None:
            raise NotFound("Production entry not found.")
        return entry

    def detail(self, entry):
        entry = services.entries_for(self.company).prefetch_related("lines__batches").get(pk=entry.pk)
        context = {
            "request": self.request,
            "postings": services.postings_by_step(entry),
            "change_postings": services.postings_for_changes(entry),
        }
        return EntryDetailSerializer(entry, context=context).data


class MeAPI(_View):
    """GET — what the caller may do here, and whether SAP will take their posts."""

    def get_permissions(self):
        return _rights(CanViewProductionOrders)

    def get(self, request):
        return Response(
            {
                "supported": self.company.code in SUPPORTED_COMPANIES,
                "rights": {step: can_take(request.user, step) for step in STEPS},
                "can_view": request.user.has_perm(VIEW_PERMISSION)
                or any(request.user.has_perm(p) for p in STEP_PERMISSIONS.values()),
                "sap_login": identity.login_status(request.user, self.company),
                "line_codes": [{"code": code, "label": label} for code, label in LINE_CODES.items()],
            }
        )


class ProductSearchAPI(_View):
    """GET ?search= — finished goods with a production BOM."""

    def get_permissions(self):
        return _rights(CanCreateProductionOrders)

    def get(self, request):
        services.require_supported(self.company)
        rows = services.reader_for(self.company).search_products(
            FG_ITEM_GROUP[self.company.code], request.query_params.get("search", ""), limit=30
        )
        return Response(
            [
                {
                    "item_code": row["item_code"],
                    "item_name": row["item_name"],
                    "uom": row["uom"],
                    "pieces_per_box": format(row["pieces_per_box"], "f"),
                    "variety": row["variety"],
                    "warehouse": row["bom_warehouse"],
                }
                for row in rows
            ]
        )


class VarietiesAPI(_View):
    """GET — the variety codes SAP has (distribution rules, dimension 1)."""

    def get_permissions(self):
        return _rights(CanViewProductionOrders)

    def get(self, request):
        return Response(services.reader_for(self.company).varieties())


class PlanPreviewAPI(_View):
    """POST — the planned order the Plan step's fields would create, from SAP now."""

    def get_permissions(self):
        return _rights(CanCreateProductionOrders)

    def post(self, request):
        serializer = PlanInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        built = services.plan_preview(self.company, serializer.validated_data)
        built.pop("lines")  # the BOM's lines go to SAP with the order; the Issue page shows them
        return Response(_jsonable(built))


def _post_result(result):
    return {**result, "step": str(result["step"])} if result else None


def _date_param(params, name):
    value = params.get(name) or ""
    if not value:
        return None
    try:
        day = parse_date(value)
    except ValueError:
        day = None
    if day is None:
        raise EntryError(f"{name} must be a date (YYYY-MM-DD).")
    return day


class SapOrdersAPI(_View):
    """GET ?status= &type= &date_from= &date_to= &search= &limit= &offset= — every
    production order in SAP, made here or in SAP itself, newest first."""

    def get_permissions(self):
        return _rights(CanViewProductionOrders)

    def get(self, request):
        params = request.query_params
        try:
            limit = max(1, min(int(params.get("limit", 50)), 100))
            offset = max(0, int(params.get("offset", 0)))
        except ValueError:
            limit, offset = 50, 0
        found = services.sap_orders(
            self.company,
            status=params.get("status", ""),
            order_type=params.get("type", ""),
            date_from=_date_param(params, "date_from"),
            date_to=_date_param(params, "date_to"),
            search=params.get("search", ""),
            limit=limit,
            offset=offset,
        )
        return Response(_jsonable(found))


class EntryListCreateAPI(_View):
    """GET ?status= &date_from= &date_to= &search= &limit= &offset= — entries, newest first.
    POST — the Plan step of a new entry; ``post`` also creates its order in SAP."""

    def get_permissions(self):
        if self.request.method == "POST":
            return _rights(CanCreateProductionOrders)
        return _rights(CanViewProductionOrders)

    def get(self, request):
        params = request.query_params
        rows = services.entries_for(self.company)
        if params.get("status") in EntryStatus.values:
            rows = rows.filter(status=params["status"])
        if params.get("date_from"):
            rows = rows.filter(posting_date__gte=params["date_from"])
        if params.get("date_to"):
            rows = rows.filter(posting_date__lte=params["date_to"])
        rows = rows.filter(services.search_filter(params.get("search", "")))
        try:
            limit = max(1, min(int(params.get("limit", 50)), 200))
            offset = max(0, int(params.get("offset", 0)))
        except ValueError:
            limit, offset = 50, 0
        counts = dict(
            services.entries_for(self.company).values_list("status").annotate(n=Count("id")).order_by()
        )
        return Response(
            {
                "count": rows.count(),
                "status_counts": counts,
                "results": EntryListSerializer(rows[offset:offset + limit], many=True).data,
            }
        )

    def post(self, request):
        serializer = PlanInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        post = data.pop("post")
        entry = services.create_entry(self.company, request.user, data)
        result = services.post_step(entry, request.user, Step.PLAN) if post else None
        return Response(
            {"entry": self.detail(entry), "result": _post_result(result)}, status=status.HTTP_201_CREATED
        )


class EntryDetailAPI(_View):
    """GET — one entry with its lines and steps. DELETE — a draft that has not reached SAP."""

    def get_permissions(self):
        if self.request.method == "DELETE":
            return _rights(CanCreateProductionOrders)
        return _rights(CanViewProductionOrders)

    def get(self, request, pk):
        return Response(self.detail(self.entry(pk)))

    def delete(self, request, pk):
        services.delete_entry(self.entry(pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


class EntryStepAPI(_View):
    """GET — what a step's page shows, read from SAP now.
    PUT — save the step's fields, and post the step when ``post``."""

    def get_permissions(self):
        return _rights(CanViewProductionOrders)

    def _step(self, slug):
        step = STEP_SLUGS.get(slug)
        if step is None:
            raise NotFound("No such step.")
        return step

    def get(self, request, pk, step):
        step = self._step(step)
        entry = self.entry(pk)
        if step == Step.PLAN:
            data = {
                "item_code": entry.item_code, "boxes": entry.boxes, "loose_pieces": entry.loose_pieces,
                "posting_date": entry.posting_date, "remarks": entry.remarks,
            }
            if entry.status not in (EntryStatus.DRAFT, EntryStatus.PLANNED):
                return Response({"locked": True})
            built = services.plan_preview(entry.company, data)
            built.pop("lines")
            return Response(_jsonable(built))
        if step == Step.ISSUE:
            return Response(_jsonable(services.issue_preview(entry)))
        if step == Step.RECEIPT:
            return Response(_jsonable(services.receipt_preview(entry, {})))
        return Response({})

    def put(self, request, pk, step):
        step = self._step(step)
        entry = self.entry(pk)
        if not can_take(request.user, step):
            raise EntryError(f"You do not have the right to take the {step.label.lower()} step.", status=403)
        serializer = STEP_INPUTS[step](data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        post = data.pop("post")
        if step == Step.PLAN and entry.status == EntryStatus.PLANNED:
            # The order is in SAP, planned: a change goes straight to SAP; there is no draft of it.
            if not post:
                raise EntryError("A change to a planned order is sent to SAP; there is no draft of it.")
            result = services.request_replan(entry, request.user, data)
            return Response({"entry": self.detail(entry), "result": _post_result(result)})
        if step == Step.PLAN:
            entry = services.save_plan(entry, request.user, data)
        elif step == Step.ISSUE:
            entry = services.save_issue(entry, request.user, data)
        elif step == Step.RECEIPT:
            entry = services.save_receipt(entry, request.user, data)
        elif step == Step.CLOSE:
            entry = services.save_close(entry, request.user, data)
        result = services.post_step(entry, request.user, step) if post else None
        return Response({"entry": self.detail(entry), "result": _post_result(result)})


class EntryUnreleaseAPI(_View):
    """POST — take a released order back to planned in SAP, while nothing is issued to it."""

    def get_permissions(self):
        return _rights(CanViewProductionOrders)

    def post(self, request, pk):
        entry = self.entry(pk)
        result = services.request_unrelease(entry, request.user)
        return Response({"entry": self.detail(entry), "result": _post_result(result)})


class ReceiptPreviewAPI(_View):
    """POST — the batch the goods would go in under, for the fields as typed."""

    def get_permissions(self):
        return _rights(CanViewProductionOrders)

    def post(self, request, pk):
        serializer = ReceiptInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data.pop("post", None)
        return Response(_jsonable(services.receipt_preview(self.entry(pk), data)))
