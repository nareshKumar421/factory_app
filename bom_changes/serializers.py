"""Shapes in and out of the BOM Changes API.

Input follows what SAP Portal's page posted (``public/bom.html``
``doSubmitCreate`` / ``doSubmitUpdate``) and the checks its routes made
(``server.js`` lines 307-309, 354-356, 390-391: an item, a quantity above zero,
at least one component), plus the ones SAP would make anyway -- a component
quantity above zero, and a BOM that does not contain its own parent.
"""

from decimal import Decimal

from rest_framework import serializers

from . import workflow
from .constants import (
    SAP_TEXT_LIMIT,
    BOMChangeKind,
    BOMChangeStatus,
    BOMType,
    IssueMethod,
    LineType,
)
from .models import BOMChangeApproval, BOMChangeLine, BOMChangeRequest
from .services import actions_for


def _code(value: str) -> str:
    return (value or "").strip().upper()


def _person(user, legacy: str = "") -> str:
    if user is not None:
        return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user.pk)
    return legacy or ""


# ---------------------------------------------------------------------------
# In
# ---------------------------------------------------------------------------


class BOMChangeLineInputSerializer(serializers.Serializer):
    item_type = serializers.ChoiceField(choices=LineType.choices, default=LineType.ITEM)
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    quantity = serializers.DecimalField(max_digits=19, decimal_places=6, min_value=Decimal("0.000001"))
    issue_method = serializers.ChoiceField(choices=IssueMethod.choices, default=IssueMethod.MANUAL)
    warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    unit_cost = serializers.DecimalField(
        max_digits=19, decimal_places=6, min_value=Decimal("0"), required=False, default=Decimal("0")
    )
    # The portal cut a comment to 100 characters on its way to SAP; it is
    # refused past that here rather than cut without telling anyone.
    comment = serializers.CharField(
        max_length=SAP_TEXT_LIMIT, required=False, allow_blank=True, default=""
    )

    def validate_item_code(self, value):
        code = _code(value)
        if not code:
            raise serializers.ValidationError("A component needs an item or resource code.")
        return code

    def validate_warehouse(self, value):
        return (value or "").strip()


class BOMChangeRequestInputSerializer(serializers.Serializer):
    """A new request (``POST requests/``) or a direct push (``POST requests/direct-push/``).

    UPDATE: header fields left out are taken from the tree SAP holds.
    """

    kind = serializers.ChoiceField(choices=BOMChangeKind.choices)
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    quantity = serializers.DecimalField(
        max_digits=18, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    bom_type = serializers.ChoiceField(choices=BOMType.choices, required=False)
    warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True)
    distribution_rule = serializers.CharField(max_length=50, required=False, allow_blank=True)
    project = serializers.CharField(max_length=50, required=False, allow_blank=True)
    lines = BOMChangeLineInputSerializer(many=True, allow_empty=False)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_item_code(self, value):
        code = _code(value)
        if not code:
            raise serializers.ValidationError("Choose the item the BOM is for.")
        return code

    def validate(self, attrs):
        if attrs["kind"] == BOMChangeKind.CREATE:
            if not (attrs.get("item_name") or "").strip():
                raise serializers.ValidationError({"item_name": ["A new BOM needs the item's name."]})
            attrs.setdefault("quantity", Decimal("1"))
            attrs.setdefault("bom_type", BOMType.PRODUCTION)
        for field in ("item_name", "warehouse", "distribution_rule", "project"):
            if field in attrs:
                attrs[field] = (attrs[field] or "").strip()
        parent = attrs["item_code"]
        if any(line["item_code"] == parent for line in attrs["lines"]):
            raise serializers.ValidationError(
                {"lines": [f"A BOM cannot contain its own item ({parent})."]}
            )
        return attrs


class DecisionSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)


class ListFilterSerializer(serializers.Serializer):
    status = serializers.CharField(required=False, allow_blank=True)
    kind = serializers.ChoiceField(choices=BOMChangeKind.choices, required=False)
    mine = serializers.BooleanField(required=False, default=False)
    actionable = serializers.BooleanField(required=False, default=False)
    search = serializers.CharField(required=False, allow_blank=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=500, default=200)

    def validate_status(self, value):
        statuses = [part.strip().upper() for part in (value or "").split(",") if part.strip()]
        unknown = sorted(set(statuses) - set(BOMChangeStatus.values))
        if unknown:
            raise serializers.ValidationError(f"Unknown status: {', '.join(unknown)}.")
        return statuses


# ---------------------------------------------------------------------------
# Out
# ---------------------------------------------------------------------------


class BOMChangeLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = BOMChangeLine
        fields = [
            "id", "visual_order", "item_type", "item_code", "item_name", "quantity",
            "issue_method", "warehouse", "unit_cost", "comment",
        ]
        read_only_fields = fields


class BOMChangeApprovalSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    decided_by_name = serializers.SerializerMethodField()
    direct = serializers.SerializerMethodField()

    class Meta:
        model = BOMChangeApproval
        fields = [
            "id", "level", "from_status", "action", "action_label", "decided_by",
            "decided_by_name", "remarks", "decided_at", "direct",
        ]
        read_only_fields = fields

    def get_decided_by_name(self, row):
        return _person(row.decided_by, row.legacy_decided_by)

    def get_direct(self, row):
        return row.level == 0


class BOMChangeRequestSerializer(serializers.ModelSerializer):
    """A request with what the caller may do to it. Needs ``request`` (the HTTP
    one) in the context; ``detail=True`` adds lines, approvals and the snapshot."""

    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    awaiting = serializers.SerializerMethodField()
    submitted_by = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    sap_pushed_by_name = serializers.SerializerMethodField()
    cancelled_by_name = serializers.SerializerMethodField()
    line_count = serializers.SerializerMethodField()

    class Meta:
        model = BOMChangeRequest
        fields = [
            "id", "kind", "kind_label", "item_code", "item_name", "quantity", "bom_type",
            "warehouse", "distribution_rule", "project", "status", "status_label", "awaiting",
            "submitted_at", "submitted_by", "is_mine", "sap_result", "sap_pushed_at",
            "sap_pushed_by_name", "push_error", "push_failed_at", "cancelled_at",
            "cancelled_by_name", "legacy_portal_id", "line_count",
        ]
        read_only_fields = fields

    def _user(self):
        return self.context["request"].user

    def get_awaiting(self, row):
        return workflow.awaiting_label(row.status, self.context.get("levels"))

    def get_submitted_by(self, row):
        return _person(row.created_by, row.legacy_submitted_by)

    def get_is_mine(self, row):
        return row.created_by_id is not None and row.created_by_id == self._user().pk

    def get_sap_pushed_by_name(self, row):
        return _person(row.sap_pushed_by, row.legacy_sap_pushed_by)

    def get_cancelled_by_name(self, row):
        return _person(row.cancelled_by)

    def get_line_count(self, row):
        return len(row.lines.all())

    def to_representation(self, row):
        data = super().to_representation(row)
        levels = self.context.get("levels")
        # can_approve / can_reject / can_cancel / can_push: the page's buttons
        # follow these, so it never offers what the server would refuse.
        data.update(actions_for(row, self._user(), levels))
        if self.context.get("detail"):
            approvals = list(row.approvals.all())
            data["lines"] = BOMChangeLineSerializer(row.lines.all(), many=True).data
            data["approvals"] = BOMChangeApprovalSerializer(approvals, many=True).data
            data["steps"] = workflow.progress(row.status, approvals, levels)
            data["original_data"] = row.original_data
        return data
