"""Serializers for the Returnable Items module."""

from decimal import Decimal

from django.db import transaction
from django.db.models import F
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from .constants import ReturnableLogAction, ReturnableStatus
from .models import (
    ReturnableGatePass,
    ReturnableGatePassAttachment,
    ReturnableGatePassItem,
    ReturnableGatePassLog,
    ReturnableGatePassSequence,
    ReturnableReturnEvent,
    ReturnableReturnEventItem,
)

ZERO = Decimal("0.000")

MANAGE_PERM = "returnable_items.can_manage_returnable_gatepass"
APPROVE_PERM = "returnable_items.can_approve_returnable_gatepass"


class CompanyScopedModelSerializer(serializers.ModelSerializer):
    company = serializers.IntegerField(source="company_id", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default="")
    updated_by_name = serializers.CharField(source="updated_by.full_name", read_only=True, default="")

    def _company(self):
        request = self.context.get("request")
        return request.company.company if request and hasattr(request, "company") else None


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


class ReturnableGatePassItemSerializer(CompanyScopedModelSerializer):
    pending_return_qty = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    is_fully_returned = serializers.BooleanField(read_only=True)
    condition_out_display = serializers.CharField(source="get_condition_out_display", read_only=True)

    class Meta:
        model = ReturnableGatePassItem
        fields = [
            "id",
            "company",
            "gate_pass",
            "line_num",
            "item_code",
            "item_name",
            "description",
            "serial_no",
            "make_model",
            "spare",
            "asset",
            "uom",
            "quantity_out",
            "quantity_returned",
            "condition_out",
            "condition_out_display",
            "estimated_value",
            "remarks",
            "pending_return_qty",
            "is_fully_returned",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["quantity_returned", "gate_pass"]


class ReturnableGatePassItemInputSerializer(serializers.Serializer):
    """Write shape for a line, used nested under the gate pass."""

    id = serializers.IntegerField(required=False)
    item_code = serializers.CharField(required=False, allow_blank=True, default="")
    item_name = serializers.CharField(max_length=250)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    serial_no = serializers.CharField(required=False, allow_blank=True, default="")
    make_model = serializers.CharField(required=False, allow_blank=True, default="")
    spare = serializers.IntegerField(required=False, allow_null=True)
    asset = serializers.IntegerField(required=False, allow_null=True)
    uom = serializers.CharField(required=False, allow_blank=True, default="NOS")
    quantity_out = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    condition_out = serializers.CharField(required=False, allow_blank=True)
    estimated_value = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class ReturnableReturnEventItemSerializer(CompanyScopedModelSerializer):
    item_name = serializers.CharField(source="pass_item.item_name", read_only=True)
    serial_no = serializers.CharField(source="pass_item.serial_no", read_only=True)
    uom = serializers.CharField(source="pass_item.uom", read_only=True)
    return_condition_display = serializers.CharField(source="get_return_condition_display", read_only=True)

    class Meta:
        model = ReturnableReturnEventItem
        fields = [
            "id",
            "company",
            "event",
            "pass_item",
            "item_name",
            "serial_no",
            "uom",
            "quantity_returned",
            "return_condition",
            "return_condition_display",
            "remarks",
            "created_at",
        ]


class ReturnableReturnEventSerializer(CompanyScopedModelSerializer):
    lines = ReturnableReturnEventItemSerializer(many=True, read_only=True)
    vehicle_number = serializers.CharField(read_only=True)
    verified_by_name = serializers.CharField(source="verified_by.full_name", read_only=True, default="")
    acknowledged_by_name = serializers.CharField(
        source="acknowledged_by.full_name", read_only=True, default=""
    )
    is_acknowledged = serializers.BooleanField(read_only=True)

    class Meta:
        model = ReturnableReturnEvent
        fields = [
            "id",
            "company",
            "gate_pass",
            "event_no",
            "event_ref",
            "returned_at",
            "vehicle",
            "driver",
            "transporter",
            "vehicle_number",
            "vehicle_number_manual",
            "driver_name_manual",
            "driver_mobile",
            "security_name",
            "verified_by",
            "verified_by_name",
            "verified_at",
            "acknowledged_by",
            "acknowledged_by_name",
            "acknowledged_at",
            "is_acknowledged",
            "remarks",
            "lines",
            "created_at",
        ]


class ReturnableGatePassAttachmentSerializer(CompanyScopedModelSerializer):
    doc_type_display = serializers.CharField(source="get_doc_type_display", read_only=True)

    class Meta:
        model = ReturnableGatePassAttachment
        fields = [
            "id",
            "company",
            "gate_pass",
            "file",
            "doc_type",
            "doc_type_display",
            "caption",
            "created_at",
            "created_by_name",
        ]

    def validate_gate_pass(self, value):
        company = self._company()
        if company and value.company_id != company.id:
            raise serializers.ValidationError("Gate pass does not belong to the current company.")
        return value


class ReturnableGatePassLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.full_name", read_only=True, default="")
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = ReturnableGatePassLog
        fields = ["id", "action", "action_display", "actor", "actor_name", "at", "note", "meta"]


# ---------------------------------------------------------------------------
# Gate pass
# ---------------------------------------------------------------------------


class ReturnableGatePassListSerializer(CompanyScopedModelSerializer):
    """Light shape for list screens and gate queues."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    purpose_display = serializers.CharField(source="get_purpose_display", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True, default="")
    item_count = serializers.IntegerField(read_only=True)
    item_names = serializers.SerializerMethodField()
    days_overdue = serializers.IntegerField(read_only=True)
    pending_return_qty = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    destination = serializers.CharField(read_only=True)
    #: Set when this pass was auto-generated from a maintenance Material Indent —
    #: lets the gate show its origin as "Material Indent" in the Type column.
    material_indent_no = serializers.SerializerMethodField()

    def get_item_names(self, obj):
        # Comma-joined item names for the list view. `items` is prefetched by the
        # viewset, so this adds no extra query.
        return ", ".join(item.item_name for item in obj.items.all())

    def get_material_indent_no(self, obj):
        indents = list(obj.source_material_indents.all())
        return indents[0].indent_no if indents else ""

    class Meta:
        model = ReturnableGatePass
        fields = [
            "id",
            "company",
            "pass_no",
            "status",
            "status_display",
            "is_returnable",
            "material_indent_no",
            "purpose",
            "purpose_display",
            "department",
            "department_name",
            "party_name",
            "recipient_name",
            "destination",
            "item_names",
            "expected_return_date",
            "is_overdue",
            "days_overdue",
            "item_count",
            "pending_return_qty",
            "gate_out_at",
            "last_return_at",
            "created_at",
            "created_by_name",
        ]


class ReturnableGatePassSerializer(CompanyScopedModelSerializer):
    items = ReturnableGatePassItemSerializer(many=True, read_only=True)
    return_events = ReturnableReturnEventSerializer(many=True, read_only=True)
    attachments = ReturnableGatePassAttachmentSerializer(many=True, read_only=True)

    items_input = ReturnableGatePassItemInputSerializer(many=True, write_only=True, required=False)

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    purpose_display = serializers.CharField(source="get_purpose_display", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True, default="")
    asset_name = serializers.CharField(source="asset.name", read_only=True, default="")
    work_order_no = serializers.CharField(source="work_order.work_order_no", read_only=True, default="")
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True, default="")
    driver_name = serializers.CharField(source="driver.name", read_only=True, default="")
    transporter_name = serializers.CharField(source="transporter.name", read_only=True, default="")

    submitted_by_name = serializers.CharField(source="submitted_by.full_name", read_only=True, default="")
    approved_by_name = serializers.CharField(source="approved_by.full_name", read_only=True, default="")
    gate_out_by_name = serializers.CharField(source="gate_out_by.full_name", read_only=True, default="")
    closed_by_name = serializers.CharField(source="closed_by.full_name", read_only=True, default="")

    recipient_display_name = serializers.CharField(source="recipient.full_name", read_only=True, default="")
    destination = serializers.CharField(read_only=True)
    material_indent_no = serializers.SerializerMethodField()

    def get_material_indent_no(self, obj):
        indents = list(obj.source_material_indents.all())
        return indents[0].indent_no if indents else ""

    total_estimated_value = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    total_quantity_out = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    total_quantity_returned = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    pending_return_qty = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    days_overdue = serializers.IntegerField(read_only=True)
    is_fully_returned = serializers.BooleanField(read_only=True)

    class Meta:
        model = ReturnableGatePass
        fields = [
            "id",
            "company",
            "pass_no",
            "status",
            "status_display",
            "is_returnable",
            "material_indent_no",
            "department",
            "department_name",
            "requested_by_name",
            "contact_no",
            "purpose",
            "purpose_display",
            "purpose_detail",
            "party_name",
            "party_contact",
            "party_address",
            "party_gstin",
            "recipient",
            "recipient_name",
            "recipient_display_name",
            "recipient_contact",
            "recipient_department",
            "issued_by_name",
            "destination",
            "expected_return_date",
            "is_overdue",
            "days_overdue",
            "asset",
            "asset_name",
            "work_order",
            "work_order_no",
            "vehicle",
            "vehicle_number",
            "driver",
            "driver_name",
            "transporter",
            "transporter_name",
            "vehicle_number_manual",
            "driver_name_manual",
            "driver_mobile",
            "security_name",
            "out_remarks",
            "is_hand_carried",
            "carried_by_name",
            "submitted_by",
            "submitted_by_name",
            "submitted_at",
            "approved_by",
            "approved_by_name",
            "approved_at",
            "gate_out_by",
            "gate_out_by_name",
            "gate_out_at",
            "last_return_at",
            "closed_by",
            "closed_by_name",
            "closed_at",
            "cancelled_by",
            "cancelled_at",
            "cancel_reason",
            "approval_rejected_reason",
            "rejected_reason",
            "short_close_reason",
            "total_estimated_value",
            "total_quantity_out",
            "total_quantity_returned",
            "pending_return_qty",
            "is_fully_returned",
            "items",
            "items_input",
            "return_events",
            "attachments",
            "created_at",
            "updated_at",
            "created_by_name",
            "updated_by_name",
        ]
        read_only_fields = [
            "pass_no",
            "status",
            "is_overdue",
            "vehicle",
            "driver",
            "transporter",
            "vehicle_number_manual",
            "driver_name_manual",
            "driver_mobile",
            "security_name",
            "out_remarks",
            "is_hand_carried",
            "carried_by_name",
            "submitted_by",
            "submitted_at",
            "approved_by",
            "approved_at",
            "gate_out_by",
            "gate_out_at",
            "last_return_at",
            "closed_by",
            "closed_at",
            "cancelled_by",
            "cancelled_at",
            "cancel_reason",
            "approval_rejected_reason",
            "rejected_reason",
            "short_close_reason",
        ]

    def validate_items_input(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        return value

    def validate(self, attrs):
        """A returnable pass needs a vendor and a return date; a non-returnable
        one needs the person receiving the material."""
        instance = getattr(self, "instance", None)
        is_returnable = attrs.get(
            "is_returnable", instance.is_returnable if instance else True
        )

        def resolve(field):
            if field in attrs:
                return attrs[field]
            return getattr(instance, field, None) if instance else None

        errors = {}
        if is_returnable:
            if not resolve("party_name"):
                errors["party_name"] = "Name the party the material is going to."
            if not resolve("expected_return_date"):
                errors["expected_return_date"] = "A returnable pass needs an expected return date."
            # A returnable pass is a request for material back; nobody "issues" it.
            attrs["issued_by_name"] = ""
        else:
            if not (resolve("recipient") or resolve("recipient_name")):
                errors["recipient_name"] = "Name the person receiving the material."
            # Nothing is coming back, so a return date would be a lie, and there
            # is no requester — only an issuer and a recipient.
            attrs["expected_return_date"] = None
            attrs["requested_by_name"] = ""
            attrs["contact_no"] = ""

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def _sync_items(self, gate_pass, items_input):
        """Replace the pass's lines with ``items_input``, preserving returned quantities.

        Lines that already have material back cannot be dropped or shrunk below
        what has returned — that would corrupt the ledger.
        """
        company = gate_pass.company
        existing = {item.id: item for item in gate_pass.items.all()}

        # Pair every incoming line with the row it updates (if any) and validate
        # before writing anything, so a bad line leaves the pass untouched.
        planned = []
        seen_ids = set()
        for index, payload in enumerate(items_input, start=1):
            item_id = payload.pop("id", None)
            payload["line_num"] = index
            item = existing.get(item_id) if item_id else None

            if item is not None:
                if payload["quantity_out"] < item.quantity_returned:
                    raise serializers.ValidationError(
                        {
                            "items_input": (
                                f"Line {index} ({item.item_name}): quantity cannot be less than "
                                f"the {item.quantity_returned} already returned."
                            )
                        }
                    )
                seen_ids.add(item.id)
            planned.append((item, payload))

        # Removals come first. A dropped line must let go of its line_num before
        # a surviving line is renumbered into it.
        for item_id, item in existing.items():
            if item_id in seen_ids:
                continue
            if item.quantity_returned > ZERO:
                raise serializers.ValidationError(
                    {
                        "items_input": (
                            f"Cannot remove line '{item.item_name}' — "
                            f"{item.quantity_returned} unit(s) have already been returned."
                        )
                    }
                )
            item.delete()

        # Park the survivors above the range about to be written. The
        # (gate_pass, line_num) constraint is checked per statement, so moving a
        # line from 2 to 1 collides with whatever still sits on 1 — even when
        # that row is itself about to move.
        if seen_ids:
            gate_pass.items.filter(id__in=seen_ids).update(
                line_num=F("line_num") + len(existing) + len(planned)
            )

        for item, payload in planned:
            if item is None:
                ReturnableGatePassItem.objects.create(
                    company=company,
                    gate_pass=gate_pass,
                    created_by=gate_pass.updated_by,
                    updated_by=gate_pass.updated_by,
                    **payload,
                )
                continue
            for field, field_value in payload.items():
                setattr(item, field, field_value)
            item.save()

    @transaction.atomic
    def create(self, validated_data):
        items_input = validated_data.pop("items_input", [])
        gate_pass = super().create(validated_data)
        self._sync_items(gate_pass, items_input)
        return gate_pass

    def _actor(self):
        request = self.context.get("request")
        return getattr(request, "user", None)

    def _assert_editable(self, instance):
        """Decide whether this user may edit this pass, and in whose capacity.

        A draft belongs to the department that raised it. The moment it is
        submitted it belongs to the approver instead: rather than bounce a pass
        back over a wrong party name or the wrong pass type, they correct it and
        sign it off. Past approval the gate is working off the pass as printed,
        so nobody edits it — it is cancelled and raised again.

        Returns True when this is the approver's edit.
        """
        user = self._actor()

        if instance.status == ReturnableStatus.DRAFT:
            if not (user and user.has_perm(MANAGE_PERM)):
                raise PermissionDenied("You cannot edit this gate pass.")
            return False

        if instance.status == ReturnableStatus.PENDING_APPROVAL:
            if not (user and user.has_perm(APPROVE_PERM)):
                raise PermissionDenied(
                    "This pass is waiting for approval — only its approver can edit it now."
                )
            return True

        raise serializers.ValidationError(
            {
                "status": (
                    "Only a draft, or a pass still waiting for approval, can be edited. "
                    "Cancel this one and raise a new one."
                )
            }
        )

    @staticmethod
    def _clear_other_shape(is_returnable):
        """Blank the half of the pass the new type does not use.

        ``validate()`` already clears the fields it owns; switching type also has
        to clear the ones it does not, or a pass that used to be non-returnable
        keeps its recipient long after it became a vendor return.
        """
        if is_returnable:
            return {
                "recipient": None,
                "recipient_name": "",
                "recipient_contact": "",
                "recipient_department": "",
                "issued_by_name": "",
            }
        return {
            "expected_return_date": None,
            "requested_by_name": "",
            "contact_no": "",
        }

    @transaction.atomic
    def update(self, instance, validated_data):
        items_input = validated_data.pop("items_input", None)
        is_approver_edit = self._assert_editable(instance)

        new_type = validated_data.get("is_returnable", instance.is_returnable)
        type_changed = new_type != instance.is_returnable
        previous_pass_no = instance.pass_no

        if type_changed:
            # The department cannot flip the type on its own draft: the number is
            # already issued and the register would carry an RGP number on a
            # non-returnable pass. The approver can, because they are the
            # authority correcting the request before it goes anywhere.
            if not is_approver_edit:
                raise serializers.ValidationError(
                    {
                        "is_returnable": (
                            "A pass cannot switch between returnable and non-returnable. "
                            "Send it for approval and let the approver change it, or "
                            "cancel it and raise a new one."
                        )
                    }
                )
            # Renumber into the other series. The old number is burned rather
            # than reused — nothing has been printed yet, and a gap in a series
            # is cheaper to explain than a number that lies about its type.
            instance.pass_no = ReturnableGatePassSequence.next_pass_no(
                instance.company, is_returnable=new_type
            )
            validated_data.update(self._clear_other_shape(new_type))

        gate_pass = super().update(instance, validated_data)
        if items_input is not None:
            self._sync_items(gate_pass, items_input)

        # The department only ever sees the pass again through its timeline, so
        # an approver's edit has to leave a mark there. A draft edit does not:
        # the department is editing its own unsent paperwork.
        if is_approver_edit:
            gate_pass.log(
                ReturnableLogAction.UPDATED,
                actor=self._actor(),
                note=self._approver_edit_note(gate_pass, type_changed, previous_pass_no),
            )
        return gate_pass

    @staticmethod
    def _approver_edit_note(gate_pass, type_changed, previous_pass_no):
        note = "Edited by the approver before sign-off."
        if type_changed:
            kind = "returnable" if gate_pass.is_returnable else "non-returnable"
            note += f" Switched to {kind} — renumbered {previous_pass_no} → {gate_pass.pass_no}."
        return note


# ---------------------------------------------------------------------------
# Transition input serializers
# ---------------------------------------------------------------------------


class GateOutInputSerializer(serializers.Serializer):
    vehicle = serializers.IntegerField(required=False, allow_null=True)
    driver = serializers.IntegerField(required=False, allow_null=True)
    transporter = serializers.IntegerField(required=False, allow_null=True)
    vehicle_number_manual = serializers.CharField(required=False, allow_blank=True, default="")
    driver_name_manual = serializers.CharField(required=False, allow_blank=True, default="")
    driver_mobile = serializers.CharField(required=False, allow_blank=True, default="")
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    out_remarks = serializers.CharField(required=False, allow_blank=True, default="")
    #: Material walked out by hand — no vehicle exists to record.
    is_hand_carried = serializers.BooleanField(required=False, default=False)
    carried_by_name = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs.get("is_hand_carried"):
            if not attrs.get("carried_by_name", "").strip():
                raise serializers.ValidationError(
                    {"carried_by_name": "Name the person carrying the material out."}
                )
            # Never leave half a vehicle record behind on a hand-carried pass.
            attrs["vehicle"] = None
            attrs["driver"] = None
            attrs["transporter"] = None
            attrs["vehicle_number_manual"] = ""
            attrs["driver_name_manual"] = ""
            attrs["driver_mobile"] = ""
            return attrs

        attrs["carried_by_name"] = ""
        if not attrs.get("vehicle") and not attrs.get("vehicle_number_manual"):
            raise serializers.ValidationError(
                {"vehicle": "Select a vehicle or enter the vehicle number."}
            )
        if not attrs.get("driver") and not attrs.get("driver_name_manual"):
            raise serializers.ValidationError({"driver": "Select a driver or enter the driver name."})
        return attrs


class ReasonInputSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False)


class ReturnLineInputSerializer(serializers.Serializer):
    pass_item = serializers.IntegerField()
    quantity_returned = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0.001")
    )
    return_condition = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class RecordReturnInputSerializer(serializers.Serializer):
    """The returning vehicle is captured independently of the outgoing one."""

    returned_at = serializers.DateTimeField(required=False)
    vehicle = serializers.IntegerField(required=False, allow_null=True)
    driver = serializers.IntegerField(required=False, allow_null=True)
    transporter = serializers.IntegerField(required=False, allow_null=True)
    vehicle_number_manual = serializers.CharField(required=False, allow_blank=True, default="")
    driver_name_manual = serializers.CharField(required=False, allow_blank=True, default="")
    driver_mobile = serializers.CharField(required=False, allow_blank=True, default="")
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    lines = ReturnLineInputSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("Record at least one returned line.")
        seen = set()
        for line in value:
            if line["pass_item"] in seen:
                raise serializers.ValidationError("The same line appears twice in this return.")
            seen.add(line["pass_item"])
        return value

    def validate(self, attrs):
        """Reject over-return outright rather than silently clamping the quantity."""
        gate_pass = self.context["gate_pass"]
        items = {item.id: item for item in gate_pass.items.all()}

        errors = []
        for line in attrs["lines"]:
            item = items.get(line["pass_item"])
            if item is None:
                errors.append(f"Line {line['pass_item']} does not belong to {gate_pass.pass_no}.")
                continue
            if line["quantity_returned"] > item.pending_return_qty:
                errors.append(
                    f"{item.item_name}: returning {line['quantity_returned']} exceeds the "
                    f"{item.pending_return_qty} still pending."
                )
        if errors:
            raise serializers.ValidationError({"lines": errors})
        return attrs
