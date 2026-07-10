from rest_framework import serializers
from .models import Weighment


class WeighmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Weighment
        fields = [
            "id",
            "gross_weight",
            "tare_weight",
            "net_weight",
            "challan_weight",
            "weighbridge_slip_no",
            "first_weighment_time",
            "second_weighment_time",
            "remarks",
            "created_at",
            "updated_at",
        ]
        read_only_fields = (
            "id",
            "net_weight",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs):
        gross_weight = attrs.get("gross_weight", getattr(self.instance, "gross_weight", None))
        tare_weight = attrs.get("tare_weight", getattr(self.instance, "tare_weight", None))

        if gross_weight is not None and gross_weight < 0:
            raise serializers.ValidationError({"gross_weight": "Gross weight cannot be negative."})
        if tare_weight is not None and tare_weight < 0:
            raise serializers.ValidationError({"tare_weight": "Tare weight cannot be negative."})
        if gross_weight is not None and tare_weight is not None and tare_weight > gross_weight:
            raise serializers.ValidationError(
                {"tare_weight": "Tare weight cannot be greater than gross weight."}
            )

        challan_weight = attrs.get("challan_weight")
        if challan_weight is not None and challan_weight < 0:
            raise serializers.ValidationError(
                {"challan_weight": "Challan weight cannot be negative."}
            )

        return attrs
