"""Serializers for the warehouse-manager assignments."""

from rest_framework import serializers

from .models_manager import UserWarehouse


class UserWarehouseSerializer(serializers.ModelSerializer):
    """One assignment, with enough about the user to render a row.

    `user.full_name` rather than a nested user object: the table shows a name, a
    code and an email, and a nested serializer here would mean the frontend
    reaching into `row.user.full_name` for a screen that never needs the rest.
    Note `accounts.User` has no `get_full_name()` — it is a plain `full_name`
    field on an `AbstractBaseUser`.
    """

    user_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)
    user_code = serializers.CharField(source="user.employee_code", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    assigned_by_name = serializers.SerializerMethodField()

    class Meta:
        model = UserWarehouse
        fields = [
            "id",
            "user",
            "user_name",
            "user_email",
            "user_code",
            "company",
            "company_code",
            "warehouse_code",
            "is_active",
            "assigned_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "company", "created_at"]

    def get_assigned_by_name(self, obj) -> str:
        user = obj.assigned_by
        if not user:
            return ""
        return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


class UserWarehouseCreateSerializer(serializers.Serializer):
    """Assign one user to one or more warehouses in the active company.

    Takes a list because the page assigns a manager to their whole site in one
    action, and doing that as N requests would leave a half-configured manager
    behind if one of them failed.
    """

    user = serializers.IntegerField()
    warehouse_codes = serializers.ListField(
        child=serializers.CharField(max_length=50),
        allow_empty=False,
    )

    def validate_warehouse_codes(self, value):
        codes = []
        for code in value:
            code = (code or "").strip().upper()
            if code and code not in codes:
                codes.append(code)
        if not codes:
            raise serializers.ValidationError("Name at least one warehouse.")
        return codes
