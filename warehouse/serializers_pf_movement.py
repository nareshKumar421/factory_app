"""Serializers for the godown outward-movement register."""

from decimal import Decimal

from rest_framework import serializers

from company.models import Company

from .models_pf_movement import (
    PFMovementDestinationKind,
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
    """One item line: the pieces typed, plus what its snapshots make of them."""

    # Both null rather than zero when SAP had no factor to snapshot — the
    # difference between "not measured that way" and "none of it".
    litres = serializers.SerializerMethodField()
    full_boxes = serializers.SerializerMethodField()
    loose_pieces = serializers.SerializerMethodField()

    class Meta:
        model = PFStockMovementLine
        fields = [
            "id",
            "item_code",
            "item_name",
            "uom",
            "pieces",
            "pieces_per_box",
            "full_boxes",
            "loose_pieces",
            "litres_per_piece",
            "litres",
            "remarks",
        ]
        read_only_fields = fields

    def get_litres(self, obj):
        """Litres on the line, or null for an item SAP holds no volume for."""
        litres = obj.litres
        # A string, not a float: the factor has 6 decimal places and a float
        # would round a 0.8242-litre pouch differently on every client.
        return None if litres is None else str(litres.quantize(Decimal("0.001")))

    def get_full_boxes(self, obj):
        return obj.full_boxes

    def get_loose_pieces(self, obj):
        return obj.loose_pieces


class PFStockMovementEventSerializer(serializers.ModelSerializer):
    """One change to a document, for the trail panel."""

    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PFStockMovementEvent
        fields = [
            "id",
            "action",
            "movement_date",
            "destination_kind",
            "to_warehouse",
            "line_count",
            "total_pieces",
            "total_litres",
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
    # `default=""` rather than a plain source: `to_company` is null on a
    # dispatch, and traversing `to_company.code` through a null relation raises
    # instead of answering blank.
    to_company_code = serializers.CharField(
        source="to_company.code", read_only=True, default=""
    )
    to_company_name = serializers.CharField(
        source="to_company.name", read_only=True, default=""
    )
    lines = PFStockMovementLineSerializer(many=True, read_only=True)
    # "Dispatch" where there is no destination godown. A blank cell reads as
    # missing data rather than as the answer.
    destination_display = serializers.CharField(read_only=True)
    is_dispatch = serializers.BooleanField(read_only=True)
    total_pieces = serializers.SerializerMethodField()
    total_litres = serializers.SerializerMethodField()
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
            "destination_kind",
            "destination_display",
            "is_dispatch",
            "to_warehouse",
            "to_warehouse_name",
            "to_company",
            "to_company_code",
            "to_company_name",
            "is_cross_company",
            "vehicle_no",
            "reference",
            "remarks",
            "lines",
            "line_count",
            "total_pieces",
            "total_litres",
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
    def get_total_pieces(self, obj) -> int:
        return sum(line.pieces for line in obj.lines.all())

    def get_total_litres(self, obj) -> str:
        """Litres over the document, skipping items SAP holds no volume for."""
        return str(obj.total_litres.quantize(Decimal("0.001")))

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

    ``item_name``, ``uom``, ``pieces_per_box`` and ``litres_per_piece`` come
    along from the SAP picker rather than being asked for: the register has to
    stay readable without SAP, and both factors have to be the ones that were
    true when the line was typed. All four are optional so a client that did not
    look the item up can still file — it just gets no box or litre equivalent.
    """

    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    uom = serializers.CharField(
        max_length=20, required=False, allow_blank=True, default=""
    )
    # Pieces are whole — SAP counts them in PCS and there is no half bottle.
    # An IntegerField rather than a Decimal for that reason, and because a
    # decimal here would invite a litre figure to be typed into a piece column.
    pieces = serializers.IntegerField(min_value=1)
    pieces_per_box = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, default=None
    )
    # From SAP, never typed. 6 places to match `OITM.SalPackUn` exactly, so a
    # 750 GMS pouch keeps its 0.824200 instead of being rounded on the way in.
    litres_per_piece = serializers.DecimalField(
        max_digits=12,
        decimal_places=6,
        required=False,
        allow_null=True,
        min_value=0,
        default=None,
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
    destination_kind = serializers.ChoiceField(
        choices=PFMovementDestinationKind.choices,
        required=False,
        default=PFMovementDestinationKind.GODOWN,
    )
    # Both optional here, and both required by the service when the kind is
    # GODOWN. The pairing rule lives in one place (`_clean_route`) rather than
    # being half-stated as `required=True` and half-enforced there.
    to_warehouse = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    # The destination's company, because the same warehouse code exists in more
    # than one schema and a bare code would not say which godown is meant. Null
    # on a dispatch, which has no destination godown.
    to_company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.filter(is_active=True),
        required=False,
        allow_null=True,
        default=None,
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
    reference = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    lines = PFStockMovementLineInputSerializer(many=True, allow_empty=False)

    def validate_to_warehouse(self, value: str) -> str:
        # Blank is allowed through — a dispatch has no destination — and the
        # service refuses a blank one when the kind demands a godown.
        return (value or "").strip().upper()


class PFStockMovementUpdateSerializer(serializers.Serializer):
    """A correction to a filed movement — every field optional.

    ``from_warehouse`` is absent on purpose: see
    ``pf_movement_service.update_movement`` for why the source cannot move.

    Sending ``destination_kind: DISPATCH`` alone drops the destination godown.
    Sending it together with a ``to_warehouse`` is a contradiction and the
    service refuses it rather than picking one.
    """

    destination_kind = serializers.ChoiceField(
        choices=PFMovementDestinationKind.choices, required=False
    )
    to_warehouse = serializers.CharField(max_length=50, required=False)
    to_company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.filter(is_active=True), required=False
    )
    to_warehouse_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )
    movement_date = serializers.DateField(required=False)
    vehicle_no = serializers.CharField(max_length=50, required=False, allow_blank=True)
    reference = serializers.CharField(max_length=50, required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)
    # Omitted leaves the lines alone; supplied replaces them wholesale.
    lines = PFStockMovementLineInputSerializer(many=True, required=False, allow_empty=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_to_warehouse(self, value: str) -> str:
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("Name the godown the stock is going to.")
        return code
