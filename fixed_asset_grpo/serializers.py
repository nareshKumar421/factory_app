"""Serializers for the Fixed Asset GRPO endpoints.

The GRPO request itself is grpo's own ``GRPOPostRequestSerializer``, unchanged,
so a draft saved here re-validates in ``post_saved_grpo`` exactly as any other.
The PO link request is raw_material_gatein's ``POReceiveRequestSerializer``
plus an optional supplier bill number.
"""

from rest_framework import serializers

from grpo.serializers import (
    GRPODashboardSummarySerializer,
    GRPOPostingSerializer,
    GRPOPostResponseSerializer,
)
from raw_material_gatein.serializers import POReceiveRequestSerializer

from .services.state import is_simulated


class FAPOLinkRequestSerializer(POReceiveRequestSerializer):
    """Link (or change) an SAP PO on an FA entry: the PO, its lines, received qty."""

    invoice_no = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default="",
        help_text="Supplier bill number, when the gate did not record one.",
    )


class FAClosureSerializer(serializers.Serializer):
    """"Done in SAP by hand": the SAP document it became, or a note (one is needed)."""

    sap_doc_num = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not (attrs.get("sap_doc_num") or "").strip() and not (attrs.get("note") or "").strip():
            raise serializers.ValidationError(
                "Give the SAP document number or a note saying where this went."
            )
        return attrs


class FANeedsPOEntrySerializer(serializers.Serializer):
    """A completed FA gate entry with no PO linked yet."""

    vehicle_entry_id = serializers.IntegerField()
    entry_no = serializers.CharField()
    entry_time = serializers.DateTimeField(allow_null=True)
    status = serializers.CharField()
    work_order_number = serializers.CharField(allow_blank=True)
    supplier_name = serializers.CharField(allow_blank=True)
    invoice_number = serializers.CharField(allow_blank=True)
    item_count = serializers.IntegerField()


class FAAllEntrySerializer(serializers.Serializer):
    """One FA gate entry on the all-entries list, with where it stands."""

    vehicle_entry_id = serializers.IntegerField()
    entry_no = serializers.CharField()
    entry_time = serializers.DateTimeField(allow_null=True)
    status = serializers.CharField()
    state = serializers.CharField()
    work_order_number = serializers.CharField(allow_blank=True)
    supplier_name = serializers.CharField(allow_blank=True)
    invoice_number = serializers.CharField(allow_blank=True)
    po_numbers = serializers.ListField(child=serializers.CharField())


class FAGRPODashboardSummarySerializer(GRPODashboardSummarySerializer):
    """grpo's summary numbers, counted over FA entries only, plus FA's own."""

    queued_count = serializers.IntegerField()
    draft_count = serializers.IntegerField()
    simulated_count = serializers.IntegerField()
    needs_po_count = serializers.IntegerField()
    closed_count = serializers.IntegerField()


class FAGRPOPostingSerializer(GRPOPostingSerializer):
    """grpo's posting, plus whether it was simulated and who posted it here."""

    simulated = serializers.SerializerMethodField()
    simulated_payload = serializers.SerializerMethodField()

    class Meta(GRPOPostingSerializer.Meta):
        fields = [*GRPOPostingSerializer.Meta.fields, "simulated", "simulated_payload"]

    def get_simulated(self, obj) -> bool:
        return is_simulated(obj)

    def get_simulated_payload(self, obj):
        if not is_simulated(obj):
            return None
        return obj.fa_record.sap_payload


class FAGRPOPostResponseSerializer(GRPOPostResponseSerializer):
    """grpo's post response, plus ``simulated``."""

    simulated = serializers.BooleanField()


class FAOpenPOItemSerializer(serializers.Serializer):
    """An open fixed-asset PO line, with what grpo's ``POItemSerializer`` leaves out."""

    po_item_code = serializers.CharField()
    item_name = serializers.CharField()
    ordered_qty = serializers.FloatField()
    received_qty = serializers.FloatField()
    remaining_qty = serializers.FloatField()
    uom = serializers.CharField(allow_blank=True)
    rate = serializers.FloatField()
    line_num = serializers.IntegerField()
    tax_code = serializers.CharField(allow_blank=True)
    warehouse_code = serializers.CharField(allow_blank=True)
    account_code = serializers.CharField(allow_blank=True)
    variety = serializers.CharField(allow_blank=True)


class FAOpenPOSerializer(serializers.Serializer):
    """An open PO of the supplier with lines in a fixed-asset warehouse."""

    po_number = serializers.CharField()
    supplier_code = serializers.CharField()
    supplier_name = serializers.CharField()
    doc_entry = serializers.IntegerField()
    branch_id = serializers.IntegerField(allow_null=True)
    vendor_ref = serializers.CharField(allow_blank=True)
    doc_date = serializers.DateField(allow_null=True)
    items = FAOpenPOItemSerializer(many=True)
