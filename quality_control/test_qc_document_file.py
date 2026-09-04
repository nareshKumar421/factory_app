"""Tests for the QC PDF document library."""

import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from quality_control.models import QCDocumentFile

User = get_user_model()

# A minimal but genuine PDF payload.
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


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


def _pdf(name="argemone.pdf", content=PDF_BYTES, content_type="application/pdf"):
    return SimpleUploadedFile(name, content, content_type=content_type)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="qc-doc-test-"))
class QCDocumentFileTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.client = _client(self.company)

    def _upload(self, client=None, **overrides):
        payload = {
            "document_code": "QA-TST-INH-14-02-10",
            "title": "ARGEMONE OIL ADULTERATION TESTING",
            "revision": "00/15-10-2023",
            "file": _pdf(),
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        return (client or self.client).post(
            reverse("qc-document-file-list-create"), payload, format="multipart"
        )

    def test_upload_stores_the_pdf_and_its_three_fields(self):
        resp = self._upload()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        document = QCDocumentFile.objects.get(id=resp.data["id"])
        self.assertEqual(document.company, self.company)
        self.assertEqual(document.document_code, "QA-TST-INH-14-02-10")
        self.assertEqual(document.title, "ARGEMONE OIL ADULTERATION TESTING")
        self.assertEqual(document.revision, "00/15-10-2023")
        self.assertEqual(document.original_name, "argemone.pdf")
        self.assertEqual(document.file_size, len(PDF_BYTES))
        self.assertTrue(document.file.name.endswith(".pdf"))

    def test_response_carries_an_absolute_url_for_the_viewer(self):
        resp = self._upload()
        self.assertTrue(resp.data["url"].startswith("http"))
        self.assertIn("/media/", resp.data["url"])

    def test_document_code_is_upper_cased_and_trimmed(self):
        resp = self._upload(document_code="  qa-tst-std-14-02-11  ")
        self.assertEqual(resp.data["document_code"], "QA-TST-STD-14-02-11")

    def test_revision_is_optional(self):
        resp = self._upload(revision="")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["revision"], "")

    def test_missing_title_is_a_field_error(self):
        resp = self._upload(title="")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("title", resp.data)

    def test_document_code_is_optional(self):
        resp = self._upload(document_code="")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["document_code"], "")

    def test_many_documents_may_share_a_blank_code(self):
        first = self._upload(document_code="", title="SCAN ONE")
        second = self._upload(document_code="   ", title="SCAN TWO")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)
        self.assertEqual(QCDocumentFile.objects.count(), 2)

    def test_a_real_code_is_still_unique_alongside_blank_ones(self):
        self._upload(document_code="", title="SCAN ONE")
        self._upload()
        resp = self._upload(title="DUPLICATE ATTEMPT")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_code", resp.data)

    def test_a_code_can_be_cleared_after_upload(self):
        did = self._upload().data["id"]
        resp = self.client.put(
            reverse("qc-document-file-detail", args=[did]),
            {"document_code": ""},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["document_code"], "")

    def test_upload_without_a_file_is_rejected(self):
        resp = self._upload(file=None)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("file", resp.data)

    def test_a_non_pdf_is_rejected(self):
        resp = self._upload(file=_pdf("photo.png", b"\x89PNG\r\n", "image/png"))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("file", resp.data)
        self.assertEqual(QCDocumentFile.objects.count(), 0)

    def test_a_pdf_with_a_vague_mime_type_is_accepted_on_its_extension(self):
        # Some browsers send application/octet-stream for a pasted file.
        resp = self._upload(
            file=_pdf("scan.pdf", PDF_BYTES, "application/octet-stream")
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_duplicate_document_code_is_a_field_error(self):
        self._upload()
        resp = self._upload()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_code", resp.data)
        self.assertEqual(QCDocumentFile.objects.count(), 1)

    def test_the_same_code_is_allowed_for_another_company(self):
        self._upload()
        other = Company.objects.create(code="OTHER_CO", name="Other Co")
        resp = self._upload(client=_client(other))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(QCDocumentFile.objects.count(), 2)

    def test_list_and_search(self):
        self._upload()
        self._upload(
            document_code="QA-TST-STD-14-02-11", title="PEROXIDE VALUE TESTING"
        )
        url = reverse("qc-document-file-list-create")
        self.assertEqual(len(self.client.get(url).data), 2)
        self.assertEqual(len(self.client.get(url, {"search": "argemone"}).data), 1)
        self.assertEqual(len(self.client.get(url, {"search": "QA-TST-STD"}).data), 1)

    def test_edit_updates_the_three_fields(self):
        did = self._upload().data["id"]
        resp = self.client.put(
            reverse("qc-document-file-detail", args=[did]),
            {"title": "ARGEMONE OIL TESTING", "revision": "01/01-01-2026"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["title"], "ARGEMONE OIL TESTING")
        self.assertEqual(resp.data["revision"], "01/01-01-2026")

    def test_edit_cannot_take_another_documents_code(self):
        self._upload()
        second = self._upload(
            document_code="QA-TST-STD-14-02-11", title="PEROXIDE VALUE TESTING"
        ).data["id"]
        resp = self.client.put(
            reverse("qc-document-file-detail", args=[second]),
            {"document_code": "QA-TST-INH-14-02-10"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_code", resp.data)

    def test_delete_is_a_soft_retire(self):
        did = self._upload().data["id"]
        resp = self.client.delete(reverse("qc-document-file-detail", args=[did]))
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(QCDocumentFile.objects.filter(id=did).exists())
        self.assertFalse(QCDocumentFile.objects.get(id=did).is_active)
        self.assertEqual(
            len(self.client.get(reverse("qc-document-file-list-create")).data), 0
        )

    def test_another_company_cannot_read_the_document(self):
        did = self._upload().data["id"]
        other = Company.objects.create(code="OTHER_CO", name="Other Co")
        resp = _client(other).get(reverse("qc-document-file-detail", args=[did]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_viewer_can_read_but_not_upload(self):
        self._upload()
        viewer = _client(self.company, perms=["can_view_document_files"])
        self.assertEqual(
            viewer.get(reverse("qc-document-file-list-create")).status_code,
            status.HTTP_200_OK,
        )
        resp = self._upload(client=viewer, document_code="QA-NEW-01")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_without_view_permission_is_blocked(self):
        nobody = _client(self.company, perms=["can_manage_material_types"])
        resp = nobody.get(reverse("qc-document-file-list-create"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_download_streams_the_pdf_inline(self):
        did = self._upload().data["id"]
        resp = self.client.get(reverse("qc-document-file-download", args=[did]))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("inline", resp["Content-Disposition"])
        self.assertIn("argemone.pdf", resp["Content-Disposition"])
        self.assertEqual(b"".join(resp.streaming_content), PDF_BYTES)

    def test_download_requires_the_view_permission(self):
        did = self._upload().data["id"]
        nobody = _client(self.company, perms=["can_manage_material_types"])
        resp = nobody.get(reverse("qc-document-file-download", args=[did]))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_download_is_scoped_to_the_company(self):
        did = self._upload().data["id"]
        other = Company.objects.create(code="OTHER_CO", name="Other Co")
        resp = _client(other).get(reverse("qc-document-file-download", args=[did]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_payload_exposes_the_authenticated_download_path(self):
        resp = self._upload()
        self.assertEqual(
            resp.data["download_url"],
            f"/quality-control/document-files/{resp.data['id']}/download/",
        )

    def test_a_retired_document_cannot_be_downloaded(self):
        did = self._upload().data["id"]
        self.client.delete(reverse("qc-document-file-detail", args=[did]))
        resp = self.client.get(reverse("qc-document-file-download", args=[did]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_defaults_to_inhouse_when_no_type_is_sent(self):
        resp = self._upload()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["procedure_type"], "INHOUSE")
        self.assertEqual(
            resp.data["procedure_type_label"], "In-house Testing Procedure"
        )

    def test_standard_type_is_stored(self):
        resp = self._upload(
            document_code="QA-TST-STD-14-02-11", procedure_type="STANDARD"
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["procedure_type"], "STANDARD")
        self.assertEqual(
            QCDocumentFile.objects.get(id=resp.data["id"]).procedure_type, "STANDARD"
        )

    def test_type_is_accepted_lower_case(self):
        resp = self._upload(procedure_type="standard")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["procedure_type"], "STANDARD")

    def test_an_unknown_type_is_rejected(self):
        resp = self._upload(procedure_type="EXTERNAL")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("procedure_type", resp.data)
        self.assertEqual(QCDocumentFile.objects.count(), 0)

    def test_list_filters_by_type(self):
        self._upload(procedure_type="INHOUSE")
        self._upload(
            document_code="QA-TST-STD-14-02-11",
            title="PEROXIDE VALUE TESTING",
            procedure_type="STANDARD",
        )
        url = reverse("qc-document-file-list-create")
        self.assertEqual(len(self.client.get(url).data), 2)
        self.assertEqual(
            len(self.client.get(url, {"procedure_type": "INHOUSE"}).data), 1
        )
        self.assertEqual(
            len(self.client.get(url, {"procedure_type": "STANDARD"}).data), 1
        )

    def test_an_unknown_filter_value_is_ignored_rather_than_erroring(self):
        self._upload()
        resp = self.client.get(
            reverse("qc-document-file-list-create"), {"procedure_type": "NONSENSE"}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_type_can_be_corrected_after_upload(self):
        did = self._upload().data["id"]
        resp = self.client.put(
            reverse("qc-document-file-detail", args=[did]),
            {"procedure_type": "STANDARD"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["procedure_type"], "STANDARD")

    def test_editing_to_an_unknown_type_is_rejected(self):
        did = self._upload().data["id"]
        resp = self.client.put(
            reverse("qc-document-file-detail", args=[did]),
            {"procedure_type": "EXTERNAL"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("procedure_type", resp.data)
        self.assertEqual(
            QCDocumentFile.objects.get(id=did).procedure_type, "INHOUSE"
        )

    def test_a_code_can_be_reused_after_the_document_is_retired(self):
        """Retiring frees the code.

        Delete is a soft retire, so the row stays for traceability. The unique
        constraint therefore has to ignore retired rows, or the code is locked
        away forever and QA cannot re-file the same document.
        """
        first = self._upload().data["id"]
        self.client.delete(reverse("qc-document-file-detail", args=[first]))

        resp = self._upload(title="ARGEMONE OIL ADULTERATION TESTING REV 2")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertNotEqual(resp.data["id"], first)

        # The retired row is still on record, just inactive.
        self.assertFalse(QCDocumentFile.objects.get(id=first).is_active)
        self.assertEqual(QCDocumentFile.objects.count(), 2)
        self.assertEqual(
            len(self.client.get(reverse("qc-document-file-list-create")).data), 1
        )

    def test_a_live_document_still_blocks_the_same_code(self):
        self._upload()
        resp = self._upload(title="SECOND ATTEMPT")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document_code", resp.data)
