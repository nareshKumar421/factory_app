from rest_framework import serializers

from .models import CostType, CostRate, CostBasis, CostScope


class CostTypeSerializer(serializers.ModelSerializer):
    default_basis_display = serializers.CharField(
        source='get_default_basis_display', read_only=True)

    class Meta:
        model = CostType
        fields = ['id', 'code', 'name', 'description', 'default_basis',
                  'default_basis_display', 'is_credit', 'is_active',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'default_basis_display', 'created_at', 'updated_at']


class CostTypeCreateSerializer(serializers.Serializer):
    code = serializers.SlugField(max_length=60)
    name = serializers.CharField(max_length=150)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    default_basis = serializers.ChoiceField(choices=CostBasis.choices, required=False)
    is_credit = serializers.BooleanField(required=False, default=False)


class CostTypeUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    default_basis = serializers.ChoiceField(choices=CostBasis.choices, required=False)
    is_credit = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)


class CostRateSerializer(serializers.ModelSerializer):
    cost_type_code = serializers.CharField(source='cost_type.code', read_only=True)
    cost_type_name = serializers.CharField(source='cost_type.name', read_only=True)
    is_credit = serializers.BooleanField(source='cost_type.is_credit', read_only=True)
    company_code = serializers.CharField(
        source='company.code', read_only=True, default=None)
    department_name = serializers.CharField(
        source='department.name', read_only=True, default=None)
    scope_display = serializers.CharField(source='get_scope_display', read_only=True)
    basis_display = serializers.CharField(source='get_basis_display', read_only=True)

    class Meta:
        model = CostRate
        fields = ['id', 'cost_type', 'cost_type_code', 'cost_type_name', 'is_credit',
                  'scope', 'scope_display', 'company', 'company_code',
                  'department', 'department_name', 'value_key',
                  'basis', 'basis_display', 'rate', 'effective_from', 'notes',
                  'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'cost_type_code', 'cost_type_name', 'is_credit',
                            'scope_display', 'company_code', 'department_name',
                            'basis_display', 'created_at', 'updated_at']


class CostRateUpsertSerializer(serializers.Serializer):
    cost_type_id = serializers.IntegerField()
    scope = serializers.ChoiceField(choices=CostScope.choices)
    company_id = serializers.IntegerField(required=False, allow_null=True)
    department_id = serializers.IntegerField(required=False, allow_null=True)
    value_key = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default='')
    basis = serializers.ChoiceField(choices=CostBasis.choices, required=False)
    rate = serializers.DecimalField(max_digits=15, decimal_places=4)
    notes = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default='')
    # Omitted = effective today. A past date backdates the rate; a future date
    # schedules it. Either way the superseded row is kept.
    effective_from = serializers.DateField(required=False)
