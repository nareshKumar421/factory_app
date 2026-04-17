from rest_framework import serializers
from .models import LabourVerificationRequest, DepartmentLabourResponse


class DepartmentLabourResponseSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = DepartmentLabourResponse
        fields = [
            "id",
            "department",
            "department_name",
            "labour_count",
            "labour_details",
            "remarks",
            "status",
            "submitted_by",
            "submitted_by_name",
            "submitted_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "department", "status", "submitted_by",
            "submitted_at", "created_at", "updated_at",
        ]


class LabourVerificationRequestListSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=None
    )
    total_departments = serializers.IntegerField(read_only=True)
    submitted_count = serializers.IntegerField(read_only=True)
    pending_count = serializers.IntegerField(read_only=True)
    total_labour_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = LabourVerificationRequest
        fields = [
            "id",
            "date",
            "status",
            "created_by",
            "created_by_name",
            "created_at",
            "closed_at",
            "remarks",
            "total_departments",
            "submitted_count",
            "pending_count",
            "total_labour_count",
        ]


class LabourVerificationRequestDetailSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=None
    )
    responses = DepartmentLabourResponseSerializer(many=True, read_only=True)
    total_departments = serializers.SerializerMethodField()
    submitted_count = serializers.SerializerMethodField()
    pending_count = serializers.SerializerMethodField()
    total_labour_count = serializers.SerializerMethodField()

    class Meta:
        model = LabourVerificationRequest
        fields = [
            "id",
            "date",
            "status",
            "created_by",
            "created_by_name",
            "created_at",
            "closed_at",
            "remarks",
            "responses",
            "total_departments",
            "submitted_count",
            "pending_count",
            "total_labour_count",
        ]

    def get_total_departments(self, obj):
        return obj.responses.count()

    def get_submitted_count(self, obj):
        return obj.responses.filter(status="SUBMITTED").count()

    def get_pending_count(self, obj):
        return obj.responses.filter(status="PENDING").count()

    def get_total_labour_count(self, obj):
        return sum(
            r.labour_count for r in obj.responses.filter(status="SUBMITTED")
        )


class SubmitLabourResponseSerializer(serializers.Serializer):
    labour_count = serializers.IntegerField(min_value=0)
    labour_details = serializers.ListField(
        child=serializers.DictField(), required=False, default=list
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
