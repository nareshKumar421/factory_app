"""Gate attachments can be soft-removed and re-uploaded ("edit" a wrong slip),
never physically deleted, and the full lifecycle is auditable.

Covers the empty-vehicle-in weighment-slip flow: upload, remove (with reason),
list shows only live attachments, ?history=1 shows the audit trail including the
removed file, and a removed attachment no longer satisfies the gatepass guard.
"""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import GateAttachment
from gate_core.views import has_gatepass_attachment
from vehicle_management.models import Transporter, Vehicle


class GateAttachmentSoftDeleteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="slip@example.com",
            password="testpass123",
            full_name="Slip User",
            employee_code="SLP001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01LAC0001", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="D", mobile_no="9000000001", license_no="DL-SLP-1"
        )
        self.entry = VehicleEntry.objects.create(
            entry_no="EVGI-SLIP-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="IN_PROGRESS",
            created_by=self.user,
            updated_by=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}
        self.list_url = f"/api/v1/gate-core/gate-attachments/{self.entry.id}/"

    def _upload(self, name="slip.pdf"):
        upload = SimpleUploadedFile(name, b"%PDF-1.4 test", content_type="application/pdf")
        response = self.client.post(
            self.list_url, {"file": upload}, format="multipart", **self.headers
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_upload_stamps_uploader_and_lists_live_only(self):
        created = self._upload()
        self.assertEqual(created["uploaded_by_name"], "Slip User")
        self.assertTrue(created["is_active"])

        listed = self.client.get(self.list_url, **self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.data), 1)

    def test_remove_is_soft_and_hidden_from_live_list(self):
        created = self._upload()
        attachment_id = created["id"]

        detail_url = f"{self.list_url}{attachment_id}/"
        removed = self.client.delete(
            detail_url, {"remove_reason": "Wrong slip"}, format="json", **self.headers
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(removed.data["is_active"])
        self.assertEqual(removed.data["removed_by_name"], "Slip User")
        self.assertEqual(removed.data["remove_reason"], "Wrong slip")

        # Row is retained on disk/table, not deleted.
        self.assertTrue(GateAttachment.objects.filter(id=attachment_id).exists())

        # Live list is now empty.
        listed = self.client.get(self.list_url, **self.headers)
        self.assertEqual(len(listed.data), 0)

    def test_history_shows_removed_attachment(self):
        removed_id = self._upload("wrong.pdf")["id"]
        self.client.delete(
            f"{self.list_url}{removed_id}/",
            {"remove_reason": "Wrong slip"},
            format="json",
            **self.headers,
        )
        self._upload("correct.pdf")

        history = self.client.get(f"{self.list_url}?history=1", **self.headers)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.data), 2)
        names = {row["file_name"] for row in history.data}
        self.assertEqual(names, {"wrong.pdf", "correct.pdf"})

    def test_removed_attachment_fails_gatepass_guard(self):
        created = self._upload()
        self.assertTrue(has_gatepass_attachment(self.entry))

        self.client.delete(f"{self.list_url}{created['id']}/", format="json", **self.headers)
        # A removed slip must not satisfy "a document was uploaded".
        self.assertFalse(has_gatepass_attachment(self.entry))
