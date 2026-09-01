"""Tests for the controlled testing procedure ("QC Documents") APIs."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from quality_control.models import (
    ProcedureType,
    TestingProcedure,
    TestingProcedureLine,
    TestingProcedureSection,
)

User = get_user_model()


def _client(company, perms="all"):
    n = User.objects.count()
    user = User.objects.create_user(
        email=f"u{n}@t.com", password="x", full_name=f"User {n}", employee_code=f"E{n}"
    )
    role = UserRole.objects.create(name=f"R{UserRole.objects.count()}")
    UserCompany.objects.create(user=user, company=company, role=role, is_active=True)
    qs = Permission.objects.filter(content_type__app_label="quality_control")
    if perms != "all":
        qs = qs.filter(codename__in=perms)
    user.user_permissions.set(qs)
    user = User.objects.get(pk=user.pk)
    c = APIClient()
    c.force_authenticate(user=user)
    c.credentials(HTTP_COMPANY_CODE=company.code)
    return c


ARGEMONE_PAYLOAD = {
    "document_code": "QA-TST-INH-14-02-10",
    "title": "ARGEMONE OIL ADULTERATION TESTING",
    "procedure_type": ProcedureType.INHOUSE,
    "heading": "INHOUSE TESTING PROCEDURE",
    "organisation": "JIVO WELLNESS PVT.LTD.",
    "revision_number": "00",
    "revision_date": "2023-10-15",
    "total_pages": 2,
    "classification": "Business Confidential",
    "source_text": "1. Scope\nThis method is applicable ...",
    "sections": [
        {
            "sequence": 0,
            "section_number": "1",
            "section_key": "SCOPE",
            "title": "Scope",
            "body": "This method is applicable for the qualitative detection "
            "of Argemone Oil adulteration in edible oil samples.",
            "lines": [],
        },
        {
            "sequence": 1,
            "section_number": "4",
            "section_key": "APPARATUS",
            "title": "Apparatus / Glassware",
            "body": "",
            "lines": [
                {
                    "sequence": 0,
                    "kind": "BULLET",
                    "marker": "",
                    "text": "Clean and dry graduated test tube with stopper",
                    "interpretation": "",
                },
                {
                    "sequence": 1,
                    "kind": "BULLET",
                    "marker": "",
                    "text": "Test tube stand",
                    "interpretation": "",
                },
            ],
        },
        {
            "sequence": 2,
            "section_number": "8",
            "section_key": "OBSERVATION",
            "title": "Observation and Interpretation",
            "body": "",
            "lines": [
                {
                    "sequence": 0,
                    "kind": "TABLE_ROW",
                    "marker": "",
                    "text": "No pink/reddish colour observed",
                    "interpretation": "Negative for Argemone Oil adulteration "
                    "by this qualitative screening test",
                },
                {
                    "sequence": 1,
                    "kind": "TABLE_ROW",
                    "marker": "",
                    "text": "Pink to reddish colour observed",
                    "interpretation": "Positive / Suspected presence of Argemone Oil",
                },
            ],
        },
    ],
}


class TestingProcedureAPITests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.client = _client(self.company)

    def _create(self, **overrides):
        payload = {**ARGEMONE_PAYLOAD, **overrides}
        return self.client.post(
            reverse("testing-procedure-list-create"), payload, format="json"
        )

    def test_create_stores_header_sections_and_lines(self):
        resp = self._create()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        procedure = TestingProcedure.objects.get(id=resp.data["id"])
        self.assertEqual(procedure.company, self.company)
        self.assertEqual(procedure.document_code, "QA-TST-INH-14-02-10")
        self.assertEqual(procedure.procedure_type, ProcedureType.INHOUSE)
        self.assertEqual(procedure.revision_label, "00/15-10-2023")
        self.assertEqual(procedure.sections.count(), 3)
        self.assertEqual(TestingProcedureLine.objects.count(), 4)

        observation = procedure.sections.get(section_key="OBSERVATION")
        rows = list(observation.lines.all())
        self.assertEqual(rows[0].text, "No pink/reddish colour observed")
        self.assertTrue(rows[0].interpretation.startswith("Negative"))
        self.assertEqual(rows[1].kind, "TABLE_ROW")

    def test_sections_and_lines_keep_pasted_order(self):
        self._create()
        procedure = TestingProcedure.objects.get()
        self.assertEqual(
            [s.section_number for s in procedure.sections.all()], ["1", "4", "8"]
        )
        apparatus = procedure.sections.get(section_key="APPARATUS")
        self.assertEqual(
            [line.text for line in apparatus.lines.all()],
            [
                "Clean and dry graduated test tube with stopper",
                "Test tube stand",
            ],
        )

    def test_duplicate_document_code_is_a_field_error_not_a_500(self):
        self._create()
        resp = self._create()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_code", resp.data)

    def test_same_code_allowed_for_a_different_company(self):
        other = Company.objects.create(code="OTHER_CO", name="Other Co")
        other_client = _client(other)
        resp = other_client.post(
            reverse("testing-procedure-list-create"), ARGEMONE_PAYLOAD, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self._create()
        self.assertEqual(TestingProcedure.objects.count(), 2)

    def test_document_code_is_upper_cased_and_trimmed(self):
        resp = self._create(document_code="  qa-tst-std-14-02-11  ")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["document_code"], "QA-TST-STD-14-02-11")

    def test_update_replaces_the_body_wholesale(self):
        created = self._create()
        pid = created.data["id"]
        resp = self.client.put(
            reverse("testing-procedure-detail", args=[pid]),
            {
                "title": "ARGEMONE OIL ADULTERATION TESTING",
                "sections": [
                    {
                        "sequence": 0,
                        "section_number": "1",
                        "section_key": "SCOPE",
                        "title": "Scope",
                        "body": "Revised scope.",
                        "lines": [],
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        procedure = TestingProcedure.objects.get(id=pid)
        self.assertEqual(procedure.sections.count(), 1)
        # Old sections cascaded away with their lines -- no orphans left behind.
        self.assertEqual(TestingProcedureLine.objects.count(), 0)
        self.assertEqual(TestingProcedureSection.objects.count(), 1)

    def test_header_only_edit_keeps_sections(self):
        pid = self._create().data["id"]
        resp = self.client.put(
            reverse("testing-procedure-detail", args=[pid]),
            {"status": "ARCHIVED"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        procedure = TestingProcedure.objects.get(id=pid)
        self.assertEqual(procedure.status, "ARCHIVED")
        self.assertEqual(procedure.sections.count(), 3)

    def test_list_filters_by_type_and_search(self):
        self._create()
        self._create(
            document_code="QA-TST-STD-14-02-11",
            title="PEROXIDE VALUE TESTING",
            procedure_type=ProcedureType.STANDARD,
            heading="STANDARD TESTING PROCEDURE",
        )

        url = reverse("testing-procedure-list-create")
        self.assertEqual(len(self.client.get(url).data), 2)
        self.assertEqual(len(self.client.get(url, {"procedure_type": "STANDARD"}).data), 1)
        self.assertEqual(len(self.client.get(url, {"search": "argemone"}).data), 1)
        self.assertEqual(len(self.client.get(url, {"search": "QA-TST-STD"}).data), 1)

    def test_list_reports_section_and_line_counts(self):
        self._create()
        row = self.client.get(reverse("testing-procedure-list-create")).data[0]
        self.assertEqual(row["section_count"], 3)
        self.assertEqual(row["line_count"], 4)
        self.assertEqual(row["revision_label"], "00/15-10-2023")

    def test_counts_endpoint_splits_by_type(self):
        self._create()
        self._create(
            document_code="QA-TST-STD-14-02-11",
            title="PEROXIDE VALUE TESTING",
            procedure_type=ProcedureType.STANDARD,
        )
        data = self.client.get(reverse("testing-procedure-counts")).data
        self.assertEqual(data, {"total": 2, "inhouse": 1, "standard": 1})

    def test_delete_is_a_soft_retire(self):
        pid = self._create().data["id"]
        resp = self.client.delete(reverse("testing-procedure-detail", args=[pid]))
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(TestingProcedure.objects.filter(id=pid).exists())
        self.assertFalse(TestingProcedure.objects.get(id=pid).is_active)
        self.assertEqual(len(self.client.get(reverse("testing-procedure-list-create")).data), 0)

    def test_detail_returns_nested_sections(self):
        pid = self._create().data["id"]
        data = self.client.get(reverse("testing-procedure-detail", args=[pid])).data
        self.assertEqual(len(data["sections"]), 3)
        self.assertEqual(data["sections"][1]["section_key_label"], "Apparatus / Glassware")
        self.assertEqual(len(data["sections"][1]["lines"]), 2)

    def test_other_company_cannot_read_the_procedure(self):
        pid = self._create().data["id"]
        other = Company.objects.create(code="OTHER_CO", name="Other Co")
        resp = _client(other).get(reverse("testing-procedure-detail", args=[pid]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_viewer_without_manage_permission_cannot_create(self):
        viewer = _client(self.company, perms=["can_view_testing_procedures"])
        resp = viewer.post(
            reverse("testing-procedure-list-create"), ARGEMONE_PAYLOAD, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            viewer.get(reverse("testing-procedure-list-create")).status_code,
            status.HTTP_200_OK,
        )

    def test_user_without_view_permission_is_blocked(self):
        nobody = _client(self.company, perms=["can_manage_material_types"])
        resp = nobody.get(reverse("testing-procedure-list-create"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_blank_title_is_rejected(self):
        resp = self._create(title="   ")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("title", resp.data)
