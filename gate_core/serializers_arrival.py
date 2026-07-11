from rest_framework import serializers

from gate_core.models import VehicleArrival


class VehicleArrivalCreateSerializer(serializers.Serializer):
    vehicle_id = serializers.IntegerField()
    driver_id = serializers.IntegerField()
    gate_in_date = serializers.DateField()
    in_time = serializers.TimeField()
    tare_weight = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, allow_null=True
    )
    weighbridge_slip_no = serializers.CharField(required=False, allow_blank=True, default="")
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class VehicleArrivalGateInSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    entry_no = serializers.CharField()
    company_id = serializers.IntegerField()
    company_code = serializers.CharField(source="company.code")
    company_name = serializers.CharField(source="company.name")
    retired_at = serializers.DateTimeField()
    cover_count = serializers.SerializerMethodField()

    def get_cover_count(self, obj):
        # Count from the prefetched ``covers`` (``.all()`` + a Python filter); a
        # ``.filter(...).count()`` re-queries per gate-in and defeats the prefetch.
        return sum(1 for cover in obj.covers.all() if cover.is_active)


class VehicleArrivalGateOutSerializer(serializers.Serializer):
    """Light per-company docking summary for the combined-gatepass view."""

    id = serializers.IntegerField()
    entry_no = serializers.CharField()
    company_id = serializers.IntegerField()
    company_code = serializers.CharField(source="company.code")
    company_name = serializers.CharField(source="company.name")
    status = serializers.CharField()
    gatepass_no = serializers.CharField()
    sap_doc_num = serializers.CharField()


class VehicleArrivalSerializer(serializers.ModelSerializer):
    gate_ins = VehicleArrivalGateInSerializer(many=True, read_only=True)
    gate_outs = VehicleArrivalGateOutSerializer(many=True, read_only=True)
    vehicle_no = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)

    class Meta:
        model = VehicleArrival
        fields = [
            "id",
            "arrival_no",
            "vehicle",
            "vehicle_no",
            "driver",
            "driver_name",
            "gate_in_date",
            "in_time",
            "tare_weight",
            "weighbridge_slip_no",
            "security_name",
            "remarks",
            "status",
            "gate_out_date",
            "out_time",
            "departed_at",
            "gatepass_no",
            "gatepass_printed_at",
            "gatepass_committed_at",
            "gate_ins",
            "gate_outs",
        ]
