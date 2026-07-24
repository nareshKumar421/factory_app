"""Serializers for the Online Quality Monitoring module."""

from rest_framework import serializers

from quality_control.models.online_monitoring import (
    OnlineQualityRecord,
    OnlineQualityReading,
    OnlineQualityTorque,
    OnlineQualitySpec,
)

# Numeric water-quality fields validated against the spec master, plus torque.
WATER_QUALITY_KEYS = [
    "ph", "tds", "turbidity", "alkalinity",
    "total_hardness", "calcium", "magnesium", "chloride",
]


class OnlineQualitySpecSerializer(serializers.ModelSerializer):
    class Meta:
        model = OnlineQualitySpec
        fields = [
            "id", "company", "parameter_key", "parameter_name", "unit",
            "min_value", "max_value", "specification_text", "validation_type",
            "sequence", "is_active",
        ]
        read_only_fields = ["id"]


class OnlineQualityTorqueSerializer(serializers.ModelSerializer):
    class Meta:
        model = OnlineQualityTorque
        fields = ["id", "head_no", "torque_value"]
        read_only_fields = ["id"]


class OnlineQualityReadingSerializer(serializers.ModelSerializer):
    """Read + write. On write, ``torque_heads`` fully replaces a reading's heads."""

    torque_heads = OnlineQualityTorqueSerializer(many=True, required=False)

    class Meta:
        model = OnlineQualityReading
        fields = [
            "id", "reading_time", "filler_speed",
            "taste", "aroma", "appearance",
            *WATER_QUALITY_KEYS,
            "package_attribute", "date_code", "rub_test", "closure_jump_test",
            "remarks", "torque_heads",
        ]
        read_only_fields = ["id"]

    def _sync_torque(self, reading, heads):
        reading.torque_heads.all().delete()
        OnlineQualityTorque.objects.bulk_create([
            OnlineQualityTorque(
                reading=reading,
                head_no=head["head_no"],
                torque_value=head.get("torque_value"),
                created_by=reading.updated_by or reading.created_by,
            )
            for head in heads
        ])

    def create(self, validated_data):
        heads = validated_data.pop("torque_heads", [])
        reading = OnlineQualityReading.objects.create(**validated_data)
        self._sync_torque(reading, heads)
        return reading

    def update(self, instance, validated_data):
        heads = validated_data.pop("torque_heads", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if heads is not None:
            self._sync_torque(instance, heads)
        return instance


class OnlineQualityRecordListSerializer(serializers.ModelSerializer):
    line_name = serializers.CharField(source="production_line.name", read_only=True)
    reading_count = serializers.IntegerField(source="readings.count", read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = OnlineQualityRecord
        fields = [
            "id", "date", "production_line", "line_name",
            "sku", "product_name", "flavour", "shift", "batch_no",
            "status", "reading_count", "created_by_name", "created_at",
        ]


class OnlineQualityRecordSerializer(serializers.ModelSerializer):
    """Full record with nested readings (+ torque)."""

    readings = OnlineQualityReadingSerializer(many=True, read_only=True)
    line_name = serializers.CharField(source="production_line.name", read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, allow_null=True, default=None
    )
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", read_only=True, allow_null=True, default=None
    )
    approved_by_name = serializers.CharField(
        source="approved_by.full_name", read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = OnlineQualityRecord
        fields = [
            "id", "company", "production_line", "line_name", "date",
            "sku", "product_name", "flavour", "shift", "batch_no",
            "status", "remarks",
            "submitted_by_name", "submitted_at",
            "approved_by_name", "approved_at", "approval_remarks",
            "rejection_remarks", "created_by_name", "created_at", "updated_at",
            "readings",
        ]
        read_only_fields = [
            "id", "company", "status", "submitted_at", "approved_at",
            "approval_remarks", "rejection_remarks", "created_at", "updated_at",
        ]


class OnlineQualityRecordCreateSerializer(serializers.Serializer):
    """Create a record header (readings are added separately)."""

    production_line_id = serializers.IntegerField()
    date = serializers.DateField()
    sku = serializers.CharField(required=False, allow_blank=True, default="")
    product_name = serializers.CharField(required=False, allow_blank=True, default="")
    flavour = serializers.CharField(required=False, allow_blank=True, default="")
    shift = serializers.CharField(required=False, allow_blank=True, default="")
    batch_no = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class OnlineQualityApprovalSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
