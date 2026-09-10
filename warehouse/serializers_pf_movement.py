"""Serializers for the godown outward-movement register."""

from rest_framework import serializers

from company.models import Company

from .models_pf_movement import (
    PFStockMovement,
    PFStockMovementEvent,
    PFStockMovementLine,
)


def _person(user) -> str:
    if not user:
        return ""
    # `accounts.User` has no `get_full_name()` — it is a plain field.
    return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

class PFStockMovementLineSerializer(serializers.ModelSerializer):
    """One item line, with the pieces its snapshotted pack size implies."""

    pieces = serializers.SerializerMethodField()

    class Meta:
        model = PFStockMovementLine
        fields = [
            "id",
            "item_code",
            "item_name",
            "uom",
            "boxes",
            "pieces_per_box",
            "pieces",
            "remarks",
        ]
        read_only_fields = fields

    def get_pieces(self, obj):
        """Null, not zero, when SAP had no pack size to snapshot."""
        return obj.pieces


class PFStockMovementEventSerializer(serializers.ModelSerializer):
    """One change to a document, for the trail panel."""

    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PFStockMovementEvent
        fields = [
            "id",
            "action",
            "movement_date",
            "to_warehouse",
            "line_count",
            "total_boxes",
            "note",
            "changed_by_name",
            "changed_at",
        ]
        read_only_fields = fields

    def get_changed_by_name(self, obj) -> str:
        return _person(obj.changed_by)


class PFStockMovementSerializer(serializers.ModelSerializer):
    """One declared consignment with its lines."""

    company_code = serializers.CharField(source="company.code", read_only=True)
    to_company_code = serializers.CharField(source="to_company.code", read_only=True)
    to_company_name = serializers.CharField(source="to_company.name", read_only=True)
    lines = PFStockMovementLineSerializer(many=True, read_only=True)
    total_boxes = serializers.SerializerMethodField()
    line_count = serializers.SerializerMethodField()
    is_cross_company = serializers.BooleanField(read_only=True)
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()
    cancelled_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PFStockMovement
        fields = [
            "id",
            "entry_no",
            "company",
            "company_code",
            "movement_date",
            "from_warehouse",
            "from_warehouse_name",
            "to_warehouse",
            "to_warehouse_name",
            "to_company",
            "to_company_code",
            "to_company_name",
            "is_cross_company",
            "vehicle_no",
            "remarks",
            "lines",
            "line_count",
            "total_boxes",
            "is_active",
            "cancelled_at",
            "cancelled_by_name",
            "cancellation_reason",
            "created_by_name",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    # Summed off the prefetched lines rather than with an annotation, so the
    # list endpoint stays at two queries whatever the filter.
    def get_total_boxes(self, obj) -> int:
        return sum(line.boxes for line in obj.lines.all())

    def get_line_count(self, obj) -> int:
        return len(obj.lines.all())

    def get_created_by_name(self, obj) -> str:
        return _person(obj.created_by)

    def get_updated_by_name(self, obj) -> str:
        return _person(obj.updated_by)

    def get_cancelled_by_name(self, obj) -> str:
        return _person(obj.cancelled_by)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

class PFStockMovementLineInputSerializer(serializers.Serializer):
    """One line as the form posts it.

    ``item_name``, ``uom`` and ``pieces_per_box`` come along from the SAP picker
    rather than being asked for: the register has to stay readable without SAP,
    and the pack size has to be the one that was true when the line was typed.
    They are optional so a client that did not look the item up can still file.
    """

    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    uom = serializers.CharField(
        max_length=20, required=False, allow_blank=True, default=""
    )
    # Boxes are whole. An IntegerField rather than a Decimal because the floor
    # does not send part boxes between godowns, and a decimal here would invite
    # a piece count to be typed into a box column.
    boxes = serializers.IntegerField(min_value=1)
    pieces_per_box = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, default=None
    )
    remarks = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )

    def validate_item_code(self, value: str) -> str:
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("Choose an item.")
        return code


class PFStockMovementCreateSerializer(serializers.Serializer):
    """What the page posts to file a movement."""

    # Optional: the service falls back to the configured PF floor, so a client
    # that only ever files from one warehouse need not name it.
    from_warehouse = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    to_warehouse = serializers.CharField(max_length=50)
    # The destination's company. Required, because the same warehouse code
    # exists in more than one schema and a bare code would not say which godown
    # is meant.
    to_company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.filter(is_active=True)
    )
    from_warehouse_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    to_warehouse_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    movement_date = serializers.DateField(required=False, allow_null=True)
    vehicle_no = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    lines = PFStockMovementLineInputSerializer(many=True, allow_empty=False)

    def validate_to_warehouse(self, value: str) -> str:
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("Name the godown the stock is going to.")
        return code


class PFStockMovementUpdateSerializer(serializers.Serializer):
    """A correction to a filed movement — every field optional.

    ``from_warehouse`` is absent on purpose: see
    ``pf_movement_service.update_movement`` for why the source cannot move.
    """

    to_warehouse = serializers.CharField(max_length=50, required=False)
    to_company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.filter(is_active=True), required=False
    )
    to_warehouse_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )
    movement_date = serializers.DateField(required=False)
    vehicle_no = serializers.CharField(max_length=50, required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)
    # Omitted leaves the lines alone; supplied replaces them wholesale.
    lines = PFStockMovementLineInputSerializer(many=True, required=False, allow_empty=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_to_warehouse(self, value: str) -> str:
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("Name the godown the stock is going to.")
        return code
