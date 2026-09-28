"""What the licence endpoints accept and return.

Write serializers validate shape only; the rules are ``exim.services_licence``'s.
Decimals go out as strings, as everywhere else in this project, so a value
like 484.151 reaches the screen exactly as it is stored.
"""

from decimal import Decimal

from rest_framework import serializers

from .models_licence import Licence, LicenceKind, LicenceLine, LicenceStatus, LineDirection


def _positive(**kwargs):
    return serializers.DecimalField(
        max_digits=18, decimal_places=3, min_value=Decimal("0.001"), **kwargs
    )


def _not_negative(**kwargs):
    return serializers.DecimalField(max_digits=18, decimal_places=3, min_value=Decimal("0"), **kwargs)


class LicenceUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=LicenceStatus.choices)
    issue_date = serializers.DateField()
    import_validity = serializers.DateField()
    export_validity = serializers.DateField()
    cif_value_inr = _not_negative()
    cif_exchange_rate = serializers.DecimalField(
        max_digits=10, decimal_places=3, min_value=Decimal("0.001")
    )
    fob_value_inr = _not_negative()
    fob_exchange_rate = serializers.DecimalField(
        max_digits=10, decimal_places=3, min_value=Decimal("0.001")
    )
    authorised_qty_mts = _not_negative()


class LicenceCreateSerializer(LicenceUpdateSerializer):
    kind = serializers.ChoiceField(choices=LicenceKind.choices)
    number = serializers.CharField(max_length=50, trim_whitespace=True)
    status = serializers.ChoiceField(choices=LicenceStatus.choices, default=LicenceStatus.OPEN)


class LineWriteSerializer(serializers.Serializer):
    document_no = serializers.CharField(max_length=50, trim_whitespace=True)
    document_date = serializers.DateField(allow_null=True, required=False)
    value_usd = _not_negative()
    quantity_mts = _positive()
    linked_line = serializers.PrimaryKeyRelatedField(
        queryset=LicenceLine.objects.all(), allow_null=True, required=False,
    )


class LineCreateSerializer(LineWriteSerializer):
    direction = serializers.ChoiceField(choices=LineDirection.choices)


def _name(user):
    return (user.full_name or user.email) if user else None


class LicenceLineSerializer(serializers.ModelSerializer):
    linked_document_no = serializers.CharField(source="linked_line.document_no", default=None)

    class Meta:
        model = LicenceLine
        fields = [
            "id",
            "direction",
            "document_no",
            "document_date",
            "value_usd",
            "quantity_mts",
            "linked_line",
            "linked_document_no",
            "created_at",
            "updated_at",
        ]


class LicenceListSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display")
    first_leg = serializers.CharField()

    class Meta:
        model = Licence
        fields = [
            "id",
            "kind",
            "kind_label",
            "number",
            "status",
            "issue_date",
            "import_validity",
            "export_validity",
            "cif_value_inr",
            "cif_exchange_rate",
            "cif_value_usd",
            "fob_value_inr",
            "fob_exchange_rate",
            "fob_value_usd",
            "authorised_qty_mts",
            "total_import_mts",
            "total_export_mts",
            "obligation_mts",
            "balance_mts",
            "first_leg",
            "updated_at",
        ]


class LicenceDetailSerializer(LicenceListSerializer):
    lines = LicenceLineSerializer(many=True, read_only=True)
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()
    copied_from_exim = serializers.SerializerMethodField()

    class Meta(LicenceListSerializer.Meta):
        fields = LicenceListSerializer.Meta.fields + [
            "lines",
            "created_at",
            "created_by_name",
            "updated_by_name",
            "copied_from_exim",
        ]

    def get_created_by_name(self, obj):
        return _name(obj.created_by)

    def get_updated_by_name(self, obj):
        return _name(obj.updated_by)

    def get_copied_from_exim(self, obj):
        return obj.exim_ref is not None
