"""Tests for the fillable QC record forms (the "Documents" screen)."""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from quality_control.models import (
    QCRecord,
    RecordTemplate,
    RecordTemplateParameter,
    RecordTemplateSection,
    RecordTimeSlot,
    RecordValue,
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


class QCRecordTestBase(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.client = _client(self.company)
        self.template = RecordTemplate.objects.create(
            company=self.company,
            document_code="NMW-DAILY-WATER",
            title="NMW DAILY WATER MONITORING RECORD",
            revision_number="01",
            revision_date=date(2026, 5, 21),
        )
        self.borewell = RecordTemplateSection.objects.create(
            template=self.template, sequence=0, title="Borewell Water"
        )
        self.ph = RecordTemplateParameter.objects.create(
            section=self.borewell,
            sequence=0,
            sr_no="4",
            name="pH",
            frequency="Every Startup / Every 2 Hours",
            specification="6.5 - 8.5",
            value_type="NUMBER",
            min_value="6.5",
            max_value="8.5",
        )
        self.turbidity = RecordTemplateParameter.objects.create(
            section=self.borewell,
            sequence=1,
            sr_no="6",
            name="Turbidity",
            specification="Max 2.0 NTU",
            unit="NTU",
            value_type="NUMBER",
            max_value="2.0",
        )
        self.appearance = RecordTemplateParameter.objects.create(
            section=self.borewell,
            sequence=2,
            sr_no="3",
            name="Appearance",
            specification="Clear without any suspended particulates.",
            value_type="CHOICE",
            allowed_values=["Clear", "Not Clear"],
            conforming_values=["Clear"],
        )
        self.hardness = RecordTemplateParameter.objects.create(
            section=self.borewell,
            sequence=3,
            sr_no="8",
            name="Total Hardness",
            specification="To be tested",
            value_type="NUMBER",
        )

    def _open_record(self, record_date="2026-09-01"):
        resp = self.client.post(
            reverse("qc-record-list-create"),
            {"template": self.template.id, "record_date": record_date},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return resp.data["id"]

    def _save_cells(self, record_id, cells):
        return self.client.post(
            reverse("qc-record-values", args=[record_id]),
            {"cells": cells},
            format="json",
        )


class SpecCheckTests(QCRecordTestBase):
    def test_number_within_range_is_in_spec(self):
        self.assertIs(self.ph.check_value("7.63"), True)

    def test_number_outside_range_is_out_of_spec(self):
        self.assertIs(self.ph.check_value("9.1"), False)
        self.assertIs(self.ph.check_value("6.0"), False)

    def test_max_only_parameter_checks_the_ceiling(self):
        self.assertIs(self.turbidity.check_value("1.59"), True)
        self.assertIs(self.turbidity.check_value("2.4"), False)

    def test_blank_cell_is_not_judged(self):
        self.assertIsNone(self.ph.check_value(""))
        self.assertIsNone(self.ph.check_value(None))

    def test_parameter_with_no_limits_is_not_judged(self):
        self.assertIsNone(self.hardness.check_value("386"))

    def test_choice_parameter_matches_case_insensitively(self):
        self.assertIs(self.appearance.check_value("Clear"), True)
        self.assertIs(self.appearance.check_value("clear"), True)
        self.assertIs(self.appearance.check_value("Cloudy"), False)

    def test_a_failing_option_is_not_treated_as_passing(self):
        # "Not Clear" is offered in the dropdown but does not meet the spec.
        self.assertIn("Not Clear", self.appearance.allowed_values)
        self.assertIs(self.appearance.check_value("Not Clear"), False)

    def test_freely_typed_observation_is_judged_against_the_spec(self):
        # The operator may type anything; it still has to meet the spec.
        self.assertIs(self.appearance.check_value("Slightly cloudy"), False)
        self.assertIs(self.appearance.check_value("  clear  "), True)

    def test_choice_without_a_conforming_list_is_not_judged(self):
        free = RecordTemplateParameter.objects.create(
            section=self.borewell,
            sequence=9,
            name="Remark",
            value_type="CHOICE",
            allowed_values=["A", "B"],
        )
        self.assertIsNone(free.check_value("A"))
        self.assertIsNone(free.check_value("anything else"))

    def test_non_numeric_text_in_a_numeric_cell_is_out_of_spec(self):
        self.assertIs(self.ph.check_value("n/a"), False)


class RecordFillTests(QCRecordTestBase):
    def test_opening_a_record_twice_returns_the_same_sheet(self):
        first = self._open_record()
        resp = self.client.post(
            reverse("qc-record-list-create"),
            {"template": self.template.id, "record_date": "2026-09-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["id"], first)
        self.assertEqual(QCRecord.objects.count(), 1)

    def test_saving_cells_creates_the_time_columns_on_demand(self):
        rid = self._open_record()
        resp = self._save_cells(
            rid,
            [
                {"slot_time": "08:10", "parameter": self.ph.id, "value": "7.63"},
                {"slot_time": "10:15", "parameter": self.ph.id, "value": "7.61"},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(RecordTimeSlot.objects.filter(record_id=rid).count(), 2)
        self.assertEqual(RecordValue.objects.filter(record_id=rid).count(), 2)

    def test_resaving_the_same_cell_updates_rather_than_duplicates(self):
        rid = self._open_record()
        self._save_cells(rid, [{"slot_time": "08:10", "parameter": self.ph.id, "value": "7.63"}])
        self._save_cells(rid, [{"slot_time": "08:10", "parameter": self.ph.id, "value": "7.70"}])
        values = RecordValue.objects.filter(record_id=rid)
        self.assertEqual(values.count(), 1)
        self.assertEqual(values.first().value, "7.70")
        self.assertEqual(RecordTimeSlot.objects.filter(record_id=rid).count(), 1)

    def test_detail_reports_in_spec_per_cell(self):
        rid = self._open_record()
        self._save_cells(
            rid,
            [
                {"slot_time": "08:10", "parameter": self.ph.id, "value": "7.63"},
                {"slot_time": "08:10", "parameter": self.turbidity.id, "value": "3.9"},
                {"slot_time": "08:10", "parameter": self.hardness.id, "value": "386"},
            ],
        )
        data = self.client.get(reverse("qc-record-detail", args=[rid])).data
        by_param = {v["parameter"]: v for v in data["values"]}
        self.assertIs(by_param[self.ph.id]["in_spec"], True)
        self.assertIs(by_param[self.turbidity.id]["in_spec"], False)
        self.assertIsNone(by_param[self.hardness.id]["in_spec"])

    def test_a_parameter_from_another_form_is_rejected(self):
        other_template = RecordTemplate.objects.create(
            company=self.company, document_code="OTHER-FORM", title="Other"
        )
        other_section = RecordTemplateSection.objects.create(
            template=other_template, sequence=0, title="S"
        )
        foreign = RecordTemplateParameter.objects.create(
            section=other_section, sequence=0, name="Foreign"
        )
        rid = self._open_record()
        resp = self._save_cells(
            rid, [{"slot_time": "08:10", "parameter": foreign.id, "value": "1"}]
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(RecordValue.objects.count(), 0)

    def test_detail_embeds_the_blank_form(self):
        rid = self._open_record()
        data = self.client.get(reverse("qc-record-detail", args=[rid])).data
        sections = data["template_detail"]["sections"]
        self.assertEqual(sections[0]["title"], "Borewell Water")
        self.assertEqual(len(sections[0]["parameters"]), 4)
        self.assertEqual(sections[0]["parameters"][0]["specification"], "6.5 - 8.5")


class RecordWorkflowTests(QCRecordTestBase):
    def test_submit_then_approve(self):
        rid = self._open_record()
        self._save_cells(rid, [{"slot_time": "08:10", "parameter": self.ph.id, "value": "7.6"}])

        resp = self.client.post(reverse("qc-record-submit", args=[rid]))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["status"], "SUBMITTED")

        resp = self.client.post(
            reverse("qc-record-approve", args=[rid]),
            {"decision": "APPROVE", "remarks": "Looks fine"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["status"], "APPROVED")

    def test_an_approved_record_is_locked(self):
        rid = self._open_record()
        self.client.post(reverse("qc-record-submit", args=[rid]))
        self.client.post(
            reverse("qc-record-approve", args=[rid]), {"decision": "APPROVE"}, format="json"
        )
        resp = self._save_cells(
            rid, [{"slot_time": "09:00", "parameter": self.ph.id, "value": "7.0"}]
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_rejected_record_can_be_resubmitted(self):
        rid = self._open_record()
        self.client.post(reverse("qc-record-submit", args=[rid]))
        self.client.post(
            reverse("qc-record-approve", args=[rid]), {"decision": "REJECT"}, format="json"
        )
        self.assertEqual(QCRecord.objects.get(id=rid).status, "REJECTED")
        resp = self.client.post(reverse("qc-record-submit", args=[rid]))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

    def test_a_draft_cannot_be_approved_directly(self):
        rid = self._open_record()
        resp = self.client.post(
            reverse("qc-record-approve", args=[rid]), {"decision": "APPROVE"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class RecordListAndPermissionTests(QCRecordTestBase):
    def test_list_counts_slots_and_filled_cells(self):
        rid = self._open_record()
        self._save_cells(
            rid,
            [
                {"slot_time": "08:10", "parameter": self.ph.id, "value": "7.63"},
                {"slot_time": "08:10", "parameter": self.turbidity.id, "value": ""},
                {"slot_time": "10:15", "parameter": self.ph.id, "value": "7.61"},
            ],
        )
        row = self.client.get(reverse("qc-record-list-create")).data[0]
        self.assertEqual(row["slot_count"], 2)
        # The blank cell is stored but does not count as filled.
        self.assertEqual(row["filled_count"], 2)
        self.assertEqual(row["template_title"], "NMW DAILY WATER MONITORING RECORD")

    def test_list_filters_by_date_range(self):
        self._open_record("2026-09-01")
        self._open_record("2026-09-05")
        url = reverse("qc-record-list-create")
        self.assertEqual(len(self.client.get(url).data), 2)
        self.assertEqual(len(self.client.get(url, {"date_from": "2026-09-03"}).data), 1)
        self.assertEqual(len(self.client.get(url, {"date_to": "2026-09-03"}).data), 1)

    def test_delete_is_a_soft_retire(self):
        rid = self._open_record()
        resp = self.client.delete(reverse("qc-record-detail", args=[rid]))
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(QCRecord.objects.filter(id=rid).exists())
        self.assertEqual(len(self.client.get(reverse("qc-record-list-create")).data), 0)

    def test_another_company_cannot_read_the_record(self):
        rid = self._open_record()
        other = Company.objects.create(code="OTHER_CO", name="Other Co")
        resp = _client(other).get(reverse("qc-record-detail", args=[rid]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_viewer_cannot_fill_but_can_read(self):
        rid = self._open_record()
        viewer = _client(self.company, perms=["can_view_qc_records"])
        self.assertEqual(
            viewer.get(reverse("qc-record-detail", args=[rid])).status_code,
            status.HTTP_200_OK,
        )
        resp = viewer.post(
            reverse("qc-record-values", args=[rid]),
            {"cells": [{"slot_time": "08:10", "parameter": self.ph.id, "value": "7"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_filler_cannot_approve(self):
        rid = self._open_record()
        filler = _client(self.company, perms=["can_view_qc_records", "can_fill_qc_records"])
        filler.post(reverse("qc-record-submit", args=[rid]))
        resp = filler.post(
            reverse("qc-record-approve", args=[rid]), {"decision": "APPROVE"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_template_list_reports_parameter_and_record_counts(self):
        self._open_record()
        row = self.client.get(reverse("record-template-list-create")).data[0]
        self.assertEqual(row["parameter_count"], 4)
        self.assertEqual(row["record_count"], 1)
        self.assertEqual(row["revision_label"], "01/21-05-2026")

    def test_a_form_with_filled_records_refuses_a_parameter_rewrite(self):
        rid = self._open_record()
        self._save_cells(rid, [{"slot_time": "08:10", "parameter": self.ph.id, "value": "7.6"}])
        resp = self.client.put(
            reverse("record-template-detail", args=[self.template.id]),
            {"sections": [{"sequence": 0, "title": "Changed", "parameters": []}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("sections", resp.data)
