"""The papers behind a project: quotations, drawings, the sanction letter.

Distinct from a daily log's photos, which belong to one day. These are uploadable
at any status — the sanction letter arrives after approval and the completion
certificate after the work — so they are deliberately not gated the way the
daily log is.
"""

import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status

from .base import ConstructionTestCase

_TEMP_MEDIA = tempfile.mkdtemp(prefix="construction-test-attachments-")

PDF = b"%PDF-1.4 fake quotation"


@override_settings(MEDIA_ROOT=_TEMP_MEDIA)
class ProjectAttachmentTests(ConstructionTestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TEMP_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def _upload(self, project, name="quote.pdf", title="Verma quotation", kind=None):
        payload = {
            "file": SimpleUploadedFile(name, PDF, content_type="application/pdf"),
            "title": title,
        }
        if kind is not None:
            payload["kind"] = kind
        return self.client.post(
            f"/api/v1/construction/projects/{project.id}/attachments/",
            payload,
            format="multipart",
            **self.headers,
        )

    def test_upload_and_read_back(self):
        project = self.make_project(approved=False)
        response = self._upload(project)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["title"], "Verma quotation")
        self.assertTrue(
            response.data["file"].startswith("http://"),
            "attachment url must be absolute, like every other file in this app",
        )
        self.assertTrue(response.data["filename"].endswith(".pdf"))

        listed = self.get(f"projects/{project.id}/attachments/").data
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["uploaded_by_name"], self.user.full_name)

    def test_a_draft_can_carry_papers(self):
        """The quotation exists before the project is approved — that is what it
        was costed from."""
        project = self.make_project(approved=False)
        self.assertEqual(self._upload(project).status_code, status.HTTP_201_CREATED)

    def test_a_finished_project_can_still_receive_the_completion_certificate(self):
        project = self.make_project()
        self.post(f"projects/{project.id}/complete/")
        self.assertEqual(
            self._upload(project, "completion.pdf", "Completion certificate").status_code,
            status.HTTP_201_CREATED,
        )

    def test_a_title_is_optional(self):
        project = self.make_project()
        response = self.client.post(
            f"/api/v1/construction/projects/{project.id}/attachments/",
            {"file": SimpleUploadedFile("drawing.pdf", PDF)},
            format="multipart",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["title"], "")

    def test_removal_is_soft_and_hides_it_from_the_list(self):
        project = self.make_project()
        created = self._upload(project).data
        response = self.delete(f"attachments/{created['id']}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.get(f"projects/{project.id}/attachments/").data, [])

    def test_uploading_needs_the_edit_permission(self):
        project = self.make_project()
        viewer = self.make_user("att@example.com", "CON950", ["can_view_project"])
        self.client.force_authenticate(viewer)
        response = self.client.post(
            f"/api/v1/construction/projects/{project.id}/attachments/",
            {"file": SimpleUploadedFile("x.pdf", PDF)},
            format="multipart",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_another_companys_project_is_not_reachable(self):
        from company.models import Company

        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        theirs = self.make_project(company=other)
        self.assertEqual(
            self._upload(theirs).status_code, status.HTTP_404_NOT_FOUND
        )


class AttachmentKindTests(ConstructionTestCase):
    """The map is shown on its own, so the file has to say it is one."""

    def _upload(self, project, name="quote.pdf", title="", kind=None):
        payload = {
            "file": SimpleUploadedFile(name, PDF, content_type="application/pdf"),
            "title": title,
        }
        if kind is not None:
            payload["kind"] = kind
        return self.client.post(
            f"/api/v1/construction/projects/{project.id}/attachments/",
            payload,
            format="multipart",
            **self.headers,
        )

    def test_a_file_is_a_document_unless_it_says_otherwise(self):
        project = self.make_project()
        response = self._upload(project)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["kind"], "DOCUMENT")
        self.assertEqual(response.data["kind_display"], "Document")

    def test_a_map_is_recorded_as_one(self):
        project = self.make_project()
        response = self._upload(project, name="site.pdf", kind="MAP")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["kind"], "MAP")
        self.assertEqual(response.data["kind_display"], "Site map")

    def test_maps_sort_ahead_of_the_paperwork(self):
        """The list is read top-down and the map is what people look for."""
        project = self.make_project()
        self._upload(project, name="quote.pdf", title="Quotation")
        self._upload(project, name="site.pdf", title="Layout", kind="MAP")
        self._upload(project, name="letter.pdf", title="Sanction letter")

        listed = self.get(f"projects/{project.id}/attachments/").data
        self.assertEqual([row["kind"] for row in listed], ["MAP", "DOCUMENT", "DOCUMENT"])
        self.assertEqual(listed[0]["title"], "Layout")

    def test_a_project_may_hold_more_than_one_map(self):
        """A site plan and a floor layout are both maps. Nothing should stop
        the second one being uploaded."""
        project = self.make_project()
        self._upload(project, name="site.pdf", title="Site plan", kind="MAP")
        second = self._upload(project, name="floor.pdf", title="Floor layout", kind="MAP")
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        listed = self.get(f"projects/{project.id}/attachments/").data
        self.assertEqual(sum(1 for row in listed if row["kind"] == "MAP"), 2)

    def test_a_kind_that_is_not_one_is_refused(self):
        project = self.make_project()
        response = self._upload(project, kind="BLUEPRINT")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
