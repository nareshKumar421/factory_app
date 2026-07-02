from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import LabourGateAudit, LabourGateEntry, LabourGateOutBatch

# How long after a labour-out batch is recorded it can still be undone.
UNDO_WINDOW_MINUTES = 10


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
    is_deleted = serializers.SerializerMethodField()
    can_undo_last = serializers.SerializerMethodField()
    can_restore = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()
    deleted_by_name = serializers.SerializerMethodField()

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
            "is_deleted",
            "can_undo_last",
            "can_restore",
            "created_by_name",
            "updated_by_name",
            "deleted_by_name",
            "deleted_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_is_deleted(self, obj):
        return not obj.is_active

    def get_can_restore(self, obj):
        # A soft-deleted row can be undone only within the grace window.
        if obj.is_active or not obj.deleted_at:
            return False
        return timezone.now() - obj.deleted_at <= timedelta(minutes=UNDO_WINDOW_MINUTES)

    def get_can_undo_last(self, obj):
        # The most recent out batch can be undone only within the grace window.
        # Computed in Python to reuse the prefetched out_batches cache.
        batches = list(obj.out_batches.all())
        if not batches:
            return False
        last = max(batches, key=lambda b: (b.created_at, b.id))
        return timezone.now() - last.created_at <= timedelta(minutes=UNDO_WINDOW_MINUTES)

    def get_created_by_name(self, obj):
        return obj.created_by.full_name if obj.created_by else None

    def get_updated_by_name(self, obj):
        return obj.updated_by.full_name if obj.updated_by else None

    def get_deleted_by_name(self, obj):
        return obj.deleted_by.full_name if obj.deleted_by else None


class LabourGateAuditSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LabourGateAudit
        fields = (
            "id",
            "action",
            "action_display",
            "detail",
            "old_value",
            "new_value",
            "performed_by_name",
            "created_at",
        )

    def get_performed_by_name(self, obj):
        return obj.performed_by.full_name if obj.performed_by else None


# ---- request serializers ----

class LabourInSerializer(serializers.Serializer):
    """Create-or-update a contractor's labour-in count for a day.

    ``department`` is optional: the gate Labour In records contractor totals with
    no department, while the Labour module records the per-department split.
    """
    department = serializers.IntegerField(required=False, allow_null=True)
    contractor = serializers.IntegerField()
    work_date = serializers.DateField()
    count_in = serializers.IntegerField(min_value=0)


class UpdateInSerializer(serializers.Serializer):
    """Edit the labour-in count on an existing entry."""
    count_in = serializers.IntegerField(min_value=0)


class OutBatchSerializer(serializers.Serializer):
    """Add one batch of people leaving the gate to an entry."""
    count = serializers.IntegerField(min_value=1)
