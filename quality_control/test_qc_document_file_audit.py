"""Tests for the QA Procedures audit log."""

import csv
import io
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from quality_control.models import QCDocumentFile, QCDocumentFileAuditLog

User = get_user_model()

PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

VIEW = "can_view_document_files"
MANAGE = "can_manage_document_files"
AUDIT = "can_view_document_file_audit"


def _user(name=None):
    n = User.objects.count()
    return User.objects.create_user(
        email=f"u{n}@t.com",
        password="x",
        full_name=name or f"User {n}",
        employee_code=f"E{n}",
    )


def _client(company, perms="all", user=None):
    user = user or _user()
    if not UserCompany.objects.filter(user=user, company=company).exists():
        role = UserRole.objects.create(name=f"R{UserRole.objects.count()}")
        UserCompany.objects.create(user=user, company=company, role=role, is_active=True)
    qs = Permission.objects.filter(content_type__app_label="quality_control")
    if perms != "all":
        qs = qs.filter(codename__in=perms)
    user.user_permissions.set(qs)
    user = User.objects.get(pk=user.pk)  # drop the cached permission set
    client = APIClient()
    client.force_authenticate(user=user)
    client.credentials(HTTP_COMPANY_CODE=company.code)
    client.audit_user = user
    return client


def _pdf(name="argemone.pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="qc-audit-test-"))
class QCDocumentFileAuditTrailTests(APITestCase):
    """What gets written when the library changes."""

    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.client = _client(self.company)

    def _upload(self, client=None, **overrides):
        payload = {
            "document_code": "QA-TST-INH-14-02-10",
            "title": "ARGEMONE OIL ADULTERATION TESTING",
            "revision": "00/15-10-2023",
            "procedure_type": "INHOUSE",
            "file": _pdf(),
        }
        payload.update(overrides)
        return (client or self.client).post(
            reverse("qc-document-file-list-create"), payload, format="multipart"
        )

    def test_an_upload_writes_one_row_naming_who_filed_it(self):
        resp = self._upload()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        row = QCDocumentFileAuditLog.objects.get()
        self.assertEqual(row.action, "UPLOADED")
        self.assertEqual(row.document_id, resp.data["id"])
        self.assertEqual(row.document_code, "QA-TST-INH-14-02-10")
        self.assertEqual(row.title, "ARGEMONE OIL ADULTERATION TESTING")
        self.assertEqual(row.user, self.client.audit_user)
        self.assertEqual(row.company, self.company)
        # The filed values, written in the same old/new shape as an edit.
        self.assertIsNone(row.changes["title"]["old"])
        self.assertEqual(row.changes["title"]["new"], "ARGEMONE OIL ADULTERATION TESTING")
        self.assertEqual(row.changes["file"]["new"], "argemone.pdf")

    def test_an_edit_records_only_the_fields_that_moved(self):
        document_id = self._upload().data["id"]
        QCDocumentFileAuditLog.objects.all().delete()

        resp = self.client.put(
            reverse("qc-document-file-detail", args=[document_id]),
            {"title": "ARGEMONE OIL ADULTERATION TESTING -11", "revision": "00/15-10-2023"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        row = QCDocumentFileAuditLog.objects.get()
        self.assertEqual(row.action, "EDITED")
        self.assertEqual(
            row.changes,
            {
                "title": {
                    "old": "ARGEMONE OIL ADULTERATION TESTING",
                    "new": "ARGEMONE OIL ADULTERATION TESTING -11",
                }
            },
        )
        # Revision was re-sent unchanged, so it is not part of the diff.
        self.assertNotIn("revision", row.changes)

    def test_a_put_that_changes_nothing_is_not_an_event(self):
        document_id = self._upload().data["id"]
        QCDocumentFileAuditLog.objects.all().delete()

        self.client.put(
            reverse("qc-document-file-detail", args=[document_id]),
            {"title": "ARGEMONE OIL ADULTERATION TESTING"},
            format="json",
        )
        self.assertEqual(QCDocumentFileAuditLog.objects.count(), 0)

    def test_retiring_a_document_writes_a_row(self):
        document_id = self._upload().data["id"]
        QCDocumentFileAuditLog.objects.all().delete()

        resp = self.client.delete(reverse("qc-document-file-detail", args=[document_id]))
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        row = QCDocumentFileAuditLog.objects.get()
        self.assertEqual(row.action, "RETIRED")
        self.assertEqual(row.document_id, document_id)
        self.assertEqual(row.changes, {"is_active": {"old": True, "new": False}})

    def test_the_snapshot_survives_the_document_being_erased(self):
        document_id = self._upload().data["id"]
        QCDocumentFile.objects.filter(id=document_id).delete()

        row = QCDocumentFileAuditLog.objects.get()
        self.assertIsNone(row.document_id)
        # The row still says which document it was about.
        self.assertEqual(row.document_code, "QA-TST-INH-14-02-10")
        self.assertEqual(row.title, "ARGEMONE OIL ADULTERATION TESTING")

    def test_the_code_is_snapshotted_as_it_was_at_the_time(self):
        document_id = self._upload().data["id"]
        self.client.put(
            reverse("qc-document-file-detail", args=[document_id]),
            {"document_code": "QA-TST-INH-14-02-99"},
            format="json",
        )
        uploaded, edited = QCDocumentFileAuditLog.objects.order_by("id")
        # The upload row keeps the old code even though the document now
        # carries a different one.
        self.assertEqual(uploaded.document_code, "QA-TST-INH-14-02-10")
        self.assertEqual(edited.document_code, "QA-TST-INH-14-02-99")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="qc-audit-api-test-"))
class QCDocumentFileAuditAPITests(APITestCase):
    """Reading the trail back."""

    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.qa = _client(self.company, perms=[VIEW, MANAGE])
        self.manager = _client(self.company, perms=[VIEW, AUDIT])

        first = self.qa.post(
            reverse("qc-document-file-list-create"),
            {
                "document_code": "QA-TST-INH-14-02-10",
                "title": "ACID VALUE TESTING",
                "revision": "01",
                "procedure_type": "INHOUSE",
                "file": _pdf(),
            },
            format="multipart",
        )
        self.first_id = first.data["id"]

        second = self.qa.post(
            reverse("qc-document-file-list-create"),
            {
                "document_code": "QA-LAB-SOP-14-02-05",
                "title": "HOT AIR OVEN",
                "revision": "00",
                "procedure_type": "STANDARD",
                "file": _pdf("oven.pdf"),
            },
            format="multipart",
        )
        self.second_id = second.data["id"]

        self.qa.put(
            reverse("qc-document-file-detail", args=[self.first_id]),
            {"revision": "02"},
            format="json",
        )

    def test_uploading_does_not_come_with_the_right_to_read_the_log(self):
        resp = self.qa.get(reverse("qc-document-file-audit-log"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_manager_sees_every_event_newest_first(self):
        resp = self.manager.get(reverse("qc-document-file-audit-log"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["count"], 3)
        self.assertEqual(
            [row["action"] for row in resp.data["results"]],
            ["EDITED", "UPLOADED", "UPLOADED"],
        )
        edited = resp.data["results"][0]
        self.assertEqual(edited["changes_summary"], "Revision: 01 → 02")
        self.assertEqual(edited["user_name"], self.qa.audit_user.full_name)
        self.assertFalse(edited["document_missing"])

    def test_the_header_counts_break_down_the_filtered_set(self):
        everything = self.manager.get(reverse("qc-document-file-audit-log"))
        self.assertEqual(
            everything.data["action_counts"],
            {"UPLOADED": 2, "EDITED": 1, "RETIRED": 0},
        )

        # The counts follow the filters, not the whole table.
        narrowed = self.manager.get(
            reverse("qc-document-file-audit-log"), {"document": self.second_id}
        )
        self.assertEqual(
            narrowed.data["action_counts"],
            {"UPLOADED": 1, "EDITED": 0, "RETIRED": 0},
        )

    def test_an_upload_summary_reads_as_the_filed_values(self):
        resp = self.manager.get(
            reverse("qc-document-file-audit-log"), {"action": "UPLOADED", "document": self.second_id}
        )
        summary = resp.data["results"][0]["changes_summary"]
        self.assertIn("Type: - → Standard", summary)
        self.assertIn("Title: - → HOT AIR OVEN", summary)

    def test_filtering_by_action_document_user_and_search(self):
        by_action = self.manager.get(
            reverse("qc-document-file-audit-log"), {"action": "EDITED"}
        )
        self.assertEqual(by_action.data["count"], 1)

        by_document = self.manager.get(
            reverse("qc-document-file-audit-log"), {"document": self.second_id}
        )
        self.assertEqual(by_document.data["count"], 1)

        by_user = self.manager.get(
            reverse("qc-document-file-audit-log"), {"user": self.qa.audit_user.id}
        )
        self.assertEqual(by_user.data["count"], 3)

        by_search = self.manager.get(
            reverse("qc-document-file-audit-log"), {"search": "hot air"}
        )
        self.assertEqual(by_search.data["count"], 1)

        # A user who has touched nothing has no rows.
        stranger = _user()
        none = self.manager.get(
            reverse("qc-document-file-audit-log"), {"user": stranger.id}
        )
        self.assertEqual(none.data["count"], 0)

    def test_a_date_range_narrows_and_an_unparseable_date_is_ignored(self):
        today = self.manager.get(
            reverse("qc-document-file-audit-log"),
            {"date_from": "2000-01-01", "date_to": "2100-01-01"},
        )
        self.assertEqual(today.data["count"], 3)

        long_ago = self.manager.get(
            reverse("qc-document-file-audit-log"),
            {"date_to": "2000-01-01"},
        )
        self.assertEqual(long_ago.data["count"], 0)

        # Garbage must not 500 the page — it is simply not a filter.
        junk = self.manager.get(
            reverse("qc-document-file-audit-log"), {"date_from": "not-a-date"}
        )
        self.assertEqual(junk.status_code, status.HTTP_200_OK)
        self.assertEqual(junk.data["count"], 3)

    def test_the_per_document_endpoint_shows_only_that_document(self):
        resp = self.manager.get(
            reverse("qc-document-file-audit-log-detail", args=[self.first_id])
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["count"], 2)
        self.assertEqual({row["document"] for row in resp.data["results"]}, {self.first_id})

    def test_the_per_document_endpoint_needs_the_audit_permission_too(self):
        resp = self.qa.get(
            reverse("qc-document-file-audit-log-detail", args=[self.first_id])
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_retired_document_keeps_its_trail(self):
        self.qa.delete(reverse("qc-document-file-detail", args=[self.first_id]))
        resp = self.manager.get(
            reverse("qc-document-file-audit-log-detail", args=[self.first_id])
        )
        self.assertEqual(resp.data["count"], 3)
        self.assertEqual(resp.data["results"][0]["action"], "RETIRED")

    def test_pagination_reports_the_full_count(self):
        resp = self.manager.get(
            reverse("qc-document-file-audit-log"), {"page_size": 2, "page": 2}
        )
        self.assertEqual(resp.data["count"], 3)
        self.assertEqual(resp.data["page"], 2)
        self.assertEqual(resp.data["total_pages"], 2)
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertFalse(resp.data["next"])
        self.assertTrue(resp.data["previous"])

    def test_the_csv_export_carries_the_same_rows(self):
        resp = self.manager.get(
            reverse("qc-document-file-audit-log"), {"export": "csv"}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("attachment;", resp["Content-Disposition"])

        rows = list(csv.reader(io.StringIO(resp.content.decode())))
        self.assertEqual(rows[0][0], "When")
        self.assertEqual(len(rows), 4)  # header + three events
        self.assertIn("ACID VALUE TESTING", {row[5] for row in rows[1:]})

    def test_the_filter_options_list_the_actors_not_the_directory(self):
        _user()  # someone who has never touched a procedure
        resp = self.manager.get(reverse("qc-document-file-audit-filters"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([u["id"] for u in resp.data["users"]], [self.qa.audit_user.id])
        self.assertEqual(
            sorted(d["document_code"] for d in resp.data["documents"]),
            ["QA-LAB-SOP-14-02-05", "QA-TST-INH-14-02-10"],
        )
        self.assertEqual(
            [a["value"] for a in resp.data["actions"]],
            ["UPLOADED", "EDITED", "RETIRED"],
        )

    def test_the_filter_options_need_the_audit_permission(self):
        resp = self.qa.get(reverse("qc-document-file-audit-filters"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="qc-audit-scope-test-"))
class QCDocumentFileAuditScopeTests(APITestCase):
    """What one company may read of another's trail."""

    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.other = Company.objects.create(code="OTHER_CO", name="Other Co")
        self.manager = _client(self.company, perms=[VIEW, AUDIT])

    def test_a_document_private_to_another_company_stays_hidden(self):
        private = QCDocumentFile.objects.create(
            company=self.other,
            document_code="QA-OTHER-01",
            title="SOMEONE ELSE'S SHEET",
            file=_pdf("other.pdf"),
        )
        shared = QCDocumentFile.objects.create(
            company=None, document_code="QA-SHARED-01", title="SHARED SHEET",
            file=_pdf("shared.pdf"),
        )
        for document in (private, shared):
            QCDocumentFileAuditLog.objects.create(
                document=document,
                document_code=document.document_code,
                title=document.title,
                action="UPLOADED",
            )

        resp = self.manager.get(reverse("qc-document-file-audit-log"))
        self.assertEqual(
            [row["document_code"] for row in resp.data["results"]], ["QA-SHARED-01"]
        )

    def test_a_row_whose_document_was_erased_is_still_readable(self):
        QCDocumentFileAuditLog.objects.create(
            document=None,
            document_code="QA-GONE-01",
            title="DELETED SHEET",
            action="RETIRED",
        )
        resp = self.manager.get(reverse("qc-document-file-audit-log"))
        self.assertEqual(resp.data["count"], 1)
        self.assertTrue(resp.data["results"][0]["document_missing"])
        self.assertEqual(resp.data["results"][0]["document_code"], "QA-GONE-01")
