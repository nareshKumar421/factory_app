from rest_framework import serializers

from .models import LabourGateEntry, LabourGateOutBatch


class LabourOutBatchSerializer(serializers.ModelSerializer):
    by = serializers.SerializerMethodField()

    class Meta:
        model = LabourGateOutBatch
        fields = ("id", "count", "created_at", "by")

    def get_by(self, obj):
        return obj.created_by.full_name if obj.created_by else None


class LabourGateEntrySerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    contractor_name = serializers.CharField(
        source="contractor.contractor_name", read_only=True
    )
    total_out = serializers.IntegerField(read_only=True)
    remaining = serializers.IntegerField(read_only=True)
    out_batches = LabourOutBatchSerializer(many=True, read_only=True)

    class Meta:
        model = LabourGateEntry
        fields = (
            "id",
            "company",
            "department",
            "department_name",
            "contractor",
            "contractor_name",
            "work_date",
            "count_in",
            "total_out",
            "remaining",
            "out_batches",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


# ---- request serializers ----

class LabourInSerializer(serializers.Serializer):
    """Create-or-update a contractor's labour-in count for a department/day."""
    department = serializers.IntegerField()
    contractor = serializers.IntegerField()
    work_date = serializers.DateField()
    count_in = serializers.IntegerField(min_value=0)


class UpdateInSerializer(serializers.Serializer):
    """Edit the labour-in count on an existing entry."""
    count_in = serializers.IntegerField(min_value=0)


class OutBatchSerializer(serializers.Serializer):
    """Add one batch of people leaving the gate to an entry."""
    count = serializers.IntegerField(min_value=1)
