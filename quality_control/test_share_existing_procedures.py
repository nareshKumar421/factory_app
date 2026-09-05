"""The 0059 migration shares previously company-scoped QA procedures."""

import importlib

from django.apps import apps as global_apps
from django.db import connection
from django.test import TestCase

from company.models import Company
from quality_control.models import QCDocumentFile

migration = importlib.import_module(
    "quality_control.migrations.0059_share_existing_qa_procedures"
)


class ShareExistingProceduresTests(TestCase):
    class _Editor:
        connection = connection

    def _run(self):
        migration.forwards(global_apps, self._Editor())

    def setUp(self):
        self.oil = Company.objects.create(code="OIL", name="Oil")
        self.mart = Company.objects.create(code="MART", name="Mart")

    def _doc(self, company, code, active=True):
        return QCDocumentFile.objects.create(
            company=company,
            document_code=code,
            title=f"DOC {code}",
            file="qc_document_files/x.pdf",
            is_active=active,
        )

    def test_a_scoped_document_becomes_shared(self):
        doc = self._doc(self.oil, "QA-TST-INH-14-02-10")
        self._run()
        doc.refresh_from_db()
        self.assertIsNone(doc.company)

    def test_documents_from_several_companies_are_all_shared(self):
        a = self._doc(self.oil, "QA-1")
        b = self._doc(self.mart, "QA-2")
        self._run()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(a.company)
        self.assertIsNone(b.company)

    def test_a_retired_document_is_left_scoped(self):
        retired = self._doc(self.oil, "QA-OLD", active=False)
        self._run()
        retired.refresh_from_db()
        self.assertEqual(retired.company, self.oil)

    def test_a_code_already_held_by_a_shared_document_is_skipped(self):
        self._doc(None, "QA-CLASH")
        scoped = self._doc(self.mart, "QA-CLASH")
        self._run()
        scoped.refresh_from_db()
        # Left alone rather than breaking shared-code uniqueness.
        self.assertEqual(scoped.company, self.mart)

    def test_a_document_without_a_code_is_still_shared(self):
        doc = self._doc(self.oil, "")
        self._run()
        doc.refresh_from_db()
        self.assertIsNone(doc.company)

    def test_running_twice_changes_nothing(self):
        self._doc(self.oil, "QA-1")
        self._run()
        self._run()
        self.assertEqual(
            QCDocumentFile.objects.filter(company__isnull=True).count(), 1
        )
