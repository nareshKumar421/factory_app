"""
stock_dashboard/serializers.py

DRF serializers for validating query parameters and shaping API responses.

Nearly everything here is read-only — the dashboard's figures come from SAP and
are never written back — so most classes are plain ``Serializer``. The one
exception is ``WarehouseBoardSettingsSerializer`` at the foot of the file, which
writes the two facts about a warehouse SAP does not hold: its rated tonnage
capacity, and the date stock was last physically verified.
"""

from django.utils import timezone
from rest_framework import serializers

from .models import LogisticsBoardSettings, WarehouseBoardSettings


# ---------------------------------------------------------------------------
# Query Parameter Serializers (Input Validation)
# ---------------------------------------------------------------------------


class StockDashboardFilterSerializer(serializers.Serializer):
    """Validates query parameters for the stock dashboard endpoint."""

    search = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Search by item code, item name, or warehouse code",
    )
    warehouse = serializers.CharField(
        required=False,
        default="",
        help_text="Comma-separated warehouse codes to filter by (e.g. 'WH-01,BH-PM')",
    )
    item_group = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        max_length=100,
        help_text="Item group name from OITB to filter by (e.g. 'PACKAGING MATERIAL')",
    )

    def validate_warehouse(self, value):
        if not value:
            return []
        return [w.strip() for w in value.split(",") if w.strip()]

    def validate_item_group(self, value):
        return value.strip() if value else ""

    status = serializers.CharField(
        required=False,
        default="",
        help_text="Comma-separated stock health statuses to filter by (e.g. 'low,critical')",
    )

    def validate_status(self, value):
        if not value:
            return []
        allowed = {"healthy", "low", "critical", "unset"}
        statuses = [s.strip() for s in value.split(",") if s.strip()]
        invalid = set(statuses) - allowed
        if invalid:
            raise serializers.ValidationError(
                f"Invalid status values: {', '.join(invalid)}. Allowed: {', '.join(sorted(allowed))}"
            )
        return statuses

    movement_status = serializers.CharField(
        required=False,
        default="",
        help_text="Comma-separated movement statuses to filter by (recent,slow)",
    )

    def validate_movement_status(self, value):
        if not value:
            return []
        allowed = {"recent", "slow"}
        statuses = [s.strip() for s in value.split(",") if s.strip()]
        invalid = set(statuses) - allowed
        if invalid:
            raise serializers.ValidationError(
                f"Invalid movement status values: {', '.join(invalid)}. Allowed: {', '.join(sorted(allowed))}"
            )
        return statuses

    sort_by = serializers.ChoiceField(
        choices=[
            "item_code",
            "item_name",
            "warehouse",
            "on_hand",
            "min_stock",
            "health_ratio",
        ],
        default="health_ratio",
        required=False,
    )
    sort_dir = serializers.ChoiceField(
        choices=["asc", "desc"],
        default="asc",
        required=False,
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)


class StockDashboardAsOfFilterSerializer(StockDashboardFilterSerializer):
    """Validates query parameters for historical SAP movement reconstruction."""

    as_of_date = serializers.DateField(
        required=True,
        help_text="Reconstruct stock as of this SAP posting date (YYYY-MM-DD).",
    )

    def validate_as_of_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("as_of_date cannot be in the future.")
        return value


class StockDashboardExportFilterSerializer(StockDashboardFilterSerializer):
    """Validates query parameters for the Excel export (as_of_date optional)."""

    as_of_date = serializers.DateField(
        required=False,
        help_text="Optional: export stock reconstructed as of this date (YYYY-MM-DD).",
    )

    def validate_as_of_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("as_of_date cannot be in the future.")
        return value


# ---------------------------------------------------------------------------
# Response Serializers (Output Shape)
# ---------------------------------------------------------------------------


class StockItemSerializer(serializers.Serializer):
    """One row per item-warehouse (or grouped item when multi-warehouse)."""

    item_code = serializers.CharField()
    item_name = serializers.CharField()
    warehouse = serializers.CharField(default="")
    on_hand = serializers.FloatField()
    min_stock = serializers.FloatField()
    uom = serializers.CharField()
    stock_status = serializers.CharField()
    health_ratio = serializers.FloatField()
    movement_status = serializers.CharField(default="slow")
    last_consumption_date = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        default=None,
    )
    days_since_last_consumption = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
    )
    # Grouped-only fields
    warehouse_count = serializers.IntegerField(default=1)
    has_warning = serializers.BooleanField(default=False)


class StockDashboardMetaSerializer(serializers.Serializer):
    total_items = serializers.IntegerField()
    healthy_count = serializers.IntegerField()
    low_stock_count = serializers.IntegerField()
    critical_stock_count = serializers.IntegerField()
    warehouses = serializers.ListField(child=serializers.CharField())
    fetched_at = serializers.CharField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    total_pages = serializers.IntegerField()
    # Present only on the experimental SAP reconstruction endpoint.
    as_of_date = serializers.CharField(required=False)
    reconstruction_note = serializers.CharField(required=False)


class StockDashboardResponseSerializer(serializers.Serializer):
    data = StockItemSerializer(many=True)
    meta = StockDashboardMetaSerializer()


# ---------------------------------------------------------------------------
# Item Detail (expand) Serializers
# ---------------------------------------------------------------------------


class ItemDetailFilterSerializer(serializers.Serializer):
    warehouse = serializers.CharField(
        required=True,
        help_text="Comma-separated warehouse codes",
    )

    def validate_warehouse(self, value):
        return [w.strip() for w in value.split(",") if w.strip()]


class ItemDetailResponseSerializer(serializers.Serializer):
    data = StockItemSerializer(many=True)


# ---------------------------------------------------------------------------
# Warehouse Occupancy Serializers
# ---------------------------------------------------------------------------


class WarehouseOccupancyFilterSerializer(serializers.Serializer):
    warehouse = serializers.CharField(
        required=True,
        max_length=8,
        help_text="One SAP warehouse code, e.g. BH-PF",
    )

    item_groups = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "Comma-separated SAP item group codes (OITM.ItmsGrpCod) to restrict "
            "to, e.g. 102,107,115. Omit for every group in the warehouse."
        ),
    )

    def validate_warehouse(self, value):
        cleaned = value.strip().upper()
        if not cleaned:
            raise serializers.ValidationError("A warehouse code is required.")
        return cleaned

    def validate_item_groups(self, value):
        """A list of integer group codes, or empty for "no restriction".

        Opt-in rather than a default, because this endpoint is shared: the
        Production Control board reads BH-PF through it and wants everything
        standing on the floor, while a stock-on-hand figure wants finished goods
        only. Defaulting either way would silently change the other board.
        """
        raw = (value or "").strip()
        if not raw:
            return []

        codes = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise serializers.ValidationError(
                    f"'{part}' is not an item group code. Use digits, e.g. 102,107,115."
                )
            codes.append(int(part))
        return codes


class WarehouseOccupancyItemSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    on_hand = serializers.FloatField()
    # Null where SAP holds no factor. 1 means the SKU is billed by the piece and
    # is NOT transacted in boxes -- callers must not divide those by a
    # boxes-per-pallet figure.
    pieces_per_box = serializers.FloatField(allow_null=True)
    # OITM.SalPackUn -- litres in one piece, used to tell a drum from a can from
    # a jar. Null where SAP holds no volume for the SKU.
    litres_per_piece = serializers.FloatField(allow_null=True)
    stock_value = serializers.FloatField()
    sub_group = serializers.CharField(allow_blank=True)
    uom = serializers.CharField(allow_blank=True)
    # OITM.U_Gross_Weight -- gross kg of one sales case, for boards that report a
    # warehouse in tonnes. Weight per piece is this over `pieces_per_box`. Null
    # where SAP records no weight for the SKU, or where the company carries no
    # such user-defined field at all; either way the caller must disclose the row
    # rather than count it as weightless.
    gross_weight_per_case = serializers.FloatField(allow_null=True)


class WarehouseOccupancyMetaSerializer(serializers.Serializer):
    warehouse = serializers.CharField()
    # Echoed back so a caller can tell a filtered answer from an unfiltered one.
    item_groups = serializers.ListField(child=serializers.IntegerField(), required=False)
    item_count = serializers.IntegerField()
    total_on_hand = serializers.FloatField()
    total_value = serializers.FloatField()
    loose_items = serializers.IntegerField()
    unconfigured_items = serializers.IntegerField()
    # How much of a tonnage total rests on nothing: SKUs with no case weight,
    # and rows whose on-hand is a mass or volume where a pack factor does not
    # apply. A board showing tonnes has to show these beside it.
    unweighed_items = serializers.IntegerField()
    non_piece_items = serializers.IntegerField()
    fetched_at = serializers.CharField()


class WarehouseOccupancyResponseSerializer(serializers.Serializer):
    data = WarehouseOccupancyItemSerializer(many=True)
    meta = WarehouseOccupancyMetaSerializer()


# ---------------------------------------------------------------------------
# Item Batch Serializers
# ---------------------------------------------------------------------------


class ItemBatchFilterSerializer(serializers.Serializer):
    warehouse = serializers.CharField(required=True, max_length=8)

    def validate_warehouse(self, value):
        cleaned = value.strip().upper()
        if not cleaned:
            raise serializers.ValidationError("A warehouse code is required.")
        return cleaned


class ItemBatchSerializer(serializers.Serializer):
    batch = serializers.CharField(allow_blank=True)
    quantity = serializers.FloatField()
    # Null where SAP holds no manufacturing date -- never the receipt date in
    # its place. `mfg_date_source` names which date `age_days` was measured from.
    mfg_date = serializers.CharField(allow_null=True)
    in_date = serializers.CharField(allow_null=True)
    exp_date = serializers.CharField(allow_null=True)
    mfg_date_source = serializers.CharField()
    notes = serializers.CharField(allow_blank=True)
    committed = serializers.FloatField()
    age_days = serializers.IntegerField(allow_null=True)
    days_to_expiry = serializers.IntegerField(allow_null=True)


class ItemBatchMetaSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    warehouse = serializers.CharField()
    batch_count = serializers.IntegerField()
    total_quantity = serializers.FloatField()
    without_mfg_date = serializers.IntegerField()
    without_exp_date = serializers.IntegerField()
    oldest_age_days = serializers.IntegerField(allow_null=True)
    fetched_at = serializers.CharField()


class ItemMovementSerializer(serializers.Serializer):
    date = serializers.CharField(allow_null=True)
    trans_type = serializers.IntegerField()
    label = serializers.CharField()
    in_qty = serializers.FloatField()
    out_qty = serializers.FloatField()
    # Taken from the quantity, never the transaction type -- a transfer goes
    # both ways. NONE means the row moved no stock (revaluation, order posting).
    direction = serializers.ChoiceField(choices=["IN", "OUT", "NONE"])
    doc_ref = serializers.CharField(allow_blank=True)


class ItemBatchResponseSerializer(serializers.Serializer):
    data = ItemBatchSerializer(many=True)
    movements = ItemMovementSerializer(many=True)
    meta = ItemBatchMetaSerializer()


# ============================================================================
# Warehouse Board Settings
# ============================================================================


class WarehouseBoardSettingsSerializer(serializers.ModelSerializer):
    """The two operator-typed facts about a warehouse, plus who last set them.

    `capacity_tonnes` is deliberately returned as a float rather than the
    Decimal string DRF defaults to: the board divides stock by it, and a client
    that has to parse a string before doing arithmetic eventually forgets to.
    """

    capacity_tonnes = serializers.FloatField(allow_null=True, required=False)
    updated_by_name = serializers.CharField(
        source="updated_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = WarehouseBoardSettings
        fields = [
            "warehouse",
            "capacity_tonnes",
            "last_audit_date",
            "updated_at",
            "updated_by_name",
        ]
        read_only_fields = ["warehouse", "updated_at", "updated_by_name"]

    def validate_capacity_tonnes(self, value):
        # Zero is rejected rather than stored: it would make the warehouse read
        # as infinitely full, and "no rated capacity" already has a
        # representation — null.
        if value is not None and value <= 0:
            raise serializers.ValidationError(
                "Capacity must be greater than zero. Leave it empty if the "
                "warehouse has no rated figure."
            )
        return value

    def validate_last_audit_date(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("The last audit cannot be in the future.")
        return value


class LogisticsBoardSettingsSerializer(serializers.ModelSerializer):
    """The company-level board figures, with the daily costs derived.

    Salary is typed monthly because that is how it is agreed and quoted, and
    served per DAY as well because that is what the board shows. The divisor is
    30, matching `factory_expense.services`, which spreads a monthly rate the
    same way -- a board whose two salary lines used different month lengths
    would not add up.
    """

    labour_rate_per_day = serializers.FloatField(allow_null=True, required=False)
    warehouse_salary_monthly = serializers.FloatField(allow_null=True, required=False)
    dispatch_salary_monthly = serializers.FloatField(allow_null=True, required=False)
    transport_salary_monthly = serializers.FloatField(allow_null=True, required=False)

    warehouse_salary_daily = serializers.SerializerMethodField()
    dispatch_salary_daily = serializers.SerializerMethodField()
    transport_salary_daily = serializers.SerializerMethodField()

    updated_by_name = serializers.CharField(
        source="updated_by.full_name", read_only=True, default=""
    )

    DAYS_IN_MONTH = 30

    class Meta:
        model = LogisticsBoardSettings
        fields = [
            "owned_vehicles",
            "owned_vehicle_numbers",
            "vehicles_out_of_service",
            "labour_rate_per_day",
            "warehouse_employees",
            "warehouse_salary_monthly",
            "warehouse_salary_daily",
            "dispatch_employees",
            "dispatch_salary_monthly",
            "dispatch_salary_daily",
            "transport_employees",
            "transport_salary_monthly",
            "transport_salary_daily",
            "updated_at",
            "updated_by_name",
        ]
        read_only_fields = ["updated_at", "updated_by_name"]

    def _daily(self, monthly):
        if monthly is None:
            return None
        return round(float(monthly) / self.DAYS_IN_MONTH, 2)

    def get_warehouse_salary_daily(self, obj):
        return self._daily(obj.warehouse_salary_monthly)

    def get_dispatch_salary_daily(self, obj):
        return self._daily(obj.dispatch_salary_monthly)

    def get_transport_salary_daily(self, obj):
        return self._daily(obj.transport_salary_monthly)

    def _non_negative(self, value, label):
        # Zero is allowed here, unlike a warehouse capacity: a section really can
        # have nobody in it, and that is different from nobody having said.
        if value is not None and value < 0:
            raise serializers.ValidationError(f"{label} cannot be negative.")
        return value

    def validate_labour_rate_per_day(self, value):
        return self._non_negative(value, "Labour rate")

    def validate_warehouse_salary_monthly(self, value):
        return self._non_negative(value, "Salary")

    def validate_dispatch_salary_monthly(self, value):
        return self._non_negative(value, "Salary")

    def validate_transport_salary_monthly(self, value):
        return self._non_negative(value, "Salary")
