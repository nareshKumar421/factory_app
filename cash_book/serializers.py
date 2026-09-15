"""Shapes the cash book sends to the page, and the shapes it accepts back."""

from decimal import Decimal

from rest_framework import serializers

from .models import BunchStatus, CashBranch, CashBunch, CashDirection, CashEntry


class CashBranchSerializer(serializers.ModelSerializer):
    """One branch, as the picker and the settings page read it."""

    entry_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CashBranch
        fields = ["id", "name", "sort_order", "is_active", "entry_count"]
        read_only_fields = ["id", "entry_count"]


class CashBranchWriteSerializer(serializers.Serializer):
    """Input for the settings page. Name is the only thing worth typing."""

    name = serializers.CharField(max_length=60)
    sort_order = serializers.IntegerField(min_value=0, max_value=999, required=False)
    is_active = serializers.BooleanField(required=False)

    def validate_name(self, value):
        name = (value or "").strip()
        if not name:
            raise serializers.ValidationError("A branch needs a name.")
        return name


class CashBunchSummarySerializer(serializers.ModelSerializer):
    """What a row of the register needs to say about the bunch it is in."""

    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = CashBunch
        fields = ["id", "number", "status", "status_label", "sent_at", "decided_at"]
        read_only_fields = fields


class CashEntrySerializer(serializers.ModelSerializer):
    """One line of the book, as the register renders it.

    ``balance_after`` is the book's own running balance, so it is sent even
    when the list is filtered -- a row means what it meant on the day, not what
    it would mean if only the filtered rows existed.
    """

    branch_name = serializers.CharField(
        source="branch.name", read_only=True, allow_null=True, default=None
    )
    direction_label = serializers.CharField(
        source="get_direction_display", read_only=True
    )
    approval_status = serializers.CharField(read_only=True)
    is_locked = serializers.BooleanField(read_only=True)
    bunch = CashBunchSummarySerializer(read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = CashEntry
        fields = [
            "id",
            "entry_date",
            "direction",
            "direction_label",
            "amount",
            "branch",
            "branch_name",
            "gl_account_code",
            "gl_account_name",
            "item",
            "detail",
            "balance_after",
            "bunch",
            "approval_status",
            "is_locked",
            "is_active",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class RecordEntrySerializer(serializers.Serializer):
    """Input for writing a line into the book.

    The G/L head and the branch are required on a payment and refused on a
    receipt -- the rule itself lives in ``services._clean_payment_fields`` so
    it holds for every caller; this only shapes the request.
    """

    entry_date = serializers.DateField()
    direction = serializers.ChoiceField(choices=CashDirection.choices)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    branch = serializers.PrimaryKeyRelatedField(
        queryset=CashBranch.objects.all(), required=False, allow_null=True
    )
    gl_account_code = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default=""
    )
    # Sent by the picker alongside the code, and used only as the fallback
    # snapshot when SAP cannot be reached to confirm the name itself.
    gl_account_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    item = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    detail = serializers.CharField()

    def validate_detail(self, value):
        detail = (value or "").strip()
        if not detail:
            raise serializers.ValidationError(
                "Write what the money was for -- an entry nobody can read back "
                "is not a record."
            )
        return detail


class UpdateEntrySerializer(RecordEntrySerializer):
    """Correcting an entry. Every field optional; at least one required."""

    entry_date = serializers.DateField(required=False)
    direction = serializers.ChoiceField(choices=CashDirection.choices, required=False)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    detail = serializers.CharField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to change. Send at least one field."
            )
        return attrs


class GLAccountSerializer(serializers.Serializer):
    """One SAP account, as the picker lists it. Output only."""

    account_code = serializers.CharField()
    account_name = serializers.CharField()
    account_type = serializers.CharField(required=False, default="")


class CashBunchSerializer(serializers.ModelSerializer):
    """A bunch, with enough of its contents to decide on it."""

    status_label = serializers.CharField(source="get_status_display", read_only=True)
    sent_by_name = serializers.CharField(
        source="sent_by.full_name", read_only=True, allow_null=True, default=None
    )
    decided_by_name = serializers.CharField(
        source="decided_by.full_name", read_only=True, allow_null=True, default=None
    )
    entry_count = serializers.SerializerMethodField()
    total_out = serializers.SerializerMethodField()
    total_in = serializers.SerializerMethodField()

    class Meta:
        model = CashBunch
        fields = [
            "id",
            "number",
            "status",
            "status_label",
            "remarks",
            "sent_at",
            "sent_by_name",
            "decided_at",
            "decided_by_name",
            "decision_note",
            "entry_count",
            "total_in",
            "total_out",
        ]
        read_only_fields = fields

    # Counted off the prefetched entries rather than re-queried per row: the
    # list endpoint prefetches them, so this costs nothing per bunch.
    def _live(self, obj):
        return [entry for entry in obj.entries.all() if entry.is_active]

    def get_entry_count(self, obj):
        return len(self._live(obj))

    def get_total_in(self, obj):
        return sum(
            (e.amount for e in self._live(obj) if e.direction == CashDirection.IN),
            Decimal("0.00"),
        )

    def get_total_out(self, obj):
        return sum(
            (e.amount for e in self._live(obj) if e.direction == CashDirection.OUT),
            Decimal("0.00"),
        )


class CashBunchDetailSerializer(CashBunchSerializer):
    """The bunch plus its lines -- what the approver actually reads."""

    entries = serializers.SerializerMethodField()

    class Meta(CashBunchSerializer.Meta):
        fields = CashBunchSerializer.Meta.fields + ["entries"]
        read_only_fields = fields

    def get_entries(self, obj):
        rows = sorted(obj.entries.all(), key=lambda entry: entry.id)
        return CashEntrySerializer(rows, many=True).data


class SendForApprovalSerializer(serializers.Serializer):
    """Input for bundling loose entries and handing them over."""

    entry_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class DecisionSerializer(serializers.Serializer):
    """Input for approving or rejecting. A rejection must say why."""

    note = serializers.CharField(required=False, allow_blank=True, default="")


class ResendSerializer(serializers.Serializer):
    """Input for sending a corrected bunch back up."""

    remarks = serializers.CharField(required=False, allow_blank=True, default=None)


__all__ = [
    "BunchStatus",
    "CashBunchDetailSerializer",
    "CashBunchSerializer",
    "CashBunchSummarySerializer",
    "CashEntrySerializer",
    "DecisionSerializer",
    "CashBranchSerializer",
    "CashBranchWriteSerializer",
    "GLAccountSerializer",
    "RecordEntrySerializer",
    "ResendSerializer",
    "SendForApprovalSerializer",
    "UpdateEntrySerializer",
]
