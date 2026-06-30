from rest_framework import serializers

from .models import LabourCountSheet, LabourCountItem, LabourOutBatch
from .services import lock


class LabourCountItemSerializer(serializers.ModelSerializer):
    contractor_name = serializers.CharField(source="contractor.contractor_name", read_only=True)

    class Meta:
        model = LabourCountItem
        fields = ("id", "contractor", "contractor_name", "count")


class LabourOutBatchSerializer(serializers.ModelSerializer):
    by = serializers.SerializerMethodField()

    class Meta:
        model = LabourOutBatch
        fields = ("id", "count", "created_at", "by")

    def get_by(self, obj):
        return obj.created_by.full_name if obj.created_by else None


class LabourCountSheetSerializer(serializers.ModelSerializer):
    items = LabourCountItemSerializer(many=True, read_only=True)
    out_batches = LabourOutBatchSerializer(many=True, read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    total_count = serializers.IntegerField(read_only=True)
    variance = serializers.SerializerMethodField()
    remaining = serializers.SerializerMethodField()
    verified_by_name = serializers.SerializerMethodField()
    is_editable = serializers.SerializerMethodField()
    can_pull_back = serializers.SerializerMethodField()

    class Meta:
        model = LabourCountSheet
        fields = (
            "id",
            "company",
            "department",
            "department_name",
            "work_date",
            "shift",
            "status",
            "is_holiday",
            "lock_at",
            "submitted_at",
            "auto_submitted",
            "verified_at",
            "verified_by_name",
            "gate_counted",
            "variance",
            "remaining",
            "verify_remark",
            "total_count",
            "items",
            "out_batches",
            "is_editable",
            "can_pull_back",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_variance(self, obj):
        # gate_counted - submitted total; null until the gate has counted.
        if obj.gate_counted is None:
            return None
        return obj.gate_counted - obj.total_count

    def get_remaining(self, obj):
        # How many are still to be marked out (can go negative if over-counted).
        return obj.total_count - (obj.gate_counted or 0)

    def get_verified_by_name(self, obj):
        return obj.verified_by.full_name if obj.verified_by else None

    def get_is_editable(self, obj):
        return lock.is_editable(obj)

    def get_can_pull_back(self, obj):
        return lock.can_pull_back(obj)


# ---- request serializers ----

class EnsureSheetSerializer(serializers.Serializer):
    work_date = serializers.DateField()
    shift = serializers.ChoiceField(choices=LabourCountSheet._meta.get_field("shift").choices)
    department = serializers.IntegerField()


class _ItemInput(serializers.Serializer):
    contractor = serializers.IntegerField()
    count = serializers.IntegerField(min_value=0)


class ItemsUpsertSerializer(serializers.Serializer):
    items = _ItemInput(many=True)


class MarkOutSerializer(serializers.Serializer):
    """Add one batch of people leaving the gate to a department sheet."""
    sheet = serializers.IntegerField()
    count = serializers.IntegerField(min_value=1)


class FinalizeSerializer(serializers.Serializer):
    """Close a department sheet at the gate (locks the running out count)."""
    sheet = serializers.IntegerField()
    remark = serializers.CharField(required=False, allow_blank=True, default="")
