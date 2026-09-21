"""Serializers for the electricity-meter manager assignments."""

from rest_framework import serializers

from .models_manager import UserElectricityMeter


class UserElectricityMeterSerializer(serializers.ModelSerializer):
    """One assignment, with enough about the user and meter to render a row.

    Flattened rather than nested: the table shows a name, an email and a meter,
    and a nested serializer here would mean the frontend reaching into
    `row.user.full_name` for a screen that never needs the rest. Note
    `accounts.User` has no `get_full_name()` — it is a plain `full_name` field
    on an `AbstractBaseUser`.
    """

    user_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)
    user_code = serializers.CharField(source="user.employee_code", read_only=True)
    meter_name = serializers.CharField(source="meter.name", read_only=True)
    meter_number = serializers.CharField(source="meter.meter_number", read_only=True)
    meter_location = serializers.CharField(source="meter.location", read_only=True)
    meter_is_main = serializers.BooleanField(source="meter.is_main", read_only=True)
    assigned_by_name = serializers.SerializerMethodField()

    class Meta:
        model = UserElectricityMeter
        fields = [
            "id",
            "user",
            "user_name",
            "user_email",
            "user_code",
            "meter",
            "meter_name",
            "meter_number",
            "meter_location",
            "meter_is_main",
            "is_active",
            "assigned_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_assigned_by_name(self, obj) -> str:
        user = obj.created_by
        if not user:
            return ""
        return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


class UserElectricityMeterCreateSerializer(serializers.Serializer):
    """Assign one user to one or more meters.

    Takes a list because the page assigns a keeper their whole block in one
    action, and doing that as N requests would leave a half-configured keeper
    behind if one of them failed.
    """

    user = serializers.IntegerField()
    meters = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
    )

    def validate_meters(self, value):
        # De-duplicate rather than reject: the picker can legitimately hand the
        # same meter twice, and a 400 there would read as a bug in the page.
        seen = []
        for meter_id in value:
            if meter_id not in seen:
                seen.append(meter_id)
        return seen
