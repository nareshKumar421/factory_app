"""The screen that edits a GRPO already in SAP: find it, show it, change it.

GET  /api/v1/grpo/sap-edit/find/?doc_num=<n>[&doc_type=I|S]
                                               the GRPOs with that SAP number
GET  /api/v1/grpo/sap-edit/<doc_entry>/        what SAP holds, and what may change
POST /api/v1/grpo/sap-edit/<doc_entry>/        change it (JSON, or multipart with
                                               ``data`` and ``attachments``)

Any GRPO of the request's company, not only those the app posted. The rules of
what may change, and why, are in :mod:`grpo.sap_edit`.
"""

import json
import logging
from decimal import Decimal

from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_postings import services as sap_postings
from sap_postings.models import SapPostingOutcome

from . import sap_edit
from .models import GRPOPosting, GRPOSapEdit, GRPOStatus, ServiceGRPOPosting
from .permissions import CanEditGRPOInSap

logger = logging.getLogger(__name__)

TYPE_LABELS = {sap_edit.MATERIAL: "Material", sap_edit.SERVICE: "Service"}


def _sap_unavailable():
    return Response(
        {"detail": "SAP could not be read just now. Please try again in a minute."},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _money(value):
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.01")))


def _iso(value):
    return value.isoformat() if value else None


def _user_name(user):
    if user is None:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


def edit_payload(edit: GRPOSapEdit) -> dict:
    return {
        "id": edit.id,
        "status": edit.status,
        "status_label": edit.get_status_display(),
        "message": edit.message,
        "summary": sap_edit.summary(edit.changes, len(edit.attachments.all())),
        "changes": edit.changes,
        "attachments": [a.original_filename for a in edit.attachments.all()],
        "requested_by": _user_name(edit.requested_by),
        "created_at": _iso(edit.created_at),
        "posted_at": _iso(edit.posted_at),
    }


def document_payload(request, company, reader, document, udfs) -> dict:
    """Everything the edit screen shows for one GRPO."""
    doc_type = document["doc_type"]
    permission = sap_edit.PERMISSION_BY_TYPE.get(doc_type, "")
    can_edit = bool(permission) and request.user.has_perm(permission)
    blocked = sap_edit.editable_reason(document)

    floor = sap_edit.back_date_floor(company.code)
    back_date = None
    if floor and document["doc_date"] and document["doc_date"] < floor:
        back_date = {
            "floor": floor.isoformat(),
            "message": (
                f"SAP refuses the app's changes to GRPOs posted before {floor:%d %b %Y} "
                f"(this one is dated {document['doc_date']:%d %b %Y}). Saving will most "
                "likely be refused; such a GRPO can still be changed in SAP by someone "
                "SAP allows to back-date."
            ),
        }

    contacts = reader.contact_persons(document["card_code"]) if document["card_code"] else []
    shared_with = (
        reader.attachment_shared_with(document["attachment_entry"], document["doc_entry"])
        if document["attachment_entry"] else []
    )
    header = []
    for field in sap_edit.header_fields(udfs):
        entry = {
            "key": field.key,
            "label": field.label,
            "type": field.type,
            "max_length": field.max_length,
            "value": sap_edit.normalize(field, sap_edit.current_header_value(document, field)),
        }
        if field.key == "contact_person":
            entry["options"] = [
                {"value": c["code"], "label": c["name"] or str(c["code"])}
                for c in contacts
                if c["active"] or c["code"] == document["contact_person"]
            ]
        header.append(entry)

    is_service = doc_type == sap_edit.SERVICE
    lines = []
    for line in document["lines"]:
        closed = sap_edit.line_closed_reason(line)
        lines.append(
            {
                "line_num": line["line_num"],
                "item_code": line["item_code"],
                "description": line["description"],
                "quantity": str(line["quantity"]) if line["quantity"] is not None else None,
                "unit_price": _money(line["unit_price"]),
                "discount_percent": str(line["discount_percent"] or 0),
                "amount": _money(line["amount"]),
                "tax_amount": _money(line["tax_amount"]),
                "tax_code": line["tax_code"],
                "gl_account": line["gl_account"],
                "variety": line["variety"],
                "month": line["month"],
                "budget": line["budget"],
                "line_status": line["line_status"],
                "closed_reason": closed,
                "ap_invoice_doc_num": line["ap_invoice_doc_num"],
                "editable": is_service and not closed and not blocked,
            }
        )

    edits = (
        GRPOSapEdit.objects.filter(company=company, doc_entry=document["doc_entry"])
        .select_related("requested_by")
        .prefetch_related("attachments")
        .order_by("-created_at")[:20]
    )
    material_posting = (
        GRPOPosting.objects.filter(
            sap_doc_entry=document["doc_entry"],
            vehicle_entry__company=company,
            status=GRPOStatus.POSTED,
        )
        .values_list("id", flat=True)
        .first()
    )
    service_posting = (
        ServiceGRPOPosting.objects.filter(
            sap_doc_entry=document["doc_entry"],
            dispatch_plan__company=company,
            status=GRPOStatus.POSTED,
        )
        .values_list("id", flat=True)
        .first()
    )

    return {
        "doc_entry": document["doc_entry"],
        "doc_num": document["doc_num"],
        "doc_type": doc_type,
        "doc_type_label": TYPE_LABELS.get(doc_type, doc_type),
        "doc_status": document["doc_status"],
        "cancelled": document["cancelled"],
        "doc_date": _iso(document["doc_date"]),
        "card_code": document["card_code"],
        "card_name": document["card_name"],
        "branch_name": document["branch_name"],
        "doc_total": _money(document["doc_total"]),
        "vat_sum": _money(document["vat_sum"]),
        "created_by": document["created_by_name"] or document["created_by_code"],
        "created_on": _iso(document["created_on"]),
        "can_edit": can_edit,
        "permission_message": "" if can_edit else (
            f"You can view this GRPO but not change it: changing a "
            f"{TYPE_LABELS.get(doc_type, '').lower()} GRPO in SAP needs its own right."
        ),
        "blocked_reason": blocked,
        "back_date": back_date,
        "header_fields": header,
        "line_fields": (
            [{"key": f.key, "label": f.label, "type": f.type} for f in sap_edit.LINE_FIELDS]
            if is_service else []
        ),
        "month_options": reader.month_codes() if is_service else [],
        "lines": lines,
        "attachments": [
            {"line": a["line"], "filename": a["filename"], "date": _iso(a["date"])}
            for a in document["attachments"]
        ],
        # Files can be removed unless the entry is shared with another document;
        # the last one only goes when a new one comes in the same save.
        "attachments_shared_message": sap_edit.shared_message(shared_with) if shared_with else "",
        "edits": [edit_payload(edit) for edit in edits],
        "material_posting_id": material_posting,
        "service_posting_id": service_posting,
    }


class GRPOSapEditFindAPI(APIView):
    """The GRPOs in SAP with one number, in the request's company."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditGRPOInSap]

    def get(self, request):
        raw = (request.query_params.get("doc_num") or "").strip()
        if not raw.isdigit():
            return Response(
                {"detail": "Type the GRPO's SAP number (digits only)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        doc_type = (request.query_params.get("doc_type") or "").strip().upper()
        if doc_type and doc_type not in TYPE_LABELS:
            return Response(
                {"detail": "doc_type is I (material) or S (service)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        company = request.company.company
        try:
            matches = sap_edit.reader_for(company.code).find(int(raw))
        except SAPConnectionError:
            return _sap_unavailable()
        except SAPDataError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            {
                "results": [
                    {
                        **match,
                        "doc_date": _iso(match["doc_date"]),
                        "doc_type_label": TYPE_LABELS.get(match["doc_type"], match["doc_type"]),
                    }
                    for match in matches
                    if not doc_type or match["doc_type"] == doc_type
                ],
                # Found, but of the other type: the screen says where it is edited.
                "other_type": [
                    {
                        "doc_entry": match["doc_entry"],
                        "doc_type": match["doc_type"],
                        "doc_type_label": TYPE_LABELS.get(match["doc_type"], match["doc_type"]),
                    }
                    for match in matches
                    if doc_type and match["doc_type"] != doc_type
                ],
            }
        )


class GRPOSapEditAPI(APIView):
    """One GRPO as SAP holds it, and the change the person asks for."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditGRPOInSap]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _load(self, company, doc_entry):
        reader = sap_edit.reader_for(company.code)
        udfs = reader.header_udfs()
        return reader, udfs, reader.document(doc_entry, udfs)

    def get(self, request, doc_entry):
        company = request.company.company
        try:
            reader, udfs, document = self._load(company, doc_entry)
            if document is None:
                return Response(
                    {"detail": f"{company.name} has no GRPO with DocEntry {doc_entry} in SAP."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(document_payload(request, company, reader, document, udfs))
        except SAPConnectionError:
            return _sap_unavailable()
        except SAPDataError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    def post(self, request, doc_entry):
        company = request.company.company
        if request.content_type and "multipart" in request.content_type:
            try:
                raw = request.data.get("data", "{}")
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                return Response(
                    {"detail": "Invalid JSON in 'data' field"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            files = request.FILES.getlist("attachments")
        else:
            data = request.data
            files = []
        if not isinstance(data, dict):
            return Response({"detail": "Invalid request data"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            reader, udfs, document = self._load(company, doc_entry)
        except SAPConnectionError:
            return _sap_unavailable()
        except SAPDataError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if document is None:
            return Response(
                {"detail": f"{company.name} has no GRPO with DocEntry {doc_entry} in SAP."},
                status=status.HTTP_404_NOT_FOUND,
            )
        permission = sap_edit.PERMISSION_BY_TYPE.get(document["doc_type"])
        if not permission or not request.user.has_perm(permission):
            label = TYPE_LABELS.get(document["doc_type"], "").lower()
            return Response(
                {"detail": f"You do not have the right to change a {label} GRPO in SAP."},
                status=status.HTTP_403_FORBIDDEN,
            )

        month_codes = None
        if document["doc_type"] == sap_edit.SERVICE and data.get("lines"):
            try:
                month_codes = {code["code"] for code in reader.month_codes()}
            except (SAPConnectionError, SAPDataError):
                month_codes = None  # SAP decides at send time

        try:
            edit, outcome = sap_edit.submit(
                company=company,
                document=document,
                udfs=udfs,
                data=data,
                files=files,
                user=request.user,
                month_codes=month_codes,
            )
        except sap_edit.EditConflict as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except sap_edit.EditError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except sap_postings.PostingInProgress as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        body = {"edit": edit_payload(edit), "outcome": outcome.kind, "message": outcome.message}
        if outcome.kind == SapPostingOutcome.POSTED:
            return Response(body, status=status.HTTP_200_OK)
        if outcome.kind == SapPostingOutcome.WAITING:
            body["detail"] = (
                "SAP is not answering just now. The change is saved in the app and goes "
                "to SAP by itself once SAP is back."
            )
            return Response(body, status=status.HTTP_202_ACCEPTED)
        body["detail"] = f"SAP refused the change: {outcome.message}"
        return Response(body, status=status.HTTP_400_BAD_REQUEST)
