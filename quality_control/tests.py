from django.contrib.auth.models import Permission
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from quality_control.enums import ParameterType
from quality_control.models import MaterialType, MaterialTypeSAPItem, QCParameterMaster


class MaterialTypeCopyParametersAPITests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", code="TEST_CO")
        self.role = UserRole.objects.create(name="QC Manager")
        self.user = User.objects.create_user(
            email="qc@example.com",
            password="password",
            full_name="QC Manager",
            employee_code="QC001",
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=self.role,
            is_default=True,
            is_active=True,
        )
        permissions = Permission.objects.filter(
            content_type__app_label="quality_control",
            codename__in=["can_manage_material_types", "can_manage_qc_parameters"],
        )
        self.user.user_permissions.add(*permissions)
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        self.source_type = MaterialType.objects.create(
            company=self.company,
            code="OLD_TYPE",
            name="Old Type",
        )
        MaterialTypeSAPItem.objects.create(
            company=self.company,
            material_type=self.source_type,
            item_code="SAP-OLD",
            item_name="Old SAP Item",
        )
        QCParameterMaster.objects.create(
            material_type=self.source_type,
            parameter_code="WEIGHT",
            parameter_name="Weight",
            standard_value="10",
            parameter_type=ParameterType.RANGE,
            min_value="9.0000",
            max_value="11.0000",
            uom="KG",
            sequence=1,
            is_mandatory=True,
        )
        QCParameterMaster.objects.create(
            material_type=self.source_type,
            parameter_code="COLOR",
            parameter_name="Color",
            standard_value="Blue",
            parameter_type=ParameterType.TEXT,
            sequence=2,
            is_mandatory=False,
        )

    def test_create_material_type_can_copy_only_parameters_from_source_material_type(self):
        response = self.client.post(
            "/api/v1/quality-control/material-types/",
            {
                "code": "NEW_TYPE",
                "name": "New Type",
                "description": "Created from old type parameters",
                "copy_parameters_from_material_type_id": self.source_type.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        target_type = MaterialType.objects.get(code="NEW_TYPE", company=self.company)

        target_parameters = list(
            QCParameterMaster.objects.filter(
                material_type=target_type,
                is_active=True,
            ).order_by("sequence")
        )
        self.assertEqual([param.parameter_code for param in target_parameters], ["WEIGHT", "COLOR"])
        self.assertEqual(target_parameters[0].parameter_name, "Weight")
        self.assertEqual(target_parameters[0].standard_value, "10")
        self.assertEqual(target_parameters[0].parameter_type, ParameterType.RANGE)
        self.assertEqual(target_parameters[0].uom, "KG")
        self.assertEqual(target_parameters[1].is_mandatory, False)
        self.assertEqual(target_type.sap_items.filter(is_active=True).count(), 0)

    def test_update_material_type_does_not_copy_parameters(self):
        target_type = MaterialType.objects.create(
            company=self.company,
            code="NEW_TYPE",
            name="New Type",
        )

        response = self.client.put(
            f"/api/v1/quality-control/material-types/{target_type.id}/",
            {
                "code": "NEW_TYPE",
                "name": "Updated New Type",
                "description": "",
                "copy_parameters_from_material_type_id": self.source_type.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(target_type.qc_parameters.filter(is_active=True).count(), 0)
