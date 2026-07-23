"""
End-to-end integration tests for the document-numbering SYSTEM.

Unlike ``tests.py`` (which unit-tests the numbering service in isolation), these
exercise the real write paths GATE / QC / GRPO use when a PDF is uploaded, and
assert the assigned code / revision / date are persisted and serialized.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

from company.models import Company
from document_control import services
from document_control.models import DocumentCode
from driver_management.models import Driver, VehicleEntry
from vehicle_management.models import Vehicle, VehicleType

User = get_user_model()

PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type /Catalog>>endobj\n%%EOF"


def _pdf(name="doc.pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


class GateUploadIntegrationTests(TestCase):
    """The real GATE upload path (GateAttachmentSerializer.create)."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create_user(
            email="gate@ji.test", password="x", full_name="Gate Op", employee_code="G1"
        )
        vt = VehicleType.objects.create(name="TRUCK-DN")
        vehicle = Vehicle.objects.create(vehicle_number="DL01DN0001", vehicle_type=vt)
        driver = Driver.objects.create(
            name="D", mobile_no="9111111111", license_no="DL-DN-1"
        )
        self.entry = VehicleEntry.objects.create(
            entry_no="VE-DN-1", company=self.company, vehicle=vehicle, driver=driver,
            entry_type="RAW_MATERIAL", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )

    def _upload(self, filename="gate.pdf"):
        from gate_core.serializers import GateAttachmentSerializer

        request = RequestFactory().post("/")  # real request → has build_absolute_uri
        request.user = self.user
        ser = GateAttachmentSerializer(
            data={"file": _pdf(filename)}, context={"request": request}
        )
        self.assertTrue(ser.is_valid(), ser.errors)
        return ser.save(gate_entry=self.entry), ser

    def test_upload_assigns_code_revision_date(self):
        att, ser = self._upload()
        # Code + identity persisted against the file.
        self.assertEqual(att.document_code.code, "WH-FRM-08-05-00-01")
        self.assertEqual(att.document_code.revision_number, 0)
        self.assertEqual(att.document_code.issue_date, date.today())
        self.assertEqual(att.document_code.module, "GATE")
        # Serializer surfaces the identity for the UI.
        self.assertEqual(ser.data["document_code"], "WH-FRM-08-05-00-01")
        self.assertEqual(ser.data["document_revision"], "00")
        self.assertEqual(ser.data["document_issue_date"], date.today().strftime("%d-%m-%Y"))

    def test_uploads_increment_sequentially(self):
        a1, _ = self._upload("a.pdf")
        a2, _ = self._upload("b.pdf")
        a3, _ = self._upload("c.pdf")
        self.assertEqual(
            [a1.document_code.code, a2.document_code.code, a3.document_code.code],
            ["WH-FRM-08-05-00-01", "WH-FRM-08-05-00-02", "WH-FRM-08-05-00-03"],
        )

    def test_source_reference_records_filename(self):
        att, _ = self._upload("coa-scan.pdf")
        self.assertEqual(att.document_code.source_reference, "coa-scan.pdf")


class ModelGuardIntegrationTests(TestCase):
    """A PDF cannot be persisted without a valid, unique code."""

    def test_gate_attachment_without_code_is_refused(self):
        from gate_core.models import GateAttachment

        with self.assertRaises(ValidationError):
            GateAttachment(gate_entry_id=1, file="x.pdf").save()

    def test_grpo_attachment_without_code_is_refused(self):
        from grpo.models import GRPOAttachment

        with self.assertRaises(ValidationError):
            GRPOAttachment(grpo_posting_id=1, original_filename="x.pdf").save()


class CrossModuleIntegrationTests(TestCase):
    """Each module numbers in its own SECTION+DOCTYPE+clause group."""

    def test_three_modules_are_independent(self):
        gate = services.allocate_for_module("GATE")
        qc = services.allocate_for_module("QC")
        grpo = services.allocate_for_module("GRPO")
        self.assertEqual(gate.code, "WH-FRM-08-05-00-01")
        self.assertEqual(qc.code, "QA-FRM-08-06-00-01")
        self.assertEqual(grpo.code, "STR-FRM-08-05-00-01")
        # Distinct rows, distinct codes, all unique.
        self.assertEqual(DocumentCode.objects.count(), 3)
        self.assertEqual(len({gate.code, qc.code, grpo.code}), 3)


class SerializerFieldExposureTests(TestCase):
    """Every module's attachment serializer surfaces the controlled-document
    identity (tested on in-memory instances to avoid heavy per-module fixtures)."""

    def _assert_fields(self, serializer_cls, instance, expected_code):
        data = serializer_cls(instance).data
        self.assertEqual(data["document_code"], expected_code)
        self.assertEqual(data["document_revision"], "00")
        self.assertEqual(
            data["document_issue_date"], date.today().strftime("%d-%m-%Y")
        )

    def test_grpo_serializer_exposes_identity(self):
        from grpo.models import GRPOAttachment
        from grpo.serializers import GRPOAttachmentSerializer

        doc = services.allocate_for_module("GRPO")
        att = GRPOAttachment(original_filename="x.pdf", document_code=doc)
        self._assert_fields(GRPOAttachmentSerializer, att, "STR-FRM-08-05-00-01")

    def test_qc_inspection_serializer_exposes_identity(self):
        from quality_control.models import InspectionAttachment
        from quality_control.serializers import InspectionAttachmentSerializer

        doc = services.allocate_for_module("QC")
        att = InspectionAttachment(original_name="coa.pdf", document_code=doc)
        self._assert_fields(InspectionAttachmentSerializer, att, "QA-FRM-08-06-00-01")

    def test_qc_arrival_slip_serializer_exposes_identity(self):
        from quality_control.models import ArrivalSlipAttachment
        from quality_control.serializers import ArrivalSlipAttachmentSerializer

        doc = services.allocate_for_module("QC")
        att = ArrivalSlipAttachment(
            attachment_type="CERTIFICATE_OF_ANALYSIS", document_code=doc
        )
        self._assert_fields(ArrivalSlipAttachmentSerializer, att, "QA-FRM-08-06-00-01")

    def test_legacy_row_without_code_serializes_blank(self):
        from grpo.models import GRPOAttachment
        from grpo.serializers import GRPOAttachmentSerializer

        att = GRPOAttachment(original_filename="legacy.pdf")  # no code (pre-feature)
        data = GRPOAttachmentSerializer(att).data
        self.assertEqual(data["document_code"], "")
        self.assertEqual(data["document_revision"], "")


class RevisionScenarioTests(TestCase):
    """Re-uploading a certificate is a revision: code stays, revision bumps
    (the semantics QC's arrival-slip re-submit relies on)."""

    def test_reupload_bumps_revision_keeps_code(self):
        doc = services.allocate_for_module("QC")
        original = doc.code
        doc.bump_revision(issue_date=date.today())
        doc.refresh_from_db()
        self.assertEqual(doc.code, original)  # code never changes
        self.assertEqual(doc.revision_number, 1)
        self.assertEqual(doc.revision_label, "01")
