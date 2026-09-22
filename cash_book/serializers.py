"""Shapes the cash book sends to the page, and the shapes it accepts back."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    CashEntryAttachment,
    AdvanceDirection,
    AdvanceEntry,
    AtmAccount,
    AtmReceipt,
    CashBranch,
    CashBunch,
    CashDirection,
    CashEntry,
    SalaryAdvance,
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


class CashEntryAttachmentSerializer(serializers.ModelSerializer):
    """One bill against a line. Output only.

    ``url`` is built from the request so it works behind whatever host the
    app is served on, rather than baking in the one it was developed on.
    """

    url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.CharField(
        source="uploaded_by.full_name", read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = CashEntryAttachment
        fields = [
            "id",
            "original_filename",
            "size_bytes",
            "url",
            "uploaded_at",
            "uploaded_by_name",
        ]
        read_only_fields = fields

    def get_url(self, attachment):
        if not attachment.file:
            return None
        url = attachment.file.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


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
    approver_name = serializers.CharField(
        source="approver.full_name", read_only=True, allow_null=True, default=None
    )
    attachments = CashEntryAttachmentSerializer(many=True, read_only=True)
    is_locked = serializers.BooleanField(read_only=True)
    bunch = CashBunchSummarySerializer(read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = CashEntry
        fields = [
            "id",
            "serial_number",
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
            "approver",
            "approver_name",
            "attachments",
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

    #: The voucher's own number. Left out, the next free one is used.
    serial_number = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
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
    # Who should agree to this payment. Required on a payment and refused on a
    # receipt -- the rule is in ``services._clean_approver`` so it holds for
    # every caller, and it checks the person can actually act on it.
    approver = serializers.PrimaryKeyRelatedField(
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
    #: False for a row somebody has taken out. It is shown, struck through,
    #: and contributes nothing to the running balance beside it.
    is_active = serializers.BooleanField(default=True)


class SetApproverSerializer(serializers.Serializer):
    """Make somebody an approver, or stop them being one. Input only."""

    person = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all()
    )
    approving = serializers.BooleanField(default=True)


class NewPersonSerializer(serializers.Serializer):
    """Somebody to add as a holder of cash. Input only."""

    name = serializers.CharField(max_length=150)


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


class SalaryAdvanceEmployeeSerializer(serializers.Serializer):
    """A name the advance screen can be recorded against. Output only.

    Deliberately thin. The picker is opened by whoever keeps the cash book,
    who has no right to read a payroll -- so it carries what identifies a
    person and nothing about what they earn.
    """

    id = serializers.IntegerField()
    employee_code = serializers.CharField()
    full_name = serializers.CharField()
    department = serializers.CharField(source="department.name", default="")
    designation = serializers.CharField(source="designation.name", default="")


class SalaryAdvanceSerializer(serializers.ModelSerializer):
    """One advance against salary, as both screens read it.

    ``state_label`` is the server's wording rather than the client's, so the
    three states read the same on the page, in the admin and in an export.
    """

    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    department = serializers.SerializerMethodField()
    state_label = serializers.CharField(source="get_state_display", read_only=True)
    decided_by_name = serializers.SerializerMethodField()
    recorded_by_name = serializers.SerializerMethodField()
    voucher_number = serializers.IntegerField(
        source="cash_entry.serial_number", read_only=True, default=None
    )
    is_outstanding = serializers.BooleanField(read_only=True)

    class Meta:
        model = SalaryAdvance
        fields = [
            "id",
            "employee",
            "employee_name",
            "employee_code",
            "department",
            "paid_on",
            "amount",
            "reason",
            "cash_entry",
            "voucher_number",
            "state",
            "state_label",
            "decided_by",
            "decided_by_name",
            "decided_at",
            "decision_note",
            "deduct_from",
            "deducted_on",
            "is_outstanding",
            "is_active",
            "recorded_by_name",
            "created_at",
        ]
        read_only_fields = fields

    def get_department(self, obj):
        return obj.employee.department.name if obj.employee.department_id else ""

    def _person(self, user):
        if user is None:
            return ""
        return getattr(user, "full_name", "") or user.email

    def get_decided_by_name(self, obj):
        return self._person(obj.decided_by)

    def get_recorded_by_name(self, obj):
        return self._person(obj.created_by)


class RecordSalaryAdvanceSerializer(serializers.Serializer):
    """Input for writing down an advance accounts have handed over."""

    employee = serializers.IntegerField(min_value=1)
    paid_on = serializers.DateField()
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    # The voucher on the register that paid it out. Optional: the cash may
    # have gone out by bank transfer, or on a voucher nobody joined up.
    cash_entry = serializers.PrimaryKeyRelatedField(
        queryset=CashEntry.objects.all(), required=False, allow_null=True, default=None
    )


class UpdateSalaryAdvanceSerializer(serializers.Serializer):
    """Corrections to an advance HR have not decided on yet."""

    employee = serializers.IntegerField(min_value=1, required=False)
    paid_on = serializers.DateField(required=False)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    reason = serializers.CharField(required=False, allow_blank=True)
    cash_entry = serializers.PrimaryKeyRelatedField(
        queryset=CashEntry.objects.all(), required=False, allow_null=True
    )


class DecideSalaryAdvanceSerializer(serializers.Serializer):
    """HR's verdict on one or more advances."""

    advance_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False
    )
    approve = serializers.BooleanField()
    note = serializers.CharField(required=False, allow_blank=True, default="")
    # Which salary month it comes off. Defaulted on the server to the month
    # after it was paid, so HR only send one when they mean another.
    deduct_from = serializers.DateField(required=False, allow_null=True, default=None)


class MarkDeductedSerializer(serializers.Serializer):
    """The day the amount actually came off a wage."""

    deducted_on = serializers.DateField(required=False, allow_null=True, default=None)


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
    "DecideSalaryAdvanceSerializer",
    "MarkDeductedSerializer",
    "RecordSalaryAdvanceSerializer",
    "SalaryAdvanceEmployeeSerializer",
    "SalaryAdvanceSerializer",
    "UpdateSalaryAdvanceSerializer",
]
