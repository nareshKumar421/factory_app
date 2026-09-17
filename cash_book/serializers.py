"""Shapes the cash book sends to the page, and the shapes it accepts back."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    AdvanceDirection,
    AdvanceEntry,
    AtmAccount,
    AtmReceipt,
    CashBranch,
    CashBunch,
    CashDirection,
    CashEntry,
)


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
    """What a row of the register needs to say about the batch it is in."""

    class Meta:
        model = CashBunch
        fields = ["id", "number", "sent_at"]
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
    atm_account_name = serializers.CharField(
        source="atm_account.name", read_only=True, allow_null=True, default=None
    )
    advance_holder_name = serializers.CharField(
        source="advance_holder.full_name",
        read_only=True,
        allow_null=True,
        default=None,
    )
    direction_label = serializers.CharField(
        source="get_direction_display", read_only=True
    )
    approval_status = serializers.CharField(read_only=True)
    approval_label = serializers.CharField(
        source="get_approval_state_display", read_only=True
    )
    approval_decided_by_name = serializers.CharField(
        source="approval_decided_by.full_name",
        read_only=True,
        allow_null=True,
        default=None,
    )
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
            "atm_account",
            "atm_account_name",
            "advance_holder",
            "advance_holder_name",
            "gl_account_code",
            "gl_account_name",
            "item",
            "detail",
            "balance_after",
            "bunch",
            "approval_status",
            "approval_label",
            "approval_sent_at",
            "approval_decided_at",
            "approval_decided_by_name",
            "approval_note",
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
    # On a receipt: the card the cash was drawn off, which takes it off that
    # card's balance. On a payment: the person whose advance it clears.
    atm_account = serializers.PrimaryKeyRelatedField(
        queryset=AtmAccount.objects.all(), required=False, allow_null=True
    )
    advance_holder = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(), required=False, allow_null=True
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
    """A batch: what is in it, what it comes to, and whether it has gone."""

    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, allow_null=True, default=None
    )
    sent_by_name = serializers.CharField(
        source="sent_by.full_name", read_only=True, allow_null=True, default=None
    )
    is_sent = serializers.BooleanField(read_only=True)
    entry_count = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    class Meta:
        model = CashBunch
        fields = [
            "id",
            "number",
            "remarks",
            "created_at",
            "created_by_name",
            "sent_at",
            "sent_by_name",
            "is_sent",
            "entry_count",
            "total",
        ]
        read_only_fields = fields

    # Counted off the prefetched entries rather than re-queried per row: the
    # list endpoint prefetches them, so this costs nothing per batch.
    def _live(self, obj):
        return [entry for entry in obj.entries.all() if entry.is_active]

    def get_entry_count(self, obj):
        return len(self._live(obj))

    def get_total(self, obj):
        return sum((entry.amount for entry in self._live(obj)), Decimal("0.00"))


class CashBunchDetailSerializer(CashBunchSerializer):
    """The batch plus its vouchers -- what the spreadsheet is built from."""

    entries = serializers.SerializerMethodField()

    class Meta(CashBunchSerializer.Meta):
        fields = CashBunchSerializer.Meta.fields + ["entries"]
        read_only_fields = fields

    def get_entries(self, obj):
        rows = sorted(self._live(obj), key=lambda entry: entry.id)
        return CashEntrySerializer(rows, many=True).data


class CreateBunchSerializer(serializers.Serializer):
    """Input for bundling approved vouchers into a batch."""

    entry_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class MarkSentSerializer(serializers.Serializer):
    """Input for saying the batch has gone to head office, or has not."""

    sent = serializers.BooleanField(required=False, default=True)


class EntryIdsSerializer(serializers.Serializer):
    """Which entries a decision is about."""

    entry_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False
    )
    note = serializers.CharField(required=False, allow_blank=True, default="")


class PersonSerializer(serializers.Serializer):
    """Whoever can hold an advance. Output only.

    ``balance`` is what they are holding, and is only filled in when the list
    was asked for holders -- it is the picker's whole reason for being narrowed
    there, so it is worth showing beside the name.
    """

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    email = serializers.EmailField()
    balance = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True, default=None
    )

    def get_name(self, obj):
        return getattr(obj, "full_name", "") or obj.email


class AtmAccountSerializer(serializers.ModelSerializer):
    """A card, with what is left on it."""

    balance = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

    class Meta:
        model = AtmAccount
        fields = ["id", "name", "opening_balance", "is_active", "balance"]
        read_only_fields = ["id", "balance"]


class AtmAccountWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    opening_balance = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False
    )
    is_active = serializers.BooleanField(required=False)

    def validate_name(self, value):
        name = (value or "").strip()
        if not name:
            raise serializers.ValidationError("A card needs a name.")
        return name


class AtmReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = AtmReceipt
        fields = ["id", "received_on", "amount", "detail", "is_active"]
        read_only_fields = ["id", "is_active"]


class RecordAtmReceiptSerializer(serializers.Serializer):
    """Input for paying money onto a card."""

    received_on = serializers.DateField()
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    detail = serializers.CharField(required=False, allow_blank=True, default="")


class MovementSerializer(serializers.Serializer):
    """One line of a card statement or a person's advance ledger.

    A plain serializer: a movement is a merge of two tables, so most rows have
    no single model behind them. Output only.
    """

    kind = serializers.CharField()
    id = serializers.IntegerField()
    date = serializers.DateField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    signed = serializers.DecimalField(max_digits=14, decimal_places=2)
    balance_after = serializers.DecimalField(max_digits=14, decimal_places=2)
    detail = serializers.CharField(allow_blank=True)
    cash_entry_id = serializers.IntegerField(allow_null=True)


class AdvanceHolderSerializer(serializers.Serializer):
    """A person and what they are still holding. Output only."""

    person = PersonSerializer()
    balance = serializers.DecimalField(max_digits=14, decimal_places=2)


class AdvanceEntrySerializer(serializers.ModelSerializer):
    person_name = serializers.CharField(
        source="person.full_name", read_only=True, allow_null=True, default=None
    )
    direction_label = serializers.CharField(
        source="get_direction_display", read_only=True
    )

    class Meta:
        model = AdvanceEntry
        fields = [
            "id",
            "person",
            "person_name",
            "entry_date",
            "direction",
            "direction_label",
            "amount",
            "detail",
            "is_active",
        ]
        read_only_fields = fields


class RecordAdvanceSerializer(serializers.Serializer):
    """Input for handing cash over, or taking it back."""

    person = serializers.PrimaryKeyRelatedField(queryset=get_user_model().objects.all())
    entry_date = serializers.DateField()
    direction = serializers.ChoiceField(choices=AdvanceDirection.choices)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    detail = serializers.CharField(required=False, allow_blank=True, default="")


__all__ = [
    "CashBunchSummarySerializer",
    "CreateBunchSerializer",
    "MarkSentSerializer",
    "AdvanceEntrySerializer",
    "AdvanceHolderSerializer",
    "AtmAccountSerializer",
    "AtmAccountWriteSerializer",
    "AtmReceiptSerializer",
    "MovementSerializer",
    "PersonSerializer",
    "RecordAdvanceSerializer",
    "RecordAtmReceiptSerializer",
    "CashBunchDetailSerializer",
    "CashBunchSerializer",
    "CashEntrySerializer",
    "EntryIdsSerializer",
    "CashBranchSerializer",
    "CashBranchWriteSerializer",
    "GLAccountSerializer",
    "RecordEntrySerializer",
    "UpdateEntrySerializer",
]
