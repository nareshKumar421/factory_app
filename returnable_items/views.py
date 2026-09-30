"""API for the Returnable Items module.

The four-stage flow lives here as ``@action`` transitions, each of which
validates the current status, stamps actor + timestamp, writes a timeline row,
and notifies the other side of the handoff:

    department  submit()          → gate
    gate        gate_out()        → department
    gate        record_return()   → department
    department  acknowledge()     → gate
    department  close()
"""

import csv
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from maintenance import store_stock
from maintenance.models import MaintenanceSpare, SpareCategory
from maintenance.serializers import unique_spare_part_number

from . import notifications as notify
from .constants import (
    CANCELLABLE_STATUSES,
    OUTSTANDING_STATUSES,
    ItemConditionOut,
    ItemReturnCondition,
    ReturnableLogAction,
    ReturnablePurpose,
    ReturnableStatus,
)
from .models import (
    ReturnableGatePass,
    ReturnableGatePassAttachment,
    ReturnableGatePassItem,
    ReturnableReturnEvent,
    ReturnableReturnEventAttachment,
    ReturnableReturnEventItem,
    is_photo_file,
)
from .permissions import (
    CanAcknowledgeReturnable,
    CanApproveReturnable,
    CanCancelReturnable,
    CanCloseReturnable,
    CanEditReturnable,
    CanGateInReturnable,
    CanGateOutReturnable,
    CanManageReturnable,
    CanRejectReturnableAtGate,
    CanShortCloseReturnable,
    CanSubmitReturnable,
    CanViewReturnableAtGate,
    CanViewReturnableReports,
)
from .serializers import (
    GateOutInputSerializer,
    ReasonInputSerializer,
    RecordReturnInputSerializer,
    ReturnableGatePassAttachmentSerializer,
    ReturnableGatePassItemSerializer,
    ReturnableGatePassListSerializer,
    ReturnableGatePassLogSerializer,
    ReturnableGatePassSerializer,
    ReturnableReturnEventSerializer,
)

logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes"}

#: Why a pass cannot move on without a photo, in the words the screens show.
NO_PHOTO_OUT = (
    "Attach a photo of the material before this pass goes for approval. "
    "The gate identifies what is leaving by its photo."
)
NO_PHOTO_BACK = (
    "Take a photo of the material that came back. "
    "The department checks what was returned against it."
)


def _company(request):
    return request.company.company


def _is_true(value):
    return str(value).lower() in TRUE_VALUES


def _split_multipart(request):
    """``(data, files)`` from a request that may carry files.

    Multipart: the JSON body in a "data" part, the files in "attachments" parts
    -- the shape GRPO and AR invoices already use. A plain JSON body carries no
    files. Raises ``json.JSONDecodeError`` on a malformed "data" part.
    """
    if request.content_type and "multipart" in request.content_type:
        raw = request.data.get("data", "{}")
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data, request.FILES.getlist("attachments")
    return request.data, []


class CompanyScopedViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def company(self):
        return _company(self.request)

    def perform_create(self, serializer):
        serializer.save(
            company=self.company(),
            created_by=self.request.user,
            updated_by=self.request.user,
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class ReturnableGatePassViewSet(CompanyScopedViewSet):
    def get_permissions(self):
        base = [IsAuthenticated(), HasCompanyContext()]
        action_permission = {
            "create": CanManageReturnable,
            # Editing is shared between the department (its own draft) and the
            # approver (a pass waiting on them); the serializer picks which.
            "update": CanEditReturnable,
            "partial_update": CanEditReturnable,
            "destroy": CanManageReturnable,
            "submit": CanSubmitReturnable,
            "approve": CanApproveReturnable,
            "reject": CanApproveReturnable,
            "pending_approval": CanApproveReturnable,
            "gate_out": CanGateOutReturnable,
            "reject_at_gate": CanRejectReturnableAtGate,
            "record_return": CanGateInReturnable,
            "acknowledge": CanAcknowledgeReturnable,
            "close": CanCloseReturnable,
            "short_close": CanShortCloseReturnable,
            "cancel": CanCancelReturnable,
            "pending_gate_out": CanGateOutReturnable,
            "pending_gate_in": CanGateInReturnable,
        }.get(self.action, CanViewReturnableAtGate)
        return base + [action_permission()]

    def get_serializer_class(self):
        if self.action == "list":
            return ReturnableGatePassListSerializer
        return ReturnableGatePassSerializer

    def get_queryset(self):
        queryset = (
            ReturnableGatePass.objects.filter(company=self.company())
            .select_related(
                "department",
                "asset",
                "work_order",
                "vehicle",
                "driver",
                "transporter",
                "submitted_by",
                "gate_out_by",
                "closed_by",
                "created_by",
                "updated_by",
            )
            .prefetch_related(
                "items",
                "attachments",
                "return_events__lines",
                "return_events__attachments",
                "source_material_indents",
            )
            .annotate(item_count=Count("items", distinct=True))
            # Newest first. Explicit, because a DISTINCT + GROUP BY query does not
            # reliably inherit the model's Meta.ordering.
            .order_by("-created_at")
        )

        params = self.request.query_params

        status_param = params.get("status")
        if status_param and status_param != "ALL":
            queryset = queryset.filter(status__in=status_param.split(","))

        purpose = params.get("purpose")
        if purpose and purpose != "ALL":
            queryset = queryset.filter(purpose=purpose)

        department = params.get("department")
        if department:
            queryset = queryset.filter(department_id=department)

        party = params.get("party")
        if party:
            queryset = queryset.filter(party_name__icontains=party)

        overdue = params.get("overdue")
        if overdue is not None and overdue != "":
            queryset = queryset.filter(is_overdue=_is_true(overdue))

        is_returnable = params.get("is_returnable")
        if is_returnable is not None and is_returnable not in ("", "ALL"):
            queryset = queryset.filter(is_returnable=_is_true(is_returnable))

        date_from = parse_date(params.get("expected_return_from") or "")
        if date_from:
            queryset = queryset.filter(expected_return_date__gte=date_from)

        date_to = parse_date(params.get("expected_return_to") or "")
        if date_to:
            queryset = queryset.filter(expected_return_date__lte=date_to)

        search = params.get("q") or params.get("search")
        if search:
            queryset = queryset.filter(
                Q(pass_no__icontains=search)
                | Q(party_name__icontains=search)
                | Q(recipient_name__icontains=search)
                | Q(items__item_name__icontains=search)
                | Q(items__item_code__icontains=search)
                | Q(items__serial_no__icontains=search)
            ).distinct()

        return queryset

    def perform_destroy(self, instance):
        if instance.status != ReturnableStatus.DRAFT:
            raise ValidationError("Only a draft gate pass can be deleted.")
        instance.delete()

    # -- helpers ----------------------------------------------------------

    def _reject(self, message):
        return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)

    def _detail_response(self, gate_pass):
        gate_pass.refresh_from_db()
        serializer = ReturnableGatePassSerializer(gate_pass, context=self.get_serializer_context())
        return Response(serializer.data)

    # -- stage 1: department submits for approval --------------------------

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        """Send the draft to the higher authority. The gate does not see it yet."""
        gate_pass = self.get_object()
        if gate_pass.status != ReturnableStatus.DRAFT:
            return self._reject("Only a draft gate pass can be submitted.")
        if not gate_pass.items.exists():
            return self._reject("Add at least one item before submitting.")
        if not gate_pass.has_photo:
            return self._reject(NO_PHOTO_OUT)

        gate_pass.status = ReturnableStatus.PENDING_APPROVAL
        gate_pass.submitted_by = request.user
        gate_pass.submitted_at = timezone.now()
        gate_pass.rejected_reason = ""
        gate_pass.approval_rejected_reason = ""
        gate_pass.save(
            update_fields=[
                "status",
                "submitted_by",
                "submitted_at",
                "rejected_reason",
                "approval_rejected_reason",
                "updated_at",
            ]
        )
        gate_pass.log(ReturnableLogAction.SUBMITTED, actor=request.user)
        notify.notify_submitted(gate_pass, actor=request.user)
        return self._detail_response(gate_pass)

    # -- stage 2: higher authority approves or sends it back ---------------

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Sign off the pass. Only now does it reach the gate's queue."""
        gate_pass = self.get_object()
        if gate_pass.status != ReturnableStatus.PENDING_APPROVAL:
            return self._reject("Only a pass awaiting approval can be approved.")
        if gate_pass.submitted_by_id == request.user.id:
            return self._reject("You cannot approve a gate pass you submitted yourself.")
        # Passes submitted before a photo was required can still be waiting here.
        # The approver may edit the pass, so they can add the photo themselves.
        if not gate_pass.has_photo:
            return self._reject(NO_PHOTO_OUT)

        remarks = (request.data.get("remarks") or "").strip()

        gate_pass.status = ReturnableStatus.PENDING_GATE_OUT
        gate_pass.approved_by = request.user
        gate_pass.approved_at = timezone.now()
        gate_pass.updated_by = request.user
        gate_pass.save(
            update_fields=["status", "approved_by", "approved_at", "updated_by", "updated_at"]
        )
        gate_pass.log(ReturnableLogAction.APPROVED, actor=request.user, note=remarks)
        notify.notify_approved(gate_pass, actor=request.user)
        return self._detail_response(gate_pass)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """Approver sends the pass back to the department as a draft."""
        gate_pass = self.get_object()
        if gate_pass.status != ReturnableStatus.PENDING_APPROVAL:
            return self._reject("Only a pass awaiting approval can be rejected.")

        serializer = ReasonInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]

        gate_pass.status = ReturnableStatus.DRAFT
        gate_pass.approval_rejected_reason = reason
        gate_pass.submitted_by = None
        gate_pass.submitted_at = None
        gate_pass.updated_by = request.user
        gate_pass.save(
            update_fields=[
                "status",
                "approval_rejected_reason",
                "submitted_by",
                "submitted_at",
                "updated_by",
                "updated_at",
            ]
        )
        gate_pass.log(ReturnableLogAction.APPROVAL_REJECTED, actor=request.user, note=reason)
        notify.notify_approval_rejected(gate_pass, reason, actor=request.user)
        return self._detail_response(gate_pass)

    # -- stage 3: gate fills vehicle details and lets it out ---------------

    @action(detail=True, methods=["post"], url_path="gate-out")
    @transaction.atomic
    def gate_out(self, request, pk=None):
        gate_pass = self.get_object()
        if gate_pass.status != ReturnableStatus.PENDING_GATE_OUT:
            return self._reject("This gate pass is not waiting for gate out.")

        serializer = GateOutInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        # Lines picked from Store / Spares leave the shelf as they leave the gate.
        store_lines = [
            (item.spare_id, item.quantity_out) for item in gate_pass.items.all() if item.spare_id
        ]
        if store_lines:
            try:
                store_stock.take_out(
                    store_lines,
                    user=request.user,
                    remarks=f"Gate pass {gate_pass.pass_no} ({gate_pass.destination})",
                )
            except store_stock.NotEnoughInStore as short:
                return self._reject(
                    f"The store shows only {short.in_store} {short.spare.uom} of "
                    f"{short.spare.name}. Ask the store to count it again."
                )

        gate_pass.vehicle_id = payload.get("vehicle")
        gate_pass.driver_id = payload.get("driver")
        gate_pass.transporter_id = payload.get("transporter")
        gate_pass.vehicle_number_manual = payload.get("vehicle_number_manual", "")
        gate_pass.driver_name_manual = payload.get("driver_name_manual", "")
        gate_pass.driver_mobile = payload.get("driver_mobile", "")
        gate_pass.security_name = payload.get("security_name", "")
        gate_pass.out_remarks = payload.get("out_remarks", "")
        gate_pass.is_hand_carried = payload.get("is_hand_carried", False)
        gate_pass.carried_by_name = payload.get("carried_by_name", "")
        gate_pass.gate_out_by = request.user
        gate_pass.gate_out_at = timezone.now()
        gate_pass.updated_by = request.user

        # Nothing is coming back on a non-returnable pass, so gate out is the end
        # of its life. Closing it here keeps it out of the gate-in queue, the
        # overdue job and the outstanding-material reports.
        if gate_pass.is_returnable:
            gate_pass.status = ReturnableStatus.OUT
        else:
            gate_pass.status = ReturnableStatus.CLOSED
            gate_pass.closed_by = request.user
            gate_pass.closed_at = timezone.now()

        gate_pass.save()

        if gate_pass.is_hand_carried:
            note = f"Hand-carried out by {gate_pass.carried_by_name}"
        else:
            vehicle_number = (
                gate_pass.vehicle_number_manual
                or (gate_pass.vehicle and gate_pass.vehicle.vehicle_number)
                or "N/A"
            )
            note = f"Vehicle {vehicle_number}"
        gate_pass.log(ReturnableLogAction.GATE_OUT, actor=request.user, note=note)
        notify.notify_gate_out(gate_pass, actor=request.user)

        if not gate_pass.is_returnable:
            gate_pass.log(
                ReturnableLogAction.CLOSED,
                actor=request.user,
                note="Non-returnable pass closed on gate out.",
            )
            notify.notify_closed(gate_pass, actor=request.user)

        return self._detail_response(gate_pass)

    @action(detail=True, methods=["post"], url_path="reject-at-gate")
    def reject_at_gate(self, request, pk=None):
        """Gate found a mismatch. Back to the department rather than silently editing."""
        gate_pass = self.get_object()
        if gate_pass.status != ReturnableStatus.PENDING_GATE_OUT:
            return self._reject("Only a pass waiting for gate out can be rejected.")

        serializer = ReasonInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]

        gate_pass.status = ReturnableStatus.DRAFT
        gate_pass.rejected_reason = reason
        gate_pass.submitted_by = None
        gate_pass.submitted_at = None
        gate_pass.updated_by = request.user
        gate_pass.save(
            update_fields=[
                "status",
                "rejected_reason",
                "submitted_by",
                "submitted_at",
                "updated_by",
                "updated_at",
            ]
        )
        gate_pass.log(ReturnableLogAction.REJECTED_AT_GATE, actor=request.user, note=reason)
        notify.notify_rejected_at_gate(gate_pass, reason, actor=request.user)
        return self._detail_response(gate_pass)

    # -- stage 3: gate records the return ---------------------------------

    @action(detail=True, methods=["post"], url_path="record-return")
    @transaction.atomic
    def record_return(self, request, pk=None):
        gate_pass = self.get_object()
        if not gate_pass.is_returnable:
            return self._reject("This is a non-returnable gate pass — nothing is coming back.")
        if gate_pass.status not in OUTSTANDING_STATUSES:
            return self._reject("Only items that are out can be returned.")

        try:
            data, photos = _split_multipart(request)
        except json.JSONDecodeError:
            return self._reject("The 'data' part is not valid JSON.")

        serializer = RecordReturnInputSerializer(
            data=data,
            context={**self.get_serializer_context(), "gate_pass": gate_pass},
        )
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        if not photos:
            return self._reject(NO_PHOTO_BACK)
        not_photos = [photo.name for photo in photos if not is_photo_file(photo.name)]
        if not_photos:
            return self._reject(
                f"Only photos can be attached to a return. Not a photo: {', '.join(not_photos)}."
            )

        next_event_no = (gate_pass.return_events.count() or 0) + 1
        event = ReturnableReturnEvent.objects.create(
            company=gate_pass.company,
            gate_pass=gate_pass,
            event_no=next_event_no,
            event_ref=f"{gate_pass.pass_no}-R{next_event_no}",
            returned_at=payload.get("returned_at") or timezone.now(),
            vehicle_id=payload.get("vehicle"),
            driver_id=payload.get("driver"),
            transporter_id=payload.get("transporter"),
            vehicle_number_manual=payload.get("vehicle_number_manual", ""),
            driver_name_manual=payload.get("driver_name_manual", ""),
            driver_mobile=payload.get("driver_mobile", ""),
            security_name=payload.get("security_name", ""),
            remarks=payload.get("remarks", ""),
            verified_by=request.user,
            verified_at=timezone.now(),
            created_by=request.user,
            updated_by=request.user,
        )

        items = {item.id: item for item in gate_pass.items.all()}
        for line in payload["lines"]:
            pass_item = items[line["pass_item"]]
            ReturnableReturnEventItem.objects.create(
                company=gate_pass.company,
                event=event,
                pass_item=pass_item,
                quantity_returned=line["quantity_returned"],
                return_condition=line.get("return_condition") or ItemReturnCondition.OK,
                remarks=line.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )
            pass_item.recalculate_returned()
            condition = line.get("return_condition") or ItemReturnCondition.OK
            # A store line comes back onto the shelf -- unless it came back as
            # scrap, which is not stock anyone can use.
            if pass_item.spare_id and condition != ItemReturnCondition.SCRAP:
                note = f"Back from gate pass {gate_pass.pass_no}"
                if condition != ItemReturnCondition.OK:
                    note = f"{note} ({ItemReturnCondition(condition).label})"
                store_stock.put_back(
                    pass_item.spare_id, line["quantity_returned"], user=request.user, remarks=note
                )

        for photo in photos:
            ReturnableReturnEventAttachment.objects.create(
                company=gate_pass.company,
                event=event,
                file=photo,
                created_by=request.user,
                updated_by=request.user,
            )

        gate_pass.last_return_at = event.returned_at
        gate_pass.refresh_status()
        gate_pass.updated_by = request.user
        gate_pass.save(
            update_fields=["status", "is_overdue", "last_return_at", "updated_by", "updated_at"]
        )

        gate_pass.log(
            ReturnableLogAction.RETURN_RECORDED,
            actor=request.user,
            note=f"{event.event_ref}: {len(payload['lines'])} line(s) returned, {len(photos)} photo(s)",
            meta={"event_id": event.id, "event_no": event.event_no, "photos": len(photos)},
        )
        notify.notify_return_recorded(gate_pass, event, actor=request.user)
        return self._detail_response(gate_pass)

    # -- stage 4: department collects, then closes -------------------------

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        """Department has physically collected the returned material from the gate.

        Acknowledges one return event (``event`` in the body) or, if omitted,
        every event still outstanding.
        """
        gate_pass = self.get_object()
        event_id = request.data.get("event")

        events = gate_pass.return_events.filter(acknowledged_at__isnull=True)
        if event_id:
            events = events.filter(id=event_id)
        events = list(events)

        if not events:
            return self._reject("There is nothing waiting to be acknowledged on this gate pass.")

        now = timezone.now()
        for event in events:
            event.acknowledged_by = request.user
            event.acknowledged_at = now
            event.updated_by = request.user
            event.save(update_fields=["acknowledged_by", "acknowledged_at", "updated_by", "updated_at"])
            gate_pass.log(
                ReturnableLogAction.ACKNOWLEDGED,
                actor=request.user,
                note=f"{event.event_ref} collected",
                meta={"event_id": event.id},
            )
            notify.notify_acknowledged(gate_pass, event, actor=request.user)

        return self._detail_response(gate_pass)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        gate_pass = self.get_object()
        if gate_pass.status != ReturnableStatus.RETURNED:
            return self._reject(
                "A gate pass can only be closed once every item is back. "
                "Use short close if some items will never return."
            )
        if gate_pass.return_events.filter(acknowledged_at__isnull=True).exists():
            return self._reject(
                "Acknowledge receipt of every returned consignment before closing the pass."
            )

        gate_pass.status = ReturnableStatus.CLOSED
        gate_pass.closed_by = request.user
        gate_pass.closed_at = timezone.now()
        gate_pass.is_overdue = False
        gate_pass.updated_by = request.user
        gate_pass.save(
            update_fields=["status", "closed_by", "closed_at", "is_overdue", "updated_by", "updated_at"]
        )
        gate_pass.log(ReturnableLogAction.CLOSED, actor=request.user)
        notify.notify_closed(gate_pass, actor=request.user)
        return self._detail_response(gate_pass)

    @action(detail=True, methods=["post"], url_path="short-close")
    def short_close(self, request, pk=None):
        """Close a pass whose items will never come back — scrapped, lost, written off.

        The unreturned quantity stays on the lines so the register still shows it.
        """
        gate_pass = self.get_object()
        if not gate_pass.is_returnable:
            return self._reject("A non-returnable pass closes on gate out and cannot be short closed.")
        if gate_pass.status not in OUTSTANDING_STATUSES:
            return self._reject("Only a pass with items still outstanding can be short closed.")

        serializer = ReasonInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]

        gate_pass.status = ReturnableStatus.CLOSED
        gate_pass.short_close_reason = reason
        gate_pass.closed_by = request.user
        gate_pass.closed_at = timezone.now()
        gate_pass.is_overdue = False
        gate_pass.updated_by = request.user
        gate_pass.save(
            update_fields=[
                "status",
                "short_close_reason",
                "closed_by",
                "closed_at",
                "is_overdue",
                "updated_by",
                "updated_at",
            ]
        )
        gate_pass.log(ReturnableLogAction.SHORT_CLOSED, actor=request.user, note=reason)
        notify.notify_closed(gate_pass, short_closed=True, actor=request.user)
        return self._detail_response(gate_pass)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def cancel(self, request, pk=None):
        gate_pass = self.get_object()
        if gate_pass.status not in CANCELLABLE_STATUSES:
            return self._reject("This gate pass can no longer be cancelled.")
        if gate_pass.return_events.exists():
            return self._reject(
                "Items have already started coming back. Short close the pass instead."
            )

        serializer = ReasonInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]

        # Cancelling after gate out voids the pass (nothing has come back yet --
        # that is short close), so what it took from the store is put back.
        if gate_pass.gate_out_at:
            for item in gate_pass.items.all():
                if item.spare_id:
                    store_stock.put_back(
                        item.spare_id,
                        item.quantity_out,
                        user=request.user,
                        remarks=f"Gate pass {gate_pass.pass_no} cancelled",
                    )

        gate_pass.status = ReturnableStatus.CANCELLED
        gate_pass.cancel_reason = reason
        gate_pass.cancelled_by = request.user
        gate_pass.cancelled_at = timezone.now()
        gate_pass.is_overdue = False
        gate_pass.updated_by = request.user
        gate_pass.save(
            update_fields=[
                "status",
                "cancel_reason",
                "cancelled_by",
                "cancelled_at",
                "is_overdue",
                "updated_by",
                "updated_at",
            ]
        )
        gate_pass.log(ReturnableLogAction.CANCELLED, actor=request.user, note=reason)
        notify.notify_cancelled(gate_pass, actor=request.user)
        return self._detail_response(gate_pass)

    # -- reads ------------------------------------------------------------

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        gate_pass = self.get_object()
        serializer = ReturnableGatePassLogSerializer(
            gate_pass.logs.select_related("actor"), many=True
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="pending-approval")
    def pending_approval(self, request):
        """The approver's queue."""
        queryset = self.get_queryset().filter(status=ReturnableStatus.PENDING_APPROVAL)
        serializer = ReturnableGatePassListSerializer(
            queryset, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="pending-gate-out")
    def pending_gate_out(self, request):
        queryset = self.get_queryset().filter(status=ReturnableStatus.PENDING_GATE_OUT)
        serializer = ReturnableGatePassListSerializer(
            queryset, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="pending-gate-in")
    def pending_gate_in(self, request):
        # Non-returnable passes are already closed and must never appear here.
        queryset = (
            self.get_queryset()
            .filter(is_returnable=True, status__in=OUTSTANDING_STATUSES)
            .order_by("expected_return_date")
        )
        serializer = ReturnableGatePassListSerializer(
            queryset, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)


class ReturnableGatePassItemViewSet(CompanyScopedViewSet):
    serializer_class = ReturnableGatePassItemSerializer

    def get_permissions(self):
        base = [IsAuthenticated(), HasCompanyContext()]
        if self.action in ("create", "update", "partial_update", "destroy"):
            return base + [CanManageReturnable()]
        return base + [CanViewReturnableAtGate()]

    def _refuse_unless_editable(self, item):
        # Past approval the gate works off the pass as printed, and a store
        # line's stock moves at gate out -- a line changed then would not match.
        if item.gate_pass.status not in (ReturnableStatus.DRAFT, ReturnableStatus.PENDING_APPROVAL):
            raise ValidationError(
                {"status": "Only a draft, or a pass still waiting for approval, can be edited."}
            )

    def perform_update(self, serializer):
        self._refuse_unless_editable(serializer.instance)
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._refuse_unless_editable(instance)
        instance.delete()

    def get_queryset(self):
        queryset = ReturnableGatePassItem.objects.filter(company=self.company())
        gate_pass = self.request.query_params.get("gate_pass")
        if gate_pass:
            queryset = queryset.filter(gate_pass_id=gate_pass)
        return queryset.select_related("gate_pass", "spare", "asset")


class ReturnableReturnEventViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ReturnableReturnEventSerializer
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReturnableAtGate]

    def get_queryset(self):
        queryset = ReturnableReturnEvent.objects.filter(company=_company(self.request))
        gate_pass = self.request.query_params.get("gate_pass")
        if gate_pass:
            queryset = queryset.filter(gate_pass_id=gate_pass)
        return queryset.select_related("vehicle", "driver", "verified_by", "acknowledged_by").prefetch_related(
            "lines__pass_item", "attachments"
        )


class ReturnableGatePassAttachmentViewSet(CompanyScopedViewSet):
    serializer_class = ReturnableGatePassAttachmentSerializer

    def get_permissions(self):
        base = [IsAuthenticated(), HasCompanyContext()]
        if self.action in ("create", "destroy"):
            # Attachments are part of the pass form, so whoever may edit a pass
            # may also swap its documents — the approver included.
            return base + [CanEditReturnable()]
        return base + [CanViewReturnableAtGate()]

    def get_queryset(self):
        queryset = ReturnableGatePassAttachment.objects.filter(company=self.company())
        gate_pass = self.request.query_params.get("gate_pass")
        if gate_pass:
            queryset = queryset.filter(gate_pass_id=gate_pass)
        return queryset.select_related("gate_pass", "created_by")


# ---------------------------------------------------------------------------
# Dashboard / reports / options
# ---------------------------------------------------------------------------


class ReturnableDashboardView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReturnableAtGate]

    def get(self, request):
        queryset = ReturnableGatePass.objects.filter(company=_company(request))

        counts = {row["status"]: row["total"] for row in queryset.values("status").annotate(total=Count("id"))}
        # Only a returnable pass can be outstanding; a non-returnable one closes
        # at the gate.
        outstanding = queryset.filter(is_returnable=True, status__in=OUTSTANDING_STATUSES)

        today = timezone.localdate()
        ageing = {
            "0_7": outstanding.filter(
                expected_return_date__gte=today - timedelta(days=7),
                expected_return_date__lt=today,
            ).count(),
            "8_15": outstanding.filter(
                expected_return_date__gte=today - timedelta(days=15),
                expected_return_date__lt=today - timedelta(days=7),
            ).count(),
            "16_30": outstanding.filter(
                expected_return_date__gte=today - timedelta(days=30),
                expected_return_date__lt=today - timedelta(days=15),
            ).count(),
            "30_plus": outstanding.filter(expected_return_date__lt=today - timedelta(days=30)).count(),
        }

        return Response(
            {
                "status_counts": {choice.value: counts.get(choice.value, 0) for choice in ReturnableStatus},
                "overdue_count": queryset.filter(is_overdue=True).count(),
                "due_today_count": outstanding.filter(expected_return_date=today).count(),
                "outstanding_count": outstanding.count(),
                "pending_approval_count": queryset.filter(
                    status=ReturnableStatus.PENDING_APPROVAL
                ).count(),
                "pending_gate_out_count": queryset.filter(status=ReturnableStatus.PENDING_GATE_OUT).count(),
                "returnable_count": queryset.filter(is_returnable=True).count(),
                "non_returnable_count": queryset.filter(is_returnable=False).count(),
                "outstanding_value": outstanding.aggregate(total=Sum("items__estimated_value"))["total"] or 0,
                "ageing_buckets": ageing,
            }
        )


class ReturnableReportsView(APIView):
    """CSV register, overdue/ageing, party-wise and item-wise pending."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReturnableReports]

    def get(self, request):
        company = _company(request)
        report_type = request.query_params.get("type", "register")
        export = _is_true(request.query_params.get("export", "false"))

        if report_type == "party_wise":
            rows = list(
                ReturnableGatePass.objects.filter(company=company, status__in=OUTSTANDING_STATUSES)
                .values("party_name")
                .annotate(
                    passes=Count("id", distinct=True),
                    quantity_out=Sum("items__quantity_out"),
                    quantity_returned=Sum("items__quantity_returned"),
                )
                .order_by("-passes")
            )
        elif report_type == "item_wise":
            rows = list(
                ReturnableGatePassItem.objects.filter(
                    company=company, gate_pass__status__in=OUTSTANDING_STATUSES
                )
                .values(
                    "item_name",
                    "serial_no",
                    "uom",
                    "gate_pass__pass_no",
                    "gate_pass__party_name",
                    "gate_pass__expected_return_date",
                    "quantity_out",
                    "quantity_returned",
                )
                .order_by("gate_pass__expected_return_date")
            )
        elif report_type == "overdue":
            rows = list(
                ReturnableGatePass.objects.filter(company=company, is_overdue=True)
                .values(
                    "pass_no",
                    "party_name",
                    "purpose",
                    "expected_return_date",
                    "status",
                    "department__name",
                )
                .order_by("expected_return_date")
            )
        else:
            queryset = ReturnableGatePass.objects.filter(company=company)
            date_from = parse_date(request.query_params.get("date_from") or "")
            date_to = parse_date(request.query_params.get("date_to") or "")
            if date_from:
                queryset = queryset.filter(created_at__date__gte=date_from)
            if date_to:
                queryset = queryset.filter(created_at__date__lte=date_to)
            status_param = request.query_params.get("status")
            if status_param and status_param != "ALL":
                queryset = queryset.filter(status__in=status_param.split(","))
            rows = list(
                queryset.values(
                    "pass_no",
                    "status",
                    "purpose",
                    "party_name",
                    "department__name",
                    "expected_return_date",
                    "gate_out_at",
                    "last_return_at",
                    "closed_at",
                    "is_overdue",
                ).order_by("-created_at")
            )

        if not export:
            return Response({"type": report_type, "rows": rows, "count": len(rows)})

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="returnable_{report_type}.csv"'
        if rows:
            writer = csv.DictWriter(response, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return response


class ReturnableSapItemSearchView(APIView):
    """Omni-search over the SAP item master (``OITM``) for the item picker.

    Reads live from HANA — there is no local item-master cache anywhere in this
    backend. Mounted here rather than reusing the production-execution or QC
    search so a department clerk does not need those modules' permissions.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReturnableAtGate]

    MIN_SEARCH_LENGTH = 2
    DEFAULT_LIMIT = 50
    MAX_LIMIT = 100

    def get(self, request):
        search = (request.query_params.get("search") or "").strip()
        if len(search) < self.MIN_SEARCH_LENGTH:
            return Response([])

        try:
            limit = int(request.query_params.get("limit", self.DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = self.DEFAULT_LIMIT
        limit = max(1, min(limit, self.MAX_LIMIT))

        from production_execution.services.sap_reader import ProductionOrderReader

        try:
            reader = ProductionOrderReader(_company(request).code)
            rows = reader.search_items(search=search, limit=limit)
        except Exception as exc:  # SAP being down must not 500 the form
            logger.error("[Returnable] SAP item search failed: %s", exc, exc_info=True)
            return Response(
                {"detail": "SAP item search is unavailable. Enter the item manually."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            [
                {
                    "item_code": row.get("ItemCode") or "",
                    "item_name": row.get("ItemName") or "",
                    "uom": row.get("UomCode") or "",
                }
                for row in rows
            ]
        )


class ReturnableStoreItemSearchView(APIView):
    """Store / Spares items for the gate pass's "From store" picker.

    Mounted here, like the SAP search, so a department clerk raising a pass
    does not need the Store module's own permissions. Every item is offered
    whatever its stock -- until the store's real stock is entered, a pass may
    take it below zero -- and one the store does not have yet can be added
    (POST), so its line still comes off the store at gate out.
    """

    LIMIT = 50

    def get_permissions(self):
        base = [IsAuthenticated(), HasCompanyContext()]
        if self.request.method == "POST":
            # Whoever may fill in a pass's lines: the department, or its approver.
            return base + [CanEditReturnable()]
        return base + [CanViewReturnableAtGate()]

    @staticmethod
    def _row(item):
        return {
            "id": item.id,
            "name": item.name,
            "uom": item.uom,
            "current_stock": str(item.current_stock),
            "storage_location": item.storage_location,
        }

    def get(self, request):
        items = MaintenanceSpare.objects.filter(company=_company(request), is_active=True)
        search = (request.query_params.get("search") or "").strip()
        if search:
            items = items.filter(
                Q(name__icontains=search)
                | Q(part_number__icontains=search)
                | Q(storage_location__icontains=search)
            )
        return Response([self._row(item) for item in items.order_by("name")[: self.LIMIT]])

    def post(self, request):
        """Add an item to the store at 0, or hand back the one already named so."""
        company = _company(request)
        name = (request.data.get("name") or "").strip()[:200]
        uom = (request.data.get("uom") or "").strip().upper()[:30] or "NOS"
        if not name:
            return Response({"name": "Write the item name."}, status=status.HTTP_400_BAD_REQUEST)
        existing = MaintenanceSpare.objects.filter(
            company=company, is_active=True, name__iexact=name
        ).first()
        if existing:
            return Response(self._row(existing))
        category, _ = SpareCategory.objects.get_or_create(
            company=company,
            name="General",
            defaults={"created_by": request.user, "updated_by": request.user},
        )
        item = MaintenanceSpare.objects.create(
            company=company,
            category=category,
            name=name,
            part_number=unique_spare_part_number(company, name),
            uom=uom,
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(self._row(item), status=status.HTTP_201_CREATED)


class ReturnableOptionsView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReturnableAtGate]

    def get(self, request):
        def as_options(choices):
            return [{"value": value, "label": label} for value, label in choices]

        return Response(
            {
                "statuses": as_options(ReturnableStatus.choices),
                "purposes": as_options(ReturnablePurpose.choices),
                "conditions_out": as_options(ItemConditionOut.choices),
                "return_conditions": as_options(ItemReturnCondition.choices),
            }
        )
