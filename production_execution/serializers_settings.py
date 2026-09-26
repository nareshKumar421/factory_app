from rest_framework import serializers

from .models import ProductionSettings


class ProductionSettingsSerializer(serializers.ModelSerializer):
    updated_by_name = serializers.SerializerMethodField()
    # False until somebody saves the page: the values shown are the defaults.
    is_saved = serializers.SerializerMethodField()

    class Meta:
        model = ProductionSettings
        fields = [
            'rm_warehouse', 'pm_warehouse', 'fg_warehouse',
            'is_saved', 'updated_by_name', 'updated_at',
        ]
        read_only_fields = ['is_saved', 'updated_by_name', 'updated_at']

    def get_updated_by_name(self, obj):
        user = obj.updated_by
        if not user:
            return ''
        return getattr(user, 'full_name', '') or str(user)

    def get_is_saved(self, obj):
        return obj.pk is not None


class ProductionSettingsUpdateSerializer(serializers.Serializer):
    rm_warehouse = serializers.CharField(max_length=20, required=False)
    pm_warehouse = serializers.CharField(max_length=20, required=False)
    fg_warehouse = serializers.CharField(max_length=20, required=False)
