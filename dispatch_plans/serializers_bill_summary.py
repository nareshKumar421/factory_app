"""Serializers for the bill summary."""

from rest_framework import serializers

from .bill_summary_service import BillSummaryService
from .models import DispatchPlan
from .models_bill_summary import APP_SOURCE, BillSummary, BillSummaryLine


class BillSummaryLineSerializer(serializers.ModelSerializer):
    is_short = serializers.BooleanField(read_only=True)

    class Meta:
        model = BillSummaryLine
        fields = [
            "id",
            "sap_line_num",
            "item_code",
            "item_name",
            "uom",
            "warehouse_code",
            "invoice_qty",
            "pcs_per_box",
            "boxes",
            "loose_qty",
            "litres",
            "gross_weight",
            "dispatch_qty",
            "is_short",
        ]


def _person(user) -> str:
    """`accounts.User` has `full_name`, not `get_full_name()`."""
    if not user:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


class BillSummaryListSerializer(serializers.ModelSerializer):
    """The app's own sheets.

    `source` and `key` are carried by these rows too, because the screen shows
    them alongside dispatches read straight out of SAP (which have no primary
    key) and routes on `key` for both. A row that has to be asked what it is
    before it can be rendered is how the two drift apart.
    """

    company_code = serializers.CharField(source="company.code", read_only=True)
    issued_by_name = serializers.SerializerMethodField()
    picked_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    rejected_by_name = serializers.SerializerMethodField()
    printed_by_name = serializers.SerializerMethodField()
    is_editable = serializers.BooleanField(read_only=True)
    totals = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()
    key = serializers.SerializerMethodField()

    class Meta:
        model = BillSummary
        fields = [
            "id",
            "source",
            "key",
            "entry_no",
            "company",
            "company_code",
            "sap_invoice_doc_entry",
            "sap_invoice_doc_num",
            "customer_code",
            "customer_name",
            "delivery_address",
            "invoice_date",
            "bill_amount",
            "branch_name",
            "branch_gstin",
            "company_legal_name",
            "warehouse_codes",
            "dispatch_date",
            "bilty_no",
            "bilty_date",
            "transporter_name",
            "vehicle_no",
            "driver_name",
            "driver_mobile",
            "status",
            "sap_status",
            "sap_error",
            "sap_note",
            "sap_posted_at",
            "issued_by_name",
            "picked_by_name",
            "approved_by_name",
            "rejected_by_name",
            "printed_by_name",
            "issued_at",
            "submitted_at",
            "approved_at",
            "rejected_at",
            "printed_at",
            "picked_at",
            "is_editable",
            "remarks",
            "cancel_reason",
            "reject_reason",
            "totals",
        ]

    def get_source(self, obj) -> str:
        return APP_SOURCE

    def get_key(self, obj) -> str:
        return str(obj.pk)

    def get_issued_by_name(self, obj) -> str:
        return _person(obj.issued_by)

    def get_picked_by_name(self, obj) -> str:
        return _person(obj.picked_by)

    def get_approved_by_name(self, obj) -> str:
        return _person(obj.approved_by)

    def get_rejected_by_name(self, obj) -> str:
        return _person(obj.rejected_by)

    def get_printed_by_name(self, obj) -> str:
        return _person(obj.printed_by)

    def get_totals(self, obj) -> dict:
        return obj.totals()


class BillSummaryDetailSerializer(BillSummaryListSerializer):
    lines = BillSummaryLineSerializer(source="active_lines", many=True, read_only=True)
    plan_transport = serializers.SerializerMethodField()

    class Meta(BillSummaryListSerializer.Meta):
        fields = BillSummaryListSerializer.Meta.fields + ["lines", "plan_transport"]

    def get_plan_transport(self, obj) -> dict | None:
        """What the bill's dispatch plan holds now, for the re-send form.

        The sheet copied the plan when it was raised. A sheet sent back for its
        bilty was usually raised before the plan had one, so the form fills its
        blanks from here instead of from a copy that was empty from the start.
        Null when the bill has no dispatch plan.
        """
        plan = (
            DispatchPlan.objects.filter(
                company_id=obj.company_id,
                sap_invoice_doc_entry=obj.sap_invoice_doc_entry,
            )
            .select_related(
                "vehicle__transporter", "transporter", "driver",
                "linked_vehicle_entry__vehicle__transporter",
                "linked_vehicle_entry__driver",
            )
            .first()
        )
        if plan is None:
            return None
        data = BillSummaryService._plan_data(plan)
        return {
            field: data[field]
            for field in (
                "bilty_no", "bilty_date", "transporter_name",
                "vehicle_no", "driver_name", "driver_mobile",
            )
        }


class BillSummaryGenerateLineSerializer(serializers.Serializer):
    sap_line_num = serializers.IntegerField()
    dispatch_qty = serializers.DecimalField(max_digits=18, decimal_places=3)


class BillSummaryGenerateSerializer(serializers.Serializer):
    """The form: what the app found, with the user's corrections and additions.

    No `dispatch_date`: that is the warehouse's to give at approval, and a field
    the dispatch desk could fill would be a field somebody fills.

    `bilty_no` is optional here and required at approval. SAP will not take the
    posting without one, but the posting does not happen until the warehouse
    approves, and the truck's LR is often not raised while its load is still
    being put together — see `bill_summary_service`.
    """

    sap_invoice_doc_entry = serializers.IntegerField()
    sap_invoice_doc_num = serializers.CharField(max_length=30, required=False, allow_blank=True)
    bilty_no = serializers.CharField(max_length=50, required=False, allow_blank=True)
    bilty_date = serializers.DateField(required=False, allow_null=True)
    transporter_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    vehicle_no = serializers.CharField(max_length=30, required=False, allow_blank=True)
    driver_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    driver_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    # Only the lines being dispatched short need sending; the rest default to the
    # full billed quantity.
    lines = BillSummaryGenerateLineSerializer(many=True, required=False)


class BillSummaryResubmitSerializer(serializers.Serializer):
    """Corrections to a sheet the warehouse has not approved, on its way back.

    Every field is optional and only what is sent is changed: the screen that
    posts this is usually fixing the one thing the warehouse asked about, and a
    serializer that demanded the whole form back would have the frontend
    re-sending values nobody looked at.
    """

    bilty_no = serializers.CharField(max_length=50, required=False, allow_blank=True)
    bilty_date = serializers.DateField(required=False, allow_null=True)
    transporter_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    vehicle_no = serializers.CharField(max_length=30, required=False, allow_blank=True)
    driver_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    driver_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)
    lines = BillSummaryGenerateLineSerializer(many=True, required=False)


class BillSummaryApproveSerializer(serializers.Serializer):
    """One dispatch date across however many sheets are being approved together.

    `ids` is a list even for a single sheet, so the one-at-a-time button and the
    whole-truck button post the same request and there is only one path to get
    an approval wrong in.
    """

    ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, max_length=200
    )
    dispatch_date = serializers.DateField()


class BillSummaryRejectSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class BillSummaryBulkSubmitSerializer(serializers.Serializer):
    """The bills of one truck, as the vehicle-linking screen has just linked them.

    `dry_run` is what the popup asks with: it wants the count before it offers,
    and the user may well say no.
    """

    doc_entries = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, max_length=200
    )
    dry_run = serializers.BooleanField(required=False, default=False)


class BillSummaryCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)
