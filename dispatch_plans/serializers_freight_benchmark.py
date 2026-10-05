"""
dispatch_plans/serializers_freight_benchmark.py

Read and write shapes for the freight benchmark page. Amounts go out as numbers,
not strings: the page adds nothing up, it only formats them.
"""

from decimal import Decimal

from rest_framework import serializers

from .models_freight_benchmark import (
    FreightBenchmark,
    FreightDestination,
    FreightRateBasis,
    FreightSlab,
)


class FreightSlabSerializer(serializers.ModelSerializer):
    # How many destinations hold a rate on it -- what stops a delete.
    destination_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = FreightSlab
        fields = [
            "id",
            "label",
            "above_kg",
            "up_to_kg",
            "sort_order",
            "is_active",
            "destination_count",
        ]


class FreightSlabWriteSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=40)
    above_kg = serializers.IntegerField(min_value=0, default=0)
    up_to_kg = serializers.IntegerField(min_value=1)
    sort_order = serializers.IntegerField(min_value=0, default=0)
    is_active = serializers.BooleanField(default=True)


class FreightBenchmarkSerializer(serializers.ModelSerializer):
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, coerce_to_string=False
    )

    class Meta:
        model = FreightBenchmark
        fields = ["slab", "basis", "amount"]


class FreightDestinationSerializer(serializers.ModelSerializer):
    rates = FreightBenchmarkSerializer(source="benchmarks", many=True, read_only=True)
    updated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = FreightDestination
        fields = [
            "id",
            "state",
            "district",
            "name",
            "pin_code",
            "distance_km",
            "remarks",
            "is_active",
            "updated_at",
            "updated_by_name",
            "rates",
        ]

    def get_updated_by_name(self, obj) -> str:
        user = obj.updated_by
        if user is None:
            return ""
        return user.full_name or user.email


class FreightRateInputSerializer(serializers.Serializer):
    slab = serializers.PrimaryKeyRelatedField(queryset=FreightSlab.objects.all())
    basis = serializers.ChoiceField(
        choices=FreightRateBasis.choices, default=FreightRateBasis.PER_TRIP
    )
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01")
    )


class FreightDestinationWriteSerializer(serializers.Serializer):
    state = serializers.CharField(max_length=60)
    district = serializers.CharField(max_length=80, allow_blank=True, default="")
    name = serializers.CharField(max_length=160)
    pin_code = serializers.RegexField(
        r"^\d{6}$",
        allow_blank=True,
        default="",
        error_messages={"invalid": "A PIN code is six digits."},
    )
    distance_km = serializers.IntegerField(min_value=0, allow_null=True, default=None)
    remarks = serializers.CharField(max_length=255, allow_blank=True, default="")
    is_active = serializers.BooleanField(default=True)
    rates = FreightRateInputSerializer(many=True, default=list)
