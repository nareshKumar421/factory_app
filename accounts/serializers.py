from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from company.models import UserCompany
from rest_framework import serializers
from .models import Department, User
from company.models import UserCompany
from django.conf import settings

class LoginSerializer(TokenObtainPairSerializer):


    ## Before login they fetch user data
    def validate(self, attrs):
        data = super().validate(attrs)

        companies = UserCompany.objects.filter(
            user=self.user,
            is_active=True
        ).values(
            "company_id",
            "company__name",
            "company__code",
            "role__name",
            "is_default"
        )

        for i in companies:
            i["company_name"] = i.pop("company__name")
            i["company_code"] = i.pop("company__code")
            i["role"] = i.pop("role__name")

        data["token"] = {
            "access_expires_in": settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds(),
            "refresh_expires_in": settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds(),
        } 
        

        data["user"] = {
            "id": self.user.id,
            "email": self.user.email,
            "full_name": self.user.full_name,
            "companies": list(companies),
        }
        return data



class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password =  serializers.CharField()



class UserCompanySerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    role = serializers.CharField(source="role.name", read_only=True)

    class Meta:
        model = UserCompany
        fields = [               ## I think these are database fields
            "company_id",
            "company_name",
            "company_code",
            "role",
            "is_default",
            "is_active",
        ]


class MeSerializer(serializers.ModelSerializer):
    companies = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    department_id = serializers.IntegerField(source="department.id", read_only=True, default=None)
    department_name = serializers.CharField(source="department.name", read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "employee_code",
            "is_active",
            "is_staff",
            "date_joined",
            "department_id",
            "department_name",
            "companies",
            "permissions",
        ]

    def get_companies(self, obj):
        qs = UserCompany.objects.filter(user=obj, is_active=True)
        return UserCompanySerializer(qs, many=True).data
    
    def get_permissions(self, obj):
        """
        Returns ALL permissions assigned to the user:
        - direct permissions
        - group permissions
        """
        return sorted(list(obj.get_all_permissions()))
    


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = "__all__"


class UserSerializer(serializers.ModelSerializer):
    department_id = serializers.IntegerField(source="department.id", read_only=True, default=None)
    department_name = serializers.CharField(source="department.name", read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "employee_code",
            "is_active",
            "is_staff",
            "date_joined",
            "department_id",
            "department_name",
        ]

    