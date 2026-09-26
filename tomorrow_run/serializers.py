from rest_framework import serializers

from . import constants as C
from .models import PlanningSheet


class ChoiceSerializer(serializers.Serializer):
    for_date = serializers.DateField()
    machine = serializers.ChoiceField(choices=C.MACHINES)
    # "" clears the machine's pick ("Go back to our suggestion")
    job = serializers.CharField(max_length=60, allow_blank=True)
    why = serializers.CharField(allow_blank=True, required=False, default="")
    other = serializers.CharField(max_length=120, allow_blank=True, required=False, default="")


class SheetUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    stock_date = serializers.DateField(required=False, allow_null=True)

    def validate_file(self, f):
        if not f.name.lower().endswith((".xlsx", ".xlsm")):
            raise serializers.ValidationError("Put in the planning sheet as an Excel file (.xlsx).")
        if f.size > 15 * 1024 * 1024:
            raise serializers.ValidationError("That file is over 15 MB; the planning sheet is a few hundred KB.")
        return f


class PlanningSheetSerializer(serializers.ModelSerializer):
    uploaded_by = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    in_charge = serializers.SerializerMethodField()

    class Meta:
        model = PlanningSheet
        fields = [
            "id", "file_name", "file_url", "tab", "title", "stock_date", "date_basis", "header_row",
            "columns", "line_count", "net_req_l", "uploaded_by", "uploaded_at", "in_charge",
        ]

    def get_uploaded_by(self, obj):
        u = obj.uploaded_by
        return (getattr(u, "full_name", "") or getattr(u, "email", "")) if u else ""

    def get_file_url(self, obj):
        # absolute: FactoryFlow has no media helper, and a bare path 404s in dev
        request = self.context.get("request")
        if not obj.file:
            return None
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url

    def get_in_charge(self, obj):
        return obj.id == self.context.get("in_charge_id")
