"""Shapes the fleet register sends to the page, and the rules it enforces.

Most of the thinking here is in the two write serializers. The fuel form is
the one screen in this module that gets used every day, so it is written to
accept what a pump slip actually shows and to work the rest out itself.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from .constants import (
    ApprovalStatus,
    DOCUMENT_WARNING_DAYS,
    FUEL_UNITS,
    FUELS_ALLOWED_FOR,
    FuelType,
    SERVICE_DUE_WARNING_KM,
)
from .models import DailyReading, FleetVehicle, FuelEntry, ServiceEntry, VehicleDocument

TWO_PLACES = Decimal("0.01")


#: Where :class:`company_vehicle.views.FleetAttachmentAPI` is mounted. The
#: serializers hand out this path rather than ``file.url`` so a stored bill is
#: only ever fetched through a permission check.
ATTACHMENT_URL = "/api/v1/company-vehicles/attachments/{kind}/{pk}/"


def _attachment(kind: str, instance, field: str):
    """The API path for one stored file, or None when nothing is filed."""
    return ATTACHMENT_URL.format(kind=kind, pk=instance.pk) if getattr(instance, field) else None


def _user_name(user):
    if not user:
        return None
    return getattr(user, "full_name", None) or user.get_username()


class ApprovalActionSerializer(serializers.Serializer):
    """The body of an approve or reject on a workshop bill.

    A rejection must say why: an entry sent back without a reason is one the
    clerk cannot act on.
    """

    approval_status = serializers.ChoiceField(
        choices=[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]
    )
    rejection_reason = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        if attrs["approval_status"] == ApprovalStatus.REJECTED and not attrs.get(
            "rejection_reason", ""
        ).strip():
            raise serializers.ValidationError(
                {"rejection_reason": "Say why it is being sent back."}
            )
        return attrs


class ApprovalFieldsMixin(serializers.Serializer):
    """The read-only approval fields a serializer shows for an approvable entry.

    Used by :class:`ServiceEntrySerializer` alone -- fuel has no approval.
    """

    approval_status_label = serializers.CharField(
        source="get_approval_status_display", read_only=True
    )
    approved_by_name = serializers.SerializerMethodField()

    def get_approved_by_name(self, obj):
        return _user_name(obj.approved_by)


# ---------------------------------------------------------------- documents


class VehicleDocumentSerializer(serializers.ModelSerializer):
    doc_type_label = serializers.CharField(source="get_doc_type_display", read_only=True)
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    days_to_expiry = serializers.SerializerMethodField()
    expiry_state = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = VehicleDocument
        fields = [
            "id",
            "vehicle",
            "vehicle_number",
            "doc_type",
            "doc_type_label",
            "document_number",
            "issuing_authority",
            "issue_date",
            "expiry_date",
            "amount",
            "remarks",
            "file",
            "file_url",
            "days_to_expiry",
            "expiry_state",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {"file": {"write_only": True, "required": False}}

    def get_days_to_expiry(self, obj):
        return (obj.expiry_date - timezone.localdate()).days

    def get_expiry_state(self, obj):
        """``EXPIRED`` / ``EXPIRING`` / ``OK`` — what colour the row gets."""
        days = self.get_days_to_expiry(obj)
        if days < 0:
            return "EXPIRED"
        if days <= DOCUMENT_WARNING_DAYS:
            return "EXPIRING"
        return "OK"

    def get_file_url(self, obj):
        return _attachment("document", obj, "file")


# ----------------------------------------------------------------- vehicles


class FleetVehicleSerializer(serializers.ModelSerializer):
    """A vehicle as the list and the detail header show it."""

    category_label = serializers.CharField(source="get_category_display", read_only=True)
    fuel_type_label = serializers.CharField(source="get_fuel_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    fuel_unit = serializers.CharField(read_only=True)
    last_odometer = serializers.IntegerField(read_only=True)
    fuels_allowed = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()
    document_alerts = serializers.SerializerMethodField()
    next_service = serializers.SerializerMethodField()
    last_mileage = serializers.SerializerMethodField()

    class Meta:
        model = FleetVehicle
        fields = [
            "id",
            "vehicle_number",
            "nickname",
            "display_name",
            "category",
            "category_label",
            "fuel_type",
            "fuel_type_label",
            "fuel_unit",
            "fuels_allowed",
            "make_model",
            "status",
            "status_label",
            "purchase_date",
            "purchase_value",
            "assigned_to",
            "department",
            "opening_odometer",
            "last_odometer",
            "remarks",
            "photo",
            "photo_url",
            "document_alerts",
            "next_service",
            "last_mileage",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {"photo": {"write_only": True, "required": False}}

    def get_display_name(self, obj):
        return str(obj)

    def get_photo_url(self, obj):
        return _attachment("vehicle", obj, "photo")

    def get_fuels_allowed(self, obj):
        """Which fuels this vehicle's fill form may offer.

        Sent per vehicle so the form does not have to know the dual-fuel rule.
        """
        return [
            {"value": fuel, "label": FuelType(fuel).label, "unit": FUEL_UNITS.get(fuel, "L")}
            for fuel in FUELS_ALLOWED_FOR.get(obj.fuel_type, [obj.fuel_type])
        ]

    def get_document_alerts(self, obj):
        """How many of this vehicle's papers have run out, or are about to."""
        today = timezone.localdate()
        soon = today + timedelta(days=DOCUMENT_WARNING_DAYS)
        documents = obj.documents.filter(is_active=True)
        return {
            "expired": documents.filter(expiry_date__lt=today).count(),
            "expiring": documents.filter(expiry_date__gte=today, expiry_date__lte=soon).count(),
        }

    def get_next_service(self, obj):
        """The due date or reading from the most recent service, with a flag.

        ``due`` is true once the date has passed or the meter is within
        :data:`SERVICE_DUE_WARNING_KM` of the reading the garage asked for.
        """
        last = (
            obj.service_entries.exclude(approval_status=ApprovalStatus.REJECTED)
            .exclude(next_service_date__isnull=True, next_service_odometer__isnull=True)
            .order_by("-entry_date", "-id")
            .first()
        )
        if not last:
            return None
        today = timezone.localdate()
        odometer = obj.last_odometer
        due = False
        if last.next_service_date and last.next_service_date <= today:
            due = True
        if (
            last.next_service_odometer
            and odometer is not None
            and odometer >= last.next_service_odometer - SERVICE_DUE_WARNING_KM
        ):
            due = True
        return {
            "date": last.next_service_date,
            "odometer": last.next_service_odometer,
            "due": due,
        }

    def get_last_mileage(self, obj):
        """The most recent measured mileage, so the list can show a trend."""
        entry = (
            obj.fuel_entries.exclude(mileage__isnull=True)
            .order_by("-entry_date", "-odometer", "-id")
            .first()
        )
        if not entry:
            return None
        return {
            "value": entry.mileage,
            "unit": f"km/{entry.unit}",
            "fuel_type": entry.fuel_type,
            "on": entry.entry_date,
        }


class FleetVehicleWriteSerializer(serializers.ModelSerializer):
    """Add or edit a vehicle. Only four fields are demanded."""

    class Meta:
        model = FleetVehicle
        fields = [
            "vehicle_number",
            "nickname",
            "category",
            "fuel_type",
            "make_model",
            "status",
            "purchase_date",
            "purchase_value",
            "assigned_to",
            "department",
            "opening_odometer",
            "photo",
            "remarks",
        ]

    def validate_vehicle_number(self, value):
        cleaned = (value or "").upper().replace(" ", "")
        if not cleaned:
            raise serializers.ValidationError("Vehicle number is required.")
        existing = FleetVehicle.objects.filter(vehicle_number=cleaned)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("This vehicle is already on the register.")
        return cleaned

    def validate(self, attrs):
        """Changing the fuel of a vehicle that already has fillings is refused.

        The old entries were measured in the other unit, and silently
        reinterpreting litres as kilograms would corrupt every past mileage.
        """
        if self.instance and "fuel_type" in attrs and attrs["fuel_type"] != self.instance.fuel_type:
            filled = self.instance.fuel_entries.exclude(
                fuel_type__in=FUELS_ALLOWED_FOR.get(attrs["fuel_type"], [])
            ).exists()
            if filled:
                raise serializers.ValidationError(
                    {
                        "fuel_type": "This vehicle already has fillings of the old fuel. "
                        "Changing it would make their mileage wrong."
                    }
                )
        return attrs


# ----------------------------------------------------------- daily readings


class DailyReadingSerializer(serializers.ModelSerializer):
    """One day's meter reading."""

    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    vehicle_nickname = serializers.CharField(source="vehicle.nickname", read_only=True)
    entered_by_name = serializers.SerializerMethodField()

    class Meta:
        model = DailyReading
        fields = [
            "id",
            "vehicle",
            "vehicle_number",
            "vehicle_nickname",
            "reading_date",
            "odometer",
            "remarks",
            "entered_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_entered_by_name(self, obj):
        return _user_name(obj.created_by)

    def validate_reading_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("A reading cannot be dated in the future.")
        return value

    def validate(self, attrs):
        """A reading below the one before it needs saying out loud.

        Same rule as the fuel form, and for the same reason: meters get
        replaced and do break, so this is a question rather than a wall --
        answer it in the remarks and it saves.
        """
        vehicle = attrs.get("vehicle") or getattr(self.instance, "vehicle", None)
        on = attrs.get("reading_date") or getattr(self.instance, "reading_date", None)
        odometer = attrs.get("odometer", getattr(self.instance, "odometer", None))
        if not (vehicle and on and odometer is not None):
            return attrs

        previous = (
            DailyReading.objects.filter(vehicle=vehicle, reading_date__lt=on)
            .order_by("-reading_date")
            .first()
        )
        last = previous.odometer if previous else None
        if last is not None and odometer < last and not (attrs.get("remarks") or "").strip():
            raise serializers.ValidationError(
                {
                    "odometer": f"The reading on {previous.reading_date} was {last} km. If the "
                    "meter was replaced or is broken, say so in the remarks and save again."
                }
            )
        return attrs


# -------------------------------------------------------------- fuel entries


class FuelEntrySerializer(serializers.ModelSerializer):
    """One filling. No approval fields -- a filling counts when it is recorded."""

    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    vehicle_nickname = serializers.CharField(source="vehicle.nickname", read_only=True)
    fuel_type_label = serializers.CharField(source="get_fuel_type_display", read_only=True)
    payment_mode_label = serializers.CharField(source="get_payment_mode_display", read_only=True)
    unit = serializers.CharField(read_only=True)
    mileage_unit = serializers.SerializerMethodField()
    bill_photo_url = serializers.SerializerMethodField()
    entered_by_name = serializers.SerializerMethodField()

    class Meta:
        model = FuelEntry
        fields = [
            "id",
            "vehicle",
            "vehicle_number",
            "vehicle_nickname",
            "entry_date",
            "fuel_type",
            "fuel_type_label",
            "unit",
            "odometer",
            "quantity",
            "rate",
            "amount",
            "is_tank_full",
            "station_name",
            "bill_number",
            "bill_photo",
            "bill_photo_url",
            "payment_mode",
            "payment_mode_label",
            "filled_by",
            "remarks",
            "odometer_note",
            "distance_km",
            "mileage",
            "mileage_unit",
            "entered_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "distance_km", "mileage", "created_at"]
        extra_kwargs = {"bill_photo": {"write_only": True, "required": False}}

    def get_mileage_unit(self, obj):
        return f"km/{obj.unit}"

    def get_bill_photo_url(self, obj):
        return _attachment("fuel", obj, "bill_photo")

    def get_entered_by_name(self, obj):
        return _user_name(obj.created_by)


class FuelEntryWriteSerializer(serializers.ModelSerializer):
    """Record one filling.

    Three conveniences, all of them because of what a pump slip looks like:

    * **Any two of quantity, rate and amount.** The slip prints the amount and
      the litres; the rate is arithmetic, so it is not asked for.
    * **A meter that went backwards is a warning, not a wall.** Odometers get
      replaced and do break. The entry is accepted once ``odometer_note``
      says what happened.
    * **A repeat of the same bill is queried once.** Same vehicle, same day,
      same amount is usually a double entry, occasionally two real fills.
      ``confirm_duplicate`` is how the page says it meant it.
    """

    confirm_duplicate = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = FuelEntry
        fields = [
            "vehicle",
            "entry_date",
            "fuel_type",
            "odometer",
            "quantity",
            "rate",
            "amount",
            "is_tank_full",
            "station_name",
            "bill_number",
            "bill_photo",
            "payment_mode",
            "filled_by",
            "remarks",
            "odometer_note",
            "confirm_duplicate",
        ]
        extra_kwargs = {
            # Null as well as absent: a form that clears a box sends null, and
            # either way the missing one of the three is worked out below.
            "amount": {"required": False, "allow_null": True},
            "rate": {"required": False, "allow_null": True},
            "fuel_type": {"required": False, "allow_null": True},
            "bill_photo": {"required": False, "allow_null": True},
        }

    def _field(self, attrs, name):
        if name in attrs:
            return attrs[name]
        return getattr(self.instance, name, None) if self.instance else None

    def validate_entry_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("A filling cannot be dated in the future.")
        return value

    def validate(self, attrs):
        vehicle = self._field(attrs, "vehicle")

        # --- which fuel ----------------------------------------------------
        allowed = FUELS_ALLOWED_FOR.get(vehicle.fuel_type, [vehicle.fuel_type])
        fuel = self._field(attrs, "fuel_type")
        if not fuel:
            # A single-fuel vehicle never has to be asked.
            if len(allowed) == 1:
                fuel = allowed[0]
                attrs["fuel_type"] = fuel
            else:
                raise serializers.ValidationError(
                    {"fuel_type": "Say whether this was petrol or CNG."}
                )
        if fuel not in allowed:
            raise serializers.ValidationError(
                {"fuel_type": f"{vehicle.vehicle_number} does not run on {FuelType(fuel).label}."}
            )

        # --- quantity, rate, amount ---------------------------------------
        quantity = self._field(attrs, "quantity")
        rate = self._field(attrs, "rate")
        amount = self._field(attrs, "amount")
        if quantity is None or quantity <= 0:
            raise serializers.ValidationError({"quantity": "How much was filled?"})
        if amount is None and rate is None:
            raise serializers.ValidationError(
                {"amount": "Enter the amount paid, or the rate per unit."}
            )
        if amount is None:
            attrs["amount"] = (quantity * rate).quantize(TWO_PLACES)
        elif rate is None:
            attrs["rate"] = (amount / quantity).quantize(TWO_PLACES)
        if (attrs.get("amount") or amount) <= 0:
            raise serializers.ValidationError({"amount": "Amount must be more than zero."})

        # --- the meter -----------------------------------------------------
        odometer = self._field(attrs, "odometer")
        previous = (
            FuelEntry.objects.filter(vehicle=vehicle, entry_date__lte=self._field(attrs, "entry_date"))
            .exclude(pk=self.instance.pk if self.instance else None)
            .order_by("-entry_date", "-odometer", "-id")
            .first()
        )
        last_reading = previous.odometer if previous else vehicle.opening_odometer
        if last_reading is not None and odometer is not None and odometer < last_reading:
            if not (self._field(attrs, "odometer_note") or "").strip():
                raise serializers.ValidationError(
                    {
                        "odometer": f"Last reading was {last_reading} km. If the meter was "
                        "replaced or is broken, write that in the note and save again."
                    }
                )

        # --- the same bill twice -------------------------------------------
        if not attrs.pop("confirm_duplicate", False):
            twin = FuelEntry.objects.filter(
                vehicle=vehicle,
                entry_date=self._field(attrs, "entry_date"),
                amount=attrs.get("amount") or amount,
            ).exclude(pk=self.instance.pk if self.instance else None)
            if twin.exists():
                raise serializers.ValidationError(
                    {
                        "confirm_duplicate": "The same amount is already entered for this "
                        "vehicle on this date. Save again to confirm it is a second filling."
                    }
                )
        return attrs


# ----------------------------------------------------------- service entries


class ServiceEntrySerializer(ApprovalFieldsMixin, serializers.ModelSerializer):
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    vehicle_nickname = serializers.CharField(source="vehicle.nickname", read_only=True)
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    payment_mode_label = serializers.CharField(source="get_payment_mode_display", read_only=True)
    bill_photo_url = serializers.SerializerMethodField()
    entered_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ServiceEntry
        fields = [
            "id",
            "vehicle",
            "vehicle_number",
            "vehicle_nickname",
            "entry_date",
            "odometer",
            "kind",
            "kind_label",
            "workshop_name",
            "description",
            "parts_amount",
            "labour_amount",
            "total_amount",
            "bill_number",
            "bill_photo",
            "bill_photo_url",
            "payment_mode",
            "payment_mode_label",
            "next_service_date",
            "next_service_odometer",
            "down_days",
            "remarks",
            "approval_status",
            "approval_status_label",
            "approved_by_name",
            "approved_at",
            "rejection_reason",
            "entered_by_name",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "approval_status",
            "approved_at",
            "rejection_reason",
            "created_at",
        ]
        extra_kwargs = {"bill_photo": {"write_only": True, "required": False}}

    def get_bill_photo_url(self, obj):
        return _attachment("service", obj, "bill_photo")

    def get_entered_by_name(self, obj):
        return _user_name(obj.created_by)


class ServiceEntryWriteSerializer(serializers.ModelSerializer):
    """Record one service or repair.

    ``total_amount`` may be left out when parts and labour are given
    separately, and given alone when the garage handed over one figure.
    """

    class Meta:
        model = ServiceEntry
        fields = [
            "vehicle",
            "entry_date",
            "odometer",
            "kind",
            "workshop_name",
            "description",
            "parts_amount",
            "labour_amount",
            "total_amount",
            "bill_number",
            "bill_photo",
            "payment_mode",
            "next_service_date",
            "next_service_odometer",
            "down_days",
            "remarks",
        ]
        extra_kwargs = {
            "total_amount": {"required": False, "allow_null": True},
            "bill_photo": {"required": False, "allow_null": True},
        }

    def _field(self, attrs, name):
        if name in attrs:
            return attrs[name]
        return getattr(self.instance, name, None) if self.instance else None

    def validate_entry_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("A service cannot be dated in the future.")
        return value

    def validate(self, attrs):
        parts = self._field(attrs, "parts_amount") or Decimal("0")
        labour = self._field(attrs, "labour_amount") or Decimal("0")
        total = self._field(attrs, "total_amount")
        if total is None:
            total = parts + labour
            attrs["total_amount"] = total
        if total <= 0:
            raise serializers.ValidationError({"total_amount": "What did the bill come to?"})
        return attrs
