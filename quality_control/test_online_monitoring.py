"""Tests for the Online Quality Monitoring module."""

import tempfile
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from production_execution.models import ProductionLine, ProductionRun, RunStatus
from quality_control.models.online_monitoring import (
    OnlineQualityRecord,
    OnlineQualitySpec,
    OnlineRecordStatus,
    SpecValidationType,
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


class OnlineMonitoringFlowTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.line = ProductionLine.objects.create(company=self.company, name="Line-1")
        self.client = _client(self.company)

    def _create_record(self):
        resp = self.client.post(reverse("online-monitoring-list"), {
            "production_line_id": self.line.id,
            "date": str(date.today()),
            "sku": "FG0000001", "product_name": "Water 1L",
            "flavour": "Plain", "shift": "A", "batch_no": "B-100",
        }, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return resp.data["id"]

    def test_create_record_starts_draft(self):
        rid = self._create_record()
        record = OnlineQualityRecord.objects.get(id=rid)
        self.assertEqual(record.status, OnlineRecordStatus.DRAFT)
        self.assertEqual(record.company, self.company)
        self.assertEqual(record.batch_no, "B-100")

    def test_add_reading_with_torque_heads(self):
        rid = self._create_record()
        resp = self.client.post(
            reverse("online-monitoring-reading-create", args=[rid]),
            {
                "reading_time": "08:00",
                "filler_speed": "16000",
                "taste": "ACCEPTABLE", "aroma": "ACCEPTABLE", "appearance": "ACCEPTABLE",
                "ph": "7.24", "tds": "172",
                "package_attribute": "OK", "date_code": "OK",
                "rub_test": "PASS", "closure_jump_test": "PASS",
                "torque_heads": [
                    {"head_no": 1, "torque_value": "10"},
                    {"head_no": 2, "torque_value": "11"},
                    {"head_no": 3, "torque_value": "9"},
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(len(resp.data["torque_heads"]), 3)

        detail = self.client.get(reverse("online-monitoring-detail", args=[rid]))
        self.assertEqual(len(detail.data["readings"]), 1)
        self.assertEqual(str(detail.data["readings"][0]["ph"]), "7.240")

    def test_full_lifecycle_submit_approve(self):
        rid = self._create_record()
        # Can't submit with no readings.
        empty = self.client.post(reverse("online-monitoring-submit", args=[rid]))
        self.assertEqual(empty.status_code, 400)

        self.client.post(reverse("online-monitoring-reading-create", args=[rid]),
                         {"reading_time": "09:00"}, format="json")
        sub = self.client.post(reverse("online-monitoring-submit", args=[rid]))
        self.assertEqual(sub.status_code, 200)
        self.assertEqual(sub.data["status"], "SUBMITTED")

        # No more edits once submitted.
        edit = self.client.post(reverse("online-monitoring-reading-create", args=[rid]),
                                {"reading_time": "10:00"}, format="json")
        self.assertEqual(edit.status_code, 400)

        appr = self.client.post(reverse("online-monitoring-approve", args=[rid]),
                                {"remarks": "ok"}, format="json")
        self.assertEqual(appr.status_code, 200)
        self.assertEqual(appr.data["status"], "APPROVED")

    def test_list_filters(self):
        rid = self._create_record()
        r = self.client.get(reverse("online-monitoring-list"), {"production_line": self.line.id})
        self.assertEqual([x["id"] for x in r.data], [rid])
        r2 = self.client.get(reverse("online-monitoring-list"), {"status": "APPROVED"})
        self.assertEqual(r2.data, [])

    def test_specs_endpoint_returns_global_and_company(self):
        # The seed data-migration is skipped under test syncdb, so create the
        # global defaults here to exercise the endpoint (company OR global).
        OnlineQualitySpec.objects.create(
            company=None, parameter_key="ph", parameter_name="pH",
            min_value=6.5, max_value=8.5, validation_type=SpecValidationType.RANGE,
        )
        OnlineQualitySpec.objects.create(
            company=self.company, parameter_key="torque", parameter_name="Torque",
            min_value=8, max_value=12, validation_type=SpecValidationType.RANGE,
        )
        r = self.client.get(reverse("online-monitoring-specs"))
        keys = {s["parameter_key"] for s in r.data}
        self.assertIn("ph", keys)  # global
        self.assertIn("torque", keys)  # company

    def test_company_scoping(self):
        rid = self._create_record()
        other = Company.objects.create(code="OTHER_CO", name="Other")
        other_client = _client(other)
        # Other company can't see this record.
        self.assertEqual(
            other_client.get(reverse("online-monitoring-detail", args=[rid])).status_code, 404
        )


class OnlineMonitoringPermissionTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.line = ProductionLine.objects.create(company=self.company, name="Line-1")

    def test_operator_cannot_approve(self):
        # Operator: view + create only (no approve).
        operator = _client(self.company, perms=[
            "can_view_online_monitoring", "can_create_online_monitoring",
            "can_submit_online_monitoring",
        ])
        rid = operator.post(reverse("online-monitoring-list"), {
            "production_line_id": self.line.id, "date": str(date.today()),
        }, format="json").data["id"]
        operator.post(reverse("online-monitoring-reading-create", args=[rid]),
                      {"reading_time": "08:00"}, format="json")
        operator.post(reverse("online-monitoring-submit", args=[rid]))
        resp = operator.post(reverse("online-monitoring-approve", args=[rid]))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class SpecValidationTests(APITestCase):
    def test_is_within_spec(self):
        ph = OnlineQualitySpec(
            parameter_key="ph", parameter_name="pH", min_value=6.5, max_value=8.5,
            validation_type=SpecValidationType.RANGE,
        )
        self.assertTrue(ph.is_within_spec(7.2))
        self.assertFalse(ph.is_within_spec(9.0))
        self.assertFalse(ph.is_within_spec(6.0))
        self.assertIsNone(ph.is_within_spec(None))

        turb = OnlineQualitySpec(
            parameter_key="turbidity", parameter_name="Turbidity", max_value=1,
            validation_type=SpecValidationType.MAX,
        )
        self.assertTrue(turb.is_within_spec(0.5))
        self.assertFalse(turb.is_within_spec(2))

        torque = OnlineQualitySpec(
            parameter_key="torque", parameter_name="Torque", min_value=8, max_value=12,
            validation_type=SpecValidationType.RANGE,
        )
        self.assertTrue(torque.is_within_spec(10))
        self.assertFalse(torque.is_within_spec(7))
        self.assertFalse(torque.is_within_spec(13))


class OnlineMonitoringEdgeCaseTests(APITestCase):
    """Harder paths: torque replacement, reject, guards, header edits, deletes."""

    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.line = ProductionLine.objects.create(company=self.company, name="Line-1")
        self.client = _client(self.company)

    def _record(self):
        return self.client.post(reverse("online-monitoring-list"), {
            "production_line_id": self.line.id, "date": str(date.today()),
        }, format="json").data["id"]

    def _add_reading(self, rid, **extra):
        payload = {"reading_time": "08:00", **extra}
        return self.client.post(
            reverse("online-monitoring-reading-create", args=[rid]), payload, format="json"
        )

    def test_torque_heads_replaced_on_update(self):
        rid = self._record()
        r = self._add_reading(rid, torque_heads=[
            {"head_no": 1, "torque_value": "10"},
            {"head_no": 2, "torque_value": "11"},
        ])
        reading_id = r.data["id"]
        # Update: send a different head set — must fully replace, not append.
        resp = self.client.patch(
            reverse("online-monitoring-reading-detail", args=[rid, reading_id]),
            {"torque_heads": [{"head_no": 1, "torque_value": "12"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(len(resp.data["torque_heads"]), 1)
        self.assertEqual(resp.data["torque_heads"][0]["head_no"], 1)
        self.assertEqual(str(resp.data["torque_heads"][0]["torque_value"]), "12.00")

    def test_update_header_on_draft_then_blocked_after_submit(self):
        rid = self._record()
        ok = self.client.patch(reverse("online-monitoring-detail", args=[rid]),
                               {"batch_no": "B-9", "remarks": "note"}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data["batch_no"], "B-9")

        self._add_reading(rid)
        self.client.post(reverse("online-monitoring-submit", args=[rid]))
        blocked = self.client.patch(reverse("online-monitoring-detail", args=[rid]),
                                    {"batch_no": "B-X"}, format="json")
        self.assertEqual(blocked.status_code, 400)

    def test_delete_reading_on_draft(self):
        rid = self._record()
        reading_id = self._add_reading(rid).data["id"]
        resp = self.client.delete(
            reverse("online-monitoring-reading-detail", args=[rid, reading_id])
        )
        self.assertEqual(resp.status_code, 204)
        detail = self.client.get(reverse("online-monitoring-detail", args=[rid]))
        self.assertEqual(len(detail.data["readings"]), 0)

    def test_cannot_approve_a_draft(self):
        rid = self._record()
        self._add_reading(rid)
        # Draft (not submitted) can't be approved.
        self.assertEqual(
            self.client.post(reverse("online-monitoring-approve", args=[rid])).status_code, 400
        )

    def test_reject_flow_records_remarks(self):
        rid = self._record()
        self._add_reading(rid)
        self.client.post(reverse("online-monitoring-submit", args=[rid]))
        resp = self.client.post(reverse("online-monitoring-reject", args=[rid]),
                                {"remarks": "pH out of spec"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "REJECTED")
        self.assertEqual(resp.data["rejection_remarks"], "pH out of spec")

    def test_delete_draft_record_but_not_submitted(self):
        rid = self._record()
        self._add_reading(rid)
        # Submit, then deletion is blocked.
        self.client.post(reverse("online-monitoring-submit", args=[rid]))
        self.assertEqual(
            self.client.delete(reverse("online-monitoring-detail", args=[rid])).status_code, 400
        )

    def test_reading_count_in_list(self):
        rid = self._record()
        self._add_reading(rid, reading_time="08:00")
        self._add_reading(rid, reading_time="10:00")
        row = next(r for r in self.client.get(reverse("online-monitoring-list")).data if r["id"] == rid)
        self.assertEqual(row["reading_count"], 2)

    def test_spec_min_validation(self):
        from quality_control.models.online_monitoring import OnlineQualitySpec, SpecValidationType
        s = OnlineQualitySpec(parameter_key="x", parameter_name="X",
                              min_value=5, validation_type=SpecValidationType.MIN)
        self.assertTrue(s.is_within_spec(6))
        self.assertTrue(s.is_within_spec(5))
        self.assertFalse(s.is_within_spec(4))
        self.assertIsNone(s.is_within_spec(""))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class OnlineMonitoringAttachmentTests(APITestCase):
    """Per-reading photo/PDF attachments."""

    def setUp(self):
        self.company = Company.objects.create(code="ATT_CO", name="Att Co")
        self.line = ProductionLine.objects.create(company=self.company, name="Line-A")
        self.client = _client(self.company)

    def _record_with_reading(self):
        rid = self.client.post(reverse("online-monitoring-list"), {
            "production_line_id": self.line.id, "date": str(date.today()),
            "sku": "FG1", "shift": "A", "batch_no": "B-1",
        }, format="json").data["id"]
        reading = self.client.post(
            reverse("online-monitoring-reading-create", args=[rid]),
            {"reading_time": "08:00", "ph": "7.2"}, format="json",
        )
        return rid, reading.data["id"]

    def _upload(self, rid, reading_id, name="photo.jpg", ctype="image/jpeg", body=b"\xff\xd8\xff\xe0data"):
        f = SimpleUploadedFile(name, body, content_type=ctype)
        return self.client.post(
            reverse("online-monitoring-reading-attachments", args=[rid, reading_id]),
            {"file": f}, format="multipart",
        )

    def test_upload_image_attaches_to_reading(self):
        rid, reading_id = self._record_with_reading()
        resp = self._upload(rid, reading_id)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["original_name"], "photo.jpg")
        self.assertTrue(resp.data["url"])
        # surfaced under the reading in the record detail
        detail = self.client.get(reverse("online-monitoring-detail", args=[rid]))
        self.assertEqual(len(detail.data["readings"][0]["attachments"]), 1)

    def test_upload_pdf_allowed(self):
        rid, reading_id = self._record_with_reading()
        resp = self._upload(rid, reading_id, name="report.pdf",
                            ctype="application/pdf", body=b"%PDF-1.4 fake")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_reject_disallowed_type(self):
        rid, reading_id = self._record_with_reading()
        resp = self._upload(rid, reading_id, name="x.exe",
                            ctype="application/x-msdownload", body=b"MZ")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_attachment(self):
        rid, reading_id = self._record_with_reading()
        att_id = self._upload(rid, reading_id).data["id"]
        resp = self.client.delete(reverse(
            "online-monitoring-reading-attachment-detail", args=[rid, reading_id, att_id]))
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        detail = self.client.get(reverse("online-monitoring-detail", args=[rid]))
        self.assertEqual(len(detail.data["readings"][0]["attachments"]), 0)

    def test_upload_blocked_on_non_draft(self):
        rid, reading_id = self._record_with_reading()
        self.client.post(reverse("online-monitoring-submit", args=[rid]))  # → SUBMITTED
        resp = self._upload(rid, reading_id)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class OnlineMonitoringRunsTests(APITestCase):
    """The SKU picker source: currently-running production runs for a line."""

    def setUp(self):
        self.company = Company.objects.create(code="RUN_CO", name="Run Co")
        self.line_a = ProductionLine.objects.create(company=self.company, name="Line-A")
        self.line_b = ProductionLine.objects.create(company=self.company, name="Line-B")
        self.client = _client(self.company)

    def test_runs_lists_inprogress_with_item_and_product(self):
        ProductionRun.objects.create(
            company=self.company, run_number=1, date=date.today(),
            line=self.line_a, product="Oil 1L", item_code="FG0000001",
            status=RunStatus.IN_PROGRESS,
        )
        ProductionRun.objects.create(  # finished → excluded
            company=self.company, run_number=2, date=date.today(),
            line=self.line_a, product="Oil 5L", item_code="FG0000002",
            status=RunStatus.COMPLETED,
        )
        resp = self.client.get(reverse("online-monitoring-runs"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["item_code"], "FG0000001")
        self.assertEqual(resp.data[0]["product"], "Oil 1L")

    def test_runs_filtered_by_line(self):
        ProductionRun.objects.create(
            company=self.company, run_number=1, date=date.today(),
            line=self.line_a, product="A", item_code="FGA", status=RunStatus.IN_PROGRESS,
        )
        ProductionRun.objects.create(
            company=self.company, run_number=2, date=date.today(),
            line=self.line_b, product="B", item_code="FGB", status=RunStatus.IN_PROGRESS,
        )
        resp = self.client.get(reverse("online-monitoring-runs"), {"line": self.line_a.id})
        self.assertEqual([r["item_code"] for r in resp.data], ["FGA"])
