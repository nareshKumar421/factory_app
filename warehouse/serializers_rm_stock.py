"""Serializers for the raw-material stock register."""

from rest_framework import serializers

from .models_rm_stock import RawMaterialStock, RawMaterialStockEntry


class RawMaterialStockSerializer(serializers.ModelSerializer):
    """One register row, with enough about the last change to render it."""

    company_code = serializers.CharField(source="company.code", read_only=True)
    set_by_name = serializers.SerializerMethodField()

    class Meta:
        model = RawMaterialStock
        fields = [
            "id",
            "company",
            "company_code",
            "warehouse_code",
            "item_code",
            "item_name",
            "uom",
            "qty",
            "as_of_date",
            "remarks",
            "is_active",
            "set_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_set_by_name(self, obj) -> str:
        user = obj.set_by
        if not user:
            return ""
        # `accounts.User` has no `get_full_name()` — it is a plain field.
        return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


class RawMaterialStockEntrySerializer(serializers.ModelSerializer):
    """One change to a quantity, for the history panel."""

    changed_by_name = serializers.SerializerMethodField()
    qty_delta = serializers.DecimalField(
        max_digits=18, decimal_places=3, read_only=True, allow_null=True
    )

    class Meta:
        model = RawMaterialStockEntry
        fields = [
            "id",
            "warehouse_code",
            "item_code",
            "action",
            "previous_qty",
            "qty",
            "qty_delta",
            "as_of_date",
            "remarks",
            "changed_by_name",
            "changed_at",
        ]
        read_only_fields = fields

    def get_changed_by_name(self, obj) -> str:
        user = obj.changed_by
        if not user:
            return ""
        return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


class SetRawMaterialStockSerializer(serializers.Serializer):
    """What the page posts when a keeper sets a quantity.

    The item's name and UoM come along from the picker so the register can be
    read without SAP, but they are optional — a save is about the quantity, and
    a client that omits them must not blank what is already stored.

    `qty` is a `DecimalField` with `min_value=0` rather than a float: raw
    material is weighed, and a float would round 12.345 kg into the register.
    """

    # Optional: the register covers one warehouse and the service supplies it.
    # A client may still send it, and a code that is not the register's is
    # refused there rather than quietly rewritten.
    warehouse_code = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=''
    )
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    uom = serializers.CharField(
        max_length=20, required=False, allow_blank=True, default=""
    )
    qty = serializers.DecimalField(max_digits=18, decimal_places=3, min_value=0)
    as_of_date = serializers.DateField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_warehouse_code(self, value: str) -> str:
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("Name the warehouse.")
        return code

    def validate_item_code(self, value: str) -> str:
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("Choose an item.")
        return code
