"""Serializers for Daily Electricity++: meters in the tree, their setups, readings.

The Daily Electricity page keeps its own serializers (``maintenance.serializers``)
and behaves exactly as it always has. These serve the same meters and readings
to Daily Electricity++, which adds the tree and holds readings to a chain.

Two rules shape the reading serializer here, and the split depends on both:

* **An opening follows on from the previous closing.** It is filled in when
  omitted and refused when it disagrees, unless the entry says the dial was
  replaced or reset. The register used to accept any opening, and fifteen
  readings on live started from a number nobody had closed on — each one a
  slice of electricity that belongs to no day.
* **The chain is kept whole when a reading changes.** Correcting a closing
  moves the next reading's opening with it, filling in a skipped day takes that
  day out of the reading that had covered it, and deleting a reading hands its
  days to the next one. See :func:`relink_next`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from blowing.models import BlowingMachine
from company.models import Company
from production_execution.models import ProductionLine

from .electricity import setup as setup_service
from .electricity.service import describe_setup
from .models import (
    DailyElectricityReading,
    ElectricityAllocationBasis,
    ElectricityConsumer,
    ElectricityMeter,
    ElectricityMeterSetup,
)
from .serializers import _meter_rate_per_unit as meter_rate_per_unit


# ---------------------------------------------------------------------------
# Setups
# ---------------------------------------------------------------------------


class ElectricityMeterShareSerializer(serializers.Serializer):
    company = serializers.SlugRelatedField(
        slug_field="code",
        queryset=Company.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    consumer = serializers.SlugRelatedField(
        slug_field="code",
        queryset=ElectricityConsumer.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    percent = serializers.DecimalField(
        max_digits=7,
        decimal_places=3,
        min_value=Decimal("0.001"),
        max_value=Decimal("100"),
    )

    def validate(self, attrs):
        if bool(attrs.get("company")) == bool(attrs.get("consumer")):
            raise serializers.ValidationError("Name a company or a consumer, not both.")
        return attrs


class ElectricityMeterDriverSerializer(serializers.Serializer):
    production_line = serializers.PrimaryKeyRelatedField(
        queryset=ProductionLine.objects.all(), required=False, allow_null=True
    )
    blowing_machine = serializers.PrimaryKeyRelatedField(
        queryset=BlowingMachine.objects.all(), required=False, allow_null=True
    )
    meter = serializers.PrimaryKeyRelatedField(
        queryset=ElectricityMeter.objects.all(), required=False, allow_null=True
    )
    company = serializers.SlugRelatedField(
        slug_field="code",
        queryset=Company.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    weight = serializers.DecimalField(
        max_digits=10,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        default=Decimal("1"),
    )

    def validate(self, attrs):
        named = [
            key
            for key in ("production_line", "blowing_machine", "meter")
            if attrs.get(key) is not None
        ]
        if len(named) != 1:
            raise serializers.ValidationError(
                "Name exactly one of a production line, a blowing machine or a meter."
            )
        return attrs


class ElectricityMeterSetupSerializer(serializers.ModelSerializer):
    """One dated version of a meter's place in the tree and its split."""

    shares = ElectricityMeterShareSerializer(many=True, required=False)
    drivers = ElectricityMeterDriverSerializer(many=True, required=False)
    parent = serializers.PrimaryKeyRelatedField(
        queryset=ElectricityMeter.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = ElectricityMeterSetup
        fields = [
            "id",
            "meter",
            "effective_from",
            "in_service",
            "parent",
            "basis",
            "note",
            "shares",
            "drivers",
        ]
        # One version per meter per day is checked by save_setup, which can
        # say which version already starts that day; DRF's own check cannot.
        validators = []

    def get_fields(self):
        fields = super().get_fields()
        if self.instance is not None and not isinstance(self.instance, list):
            # A version belongs to its meter; moving it to another is a
            # different version, not a correction of this one.
            fields["meter"].read_only = True
        return fields

    def validate(self, attrs):
        if self.instance is None and not attrs.get("effective_from"):
            raise serializers.ValidationError({"effective_from": "Say from which day this applies."})
        return attrs

    def create(self, validated_data):
        meter = validated_data.pop("meter")
        return setup_service.save_setup(
            meter, validated_data, user=self.context["request"].user
        )

    def update(self, instance, validated_data):
        return setup_service.save_setup(
            instance.meter,
            validated_data,
            user=self.context["request"].user,
            instance=instance,
        )

    def to_representation(self, instance):
        data = describe_setup(instance)
        data.update(
            {
                "meter": instance.meter_id,
                "meter_name": instance.meter.name,
                "created_by_name": getattr(instance.created_by, "full_name", "") or "",
                "updated_by_name": getattr(instance.updated_by, "full_name", "") or "",
                "created_at": instance.created_at,
                "updated_at": instance.updated_at,
            }
        )
        data["effective_from"] = instance.effective_from.isoformat()
        return data


# ---------------------------------------------------------------------------
# Meters
# ---------------------------------------------------------------------------


class ElectricityMeterPlacementSerializer(serializers.Serializer):
    """Where a new meter goes, sent with it on create."""

    effective_from = serializers.DateField(required=False)
    parent = serializers.PrimaryKeyRelatedField(
        queryset=ElectricityMeter.objects.all(), required=False, allow_null=True
    )
    basis = serializers.ChoiceField(
        choices=ElectricityAllocationBasis.choices, required=False
    )
    shares = ElectricityMeterShareSerializer(many=True, required=False)
    drivers = ElectricityMeterDriverSerializer(many=True, required=False)
    note = serializers.CharField(required=False, allow_blank=True)


class TreeMeterSerializer(serializers.ModelSerializer):
    """A meter, and — read-only — where it sits in the tree today.

    ``is_main``, ``counts_as_supply``, ``company_codes`` and ``consumer_codes``
    are the Daily Electricity page's, read-only here: that page sets them, the
    tree never does. A meter is placed with ``placement`` when it is created
    here, and moved or re-split afterwards through its setup versions.
    """

    # Annotated by the viewset — used by the UI to prefill the next opening.
    last_reading_date = serializers.DateField(read_only=True)
    last_closing_reading = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    readings_count = serializers.IntegerField(read_only=True)
    # Read-only, resolved from the central Cost Master (see meter_rate_per_unit).
    rate_per_unit = serializers.SerializerMethodField()
    company_codes = serializers.SlugRelatedField(
        source="companies", slug_field="code", many=True, read_only=True
    )
    consumer_codes = serializers.SlugRelatedField(
        source="consumers", slug_field="code", many=True, read_only=True
    )
    companies_display = serializers.SerializerMethodField()
    supply_source_display = serializers.CharField(
        source="get_supply_source_display", read_only=True
    )
    register_of_name = serializers.CharField(
        source="register_of.name", read_only=True, default=None
    )
    register_of = serializers.PrimaryKeyRelatedField(
        queryset=ElectricityMeter.objects.all(), required=False, allow_null=True
    )
    placement = ElectricityMeterPlacementSerializer(write_only=True, required=False)
    tree = serializers.SerializerMethodField()

    class Meta:
        model = ElectricityMeter
        fields = [
            "id",
            "name",
            "meter_number",
            "location",
            "multiplying_factor",
            "register_of",
            "register_of_name",
            "supply_source",
            "supply_source_display",
            "is_main",
            "counts_as_supply",
            "company_codes",
            "consumer_codes",
            "companies_display",
            "rate_per_unit",
            "last_reading_date",
            "last_closing_reading",
            "readings_count",
            "tree",
            "placement",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["is_main", "counts_as_supply", "created_at", "updated_at"]

    def get_companies_display(self, obj) -> str:
        names = [company.name for company in obj.companies.all()]
        names += [consumer.name for consumer in obj.consumers.all()]
        return ", ".join(names)

    def get_rate_per_unit(self, obj) -> str:
        return str(meter_rate_per_unit(obj))

    def get_tree(self, obj):
        """Where the meter sits on the day the list was asked for (today)."""
        from .electricity.service import meter_tree

        tree = self.context.get("tree")
        if tree is None:
            tree = meter_tree(self.context.get("tree_date") or timezone.localdate())
            self.context["tree"] = tree
        info = tree.get(obj.id)
        if info is None:
            return {"in_service": False, "order": None, "depth": 0, "parent": None,
                    "parent_name": None, "unplaced": False, "setup": None}
        setup_row = info.get("setup")
        return {
            "in_service": info["in_service"],
            "order": info["order"],
            "depth": info["depth"],
            "parent": info["parent_id"],
            "parent_name": info.get("parent_name"),
            "unplaced": info.get("unplaced", False),
            "setup": describe_setup(setup_row) if setup_row is not None else None,
        }

    def validate_register_of(self, value):
        if value is None:
            return value
        if self.instance is not None and value.pk == self.instance.pk:
            raise serializers.ValidationError("A meter cannot be its own second register.")
        if value.register_of_id:
            raise serializers.ValidationError(
                f"{value.name} is itself a second register of {value.register_of.name}."
            )
        if self.instance is not None and self.instance.setups.exists():
            raise serializers.ValidationError(
                "This meter has its own place in the tree. Take it out of the tree "
                "before making it another meter's second register."
            )
        return value

    def validate(self, attrs):
        placement = attrs.get("placement")
        if placement and attrs.get("register_of"):
            raise serializers.ValidationError(
                {"placement": "A second register is not placed in the tree."}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        placement = validated_data.pop("placement", None) or {}
        # Known before the first save, so a main keeps the supply it was given
        # (the model clears the source on anything that is not a main).
        # A meter added here shows on the Daily Electricity page too, so it gets
        # that page's flags right at birth: a main meter is a main, and a
        # second register is never a supply of its own. After this the tree
        # and the page each keep their own account.
        validated_data["is_main"] = (
            validated_data.get("register_of") is not None or placement.get("parent") is None
        )
        if validated_data.get("register_of") is not None:
            validated_data["counts_as_supply"] = False
        meter = super().create(validated_data)
        if meter.register_of_id is None:
            placement.setdefault("effective_from", timezone.localdate())
            placement.setdefault("parent", None)
            placement.setdefault("basis", ElectricityAllocationBasis.UNASSIGNED)
            setup_service.save_setup(meter, placement, user=self.context["request"].user)
        return meter

    def update(self, instance, validated_data):
        validated_data.pop("placement", None)
        return super().update(instance, validated_data)


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


def previous_reading(meter, day, *, exclude_pk=None) -> Optional[DailyElectricityReading]:
    rows = DailyElectricityReading.objects.filter(meter=meter, date__lt=day, is_active=True)
    if exclude_pk:
        rows = rows.exclude(pk=exclude_pk)
    return rows.order_by("-date").first()


def next_reading(meter, day, *, exclude_pk=None) -> Optional[DailyElectricityReading]:
    rows = DailyElectricityReading.objects.filter(meter=meter, date__gt=day, is_active=True)
    if exclude_pk:
        rows = rows.exclude(pk=exclude_pk)
    return rows.order_by("date").first()


def relink_next(reading_next: Optional[DailyElectricityReading], was: Decimal, now: Decimal, user=None):
    """Move the next reading's opening from ``was`` to ``now`` — only when it
    followed on from ``was``. A next reading that already started somewhere
    else (a reset, a break) is left exactly as it is."""
    if reading_next is None or reading_next.meter_reset:
        return
    if reading_next.opening_reading != was or was == now:
        return
    reading_next.opening_reading = now
    reading_next.updated_by = user
    reading_next.save()


def meter_in_service(meter, day) -> Optional[str]:
    """Why ``meter`` cannot take a reading on ``day``, or None."""
    principal = meter.register_of if meter.register_of_id else meter
    versions = list(principal.setups.filter(is_active=True).order_by("effective_from"))
    if not versions:
        # Not placed yet: readings are still taken, and the split names it.
        return None
    current = None
    for version in versions:
        if version.effective_from > day:
            break
        current = version
    if current is not None and current.in_service:
        return None
    if current is None:
        return (
            f"{principal.name} is in the meter tree only from "
            f"{versions[0].effective_from:%d %b %Y}."
        )
    return f"{principal.name} is out of service from {current.effective_from:%d %b %Y}."


class TreeReadingSerializer(serializers.ModelSerializer):
    meter_name = serializers.CharField(source="meter.name", read_only=True)
    meter_is_main = serializers.BooleanField(source="meter.is_main", read_only=True)
    meter_supply_source = serializers.CharField(source="meter.supply_source", read_only=True)
    meter_supply_source_display = serializers.CharField(
        source="meter.get_supply_source_display", read_only=True
    )
    meter_counts_as_supply = serializers.BooleanField(
        source="meter.counts_as_supply", read_only=True
    )
    meter_register_of = serializers.IntegerField(source="meter.register_of_id", read_only=True)
    meter_companies_display = serializers.SerializerMethodField()
    # The Daily Electricity page's attribution, shown but never written here:
    # on this page who pays is the tree's job.
    company_codes = serializers.SlugRelatedField(
        source="companies", slug_field="code", many=True, read_only=True
    )
    consumer_codes = serializers.SlugRelatedField(
        source="consumers", slug_field="code", many=True, read_only=True
    )
    attribution_display = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=""
    )
    # Optional: when omitted, carried from the meter's previous closing.
    opening_reading = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    rate_per_unit = serializers.DecimalField(max_digits=12, decimal_places=4, required=False)
    multiplying_factor = serializers.DecimalField(
        max_digits=10, decimal_places=4, required=False, min_value=Decimal("0.0001")
    )
    dial_difference = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = DailyElectricityReading
        fields = [
            "id",
            "meter",
            "meter_name",
            "meter_is_main",
            "meter_supply_source",
            "meter_supply_source_display",
            "meter_counts_as_supply",
            "meter_register_of",
            "meter_companies_display",
            "company_codes",
            "consumer_codes",
            "attribution_display",
            "date",
            "reading_time",
            "opening_reading",
            "closing_reading",
            "meter_reset",
            "dial_difference",
            "multiplying_factor",
            "units_consumed",
            "rate_per_unit",
            "total_cost",
            "remarks",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "dial_difference",
            "units_consumed",
            "total_cost",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def get_meter_companies_display(self, obj) -> str:
        names = [company.name for company in obj.meter.companies.all()]
        names += [consumer.name for consumer in obj.meter.consumers.all()]
        return ", ".join(names)

    def get_attribution_display(self, obj) -> str:
        return ", ".join(obj.attribution_names())

    def validate(self, attrs):
        instance = self.instance
        meter = attrs.get("meter") or (instance.meter if instance else None)
        day = attrs.get("date") or (instance.date if instance else None)
        if instance is not None and (
            ("meter" in attrs and attrs["meter"] != instance.meter)
            or ("date" in attrs and attrs["date"] != instance.date)
        ):
            raise serializers.ValidationError(
                {
                    "date": (
                        "A reading cannot be moved to another meter or day. Delete it "
                        "and enter it again where it belongs, so the days either side "
                        "stay joined up."
                    )
                }
            )
        if meter is None or day is None:
            return attrs

        clash = DailyElectricityReading.objects.filter(meter=meter, date=day)
        if instance:
            clash = clash.exclude(pk=instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"date": "A reading for this meter and date already exists."}
            )

        if instance is None:
            refused = meter_in_service(meter, day)
            if refused:
                raise serializers.ValidationError({"date": refused})

        exclude = instance.pk if instance else None
        previous = previous_reading(meter, day, exclude_pk=exclude)
        following = next_reading(meter, day, exclude_pk=exclude)
        meter_reset = attrs.get("meter_reset", instance.meter_reset if instance else False)

        opening = attrs.get("opening_reading", instance.opening_reading if instance else None)
        # Only an opening being set is held to the chain. History entered
        # before the rule may be broken, and must stay correctable — a remark,
        # a closing — without first being made whole.
        opening_changed = instance is None or (
            "opening_reading" in attrs and attrs["opening_reading"] != instance.opening_reading
        ) or (instance.meter_reset and not meter_reset)
        if opening is None:
            if previous is None:
                raise serializers.ValidationError(
                    {
                        "opening_reading": (
                            "Enter the opening reading — this meter has no earlier "
                            "reading to carry forward."
                        )
                    }
                )
            opening = previous.closing_reading
            attrs["opening_reading"] = opening
        elif (
            opening_changed
            and previous is not None
            and not meter_reset
            and opening != previous.closing_reading
        ):
            raise serializers.ValidationError(
                {
                    "opening_reading": (
                        f"The opening must be the previous closing, "
                        f"{previous.closing_reading} on {previous.date:%d %b %Y}. If the "
                        "meter was replaced or its dial reset, tick 'Meter reset'. If "
                        "that closing is wrong, correct that reading instead."
                    )
                }
            )

        closing = attrs.get("closing_reading", instance.closing_reading if instance else None)
        if opening is not None and closing is not None and closing < opening:
            raise serializers.ValidationError(
                {"closing_reading": "Closing reading cannot be less than opening reading."}
            )

        # The next reading carries on from this one, so this closing cannot
        # overtake it.
        carries_on_from = instance.closing_reading if instance else opening
        if (
            following is not None
            and not following.meter_reset
            and following.opening_reading == carries_on_from
            and closing is not None
            and closing > following.closing_reading
        ):
            raise serializers.ValidationError(
                {
                    "closing_reading": (
                        f"This is above the next reading's closing, "
                        f"{following.closing_reading} on {following.date:%d %b %Y}."
                    )
                }
            )

        if instance is None:
            # Snapshot the master's rate and MF unless the entry overrides them.
            if "rate_per_unit" not in attrs:
                attrs["rate_per_unit"] = meter_rate_per_unit(meter, as_of=day)
            if "multiplying_factor" not in attrs:
                attrs["multiplying_factor"] = meter.multiplying_factor
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        if not validated_data.get("reading_time"):
            validated_data["reading_time"] = timezone.localtime().time()
        reading = super().create(validated_data)
        # A reading filling in a skipped day: the next reading had covered this
        # day from the previous closing, and now carries on from this one.
        following = next_reading(reading.meter, reading.date, exclude_pk=reading.pk)
        relink_next(
            following,
            reading.opening_reading,
            reading.closing_reading,
            user=validated_data.get("created_by"),
        )
        return reading

    @transaction.atomic
    def update(self, instance, validated_data):
        was = instance.closing_reading
        following = next_reading(instance.meter, instance.date, exclude_pk=instance.pk)
        reading = super().update(instance, validated_data)
        relink_next(following, was, reading.closing_reading, user=validated_data.get("updated_by"))
        return reading


# ---------------------------------------------------------------------------
# The day sheet
# ---------------------------------------------------------------------------


class DaySheetEntrySerializer(serializers.Serializer):
    meter = serializers.PrimaryKeyRelatedField(queryset=ElectricityMeter.objects.all())
    closing_reading = serializers.DecimalField(max_digits=14, decimal_places=2)
    opening_reading = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False
    )
    meter_reset = serializers.BooleanField(required=False, default=False)
    reading_time = serializers.TimeField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class DaySheetSaveSerializer(serializers.Serializer):
    date = serializers.DateField()
    entries = DaySheetEntrySerializer(many=True)

    def validate_entries(self, entries):
        seen = set()
        for entry in entries:
            if entry["meter"].pk in seen:
                raise serializers.ValidationError(
                    f"{entry['meter'].name} is on the sheet twice."
                )
            seen.add(entry["meter"].pk)
        return entries


class RunSourceSerializer(serializers.Serializer):
    """A production line or blowing machine a run-hours split can follow."""

    kind = serializers.CharField()
    id = serializers.IntegerField()
    name = serializers.CharField()
    company = serializers.CharField()
    company_name = serializers.CharField()
    is_active = serializers.BooleanField()
