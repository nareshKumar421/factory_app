"""The 0055 migration folds per-company form copies into one shared form."""

import importlib
from datetime import date

from django.apps import apps as global_apps
from django.db import connection
from django.test import TestCase

from company.models import Company
from quality_control.models import (
    QCRecord,
    RecordTemplate,
    RecordTemplateParameter,
    RecordTemplateSection,
    RecordTimeSlot,
    RecordValue,
)

migration = importlib.import_module(
    "quality_control.migrations.0055_consolidate_shared_record_forms"
)


class ConsolidateSharedFormsTests(TestCase):
    # The migration only reads `schema_editor.connection.alias`; a real SQLite
    # schema editor cannot be opened inside a TestCase transaction.
    class _Editor:
        connection = connection

    def _run(self):
        migration.forwards(global_apps, self._Editor())

    def setUp(self):
        self.oil = Company.objects.create(code="OIL", name="Oil")
        self.mart = Company.objects.create(code="MART", name="Mart")
        self.bev = Company.objects.create(code="BEV", name="Bev")

    def _form(self, company, code="NMW-DAILY-WATER"):
        template = RecordTemplate.objects.create(
            company=company, document_code=code, title="NMW DAILY WATER"
        )
        section = RecordTemplateSection.objects.create(
            template=template, sequence=0, title="Borewell Water"
        )
        RecordTemplateParameter.objects.create(
            section=section, sequence=0, sr_no="4", name="pH"
        )
        return template

    def test_the_oldest_copy_becomes_the_shared_form(self):
        first = self._form(self.oil)
        self._form(self.mart)
        self._form(self.bev)

        self._run()

        first.refresh_from_db()
        self.assertIsNone(first.company)
        self.assertEqual(RecordTemplate.objects.count(), 1)

    def test_filled_sheets_are_repointed_onto_the_shared_form(self):
        keeper = self._form(self.oil)
        duplicate = self._form(self.bev)
        sheet = QCRecord.objects.create(
            company=self.bev, template=duplicate, record_date=date(2026, 9, 4)
        )

        self._run()

        sheet.refresh_from_db()
        self.assertEqual(sheet.template_id, keeper.id)
        self.assertEqual(sheet.company, self.bev)
        self.assertFalse(RecordTemplate.objects.filter(id=duplicate.id).exists())

    def test_sheets_from_two_plants_on_one_date_both_survive_the_merge(self):
        keeper = self._form(self.oil)
        duplicate = self._form(self.bev)
        QCRecord.objects.create(
            company=self.oil, template=keeper, record_date=date(2026, 9, 4)
        )
        QCRecord.objects.create(
            company=self.bev, template=duplicate, record_date=date(2026, 9, 4)
        )

        self._run()

        self.assertEqual(QCRecord.objects.count(), 2)
        self.assertEqual(QCRecord.objects.filter(template=keeper).count(), 2)

    def test_a_duplicate_holding_captured_readings_is_left_alone(self):
        """Never destroy a filled sheet's data to tidy up a form."""
        self._form(self.oil)
        duplicate = self._form(self.bev)
        sheet = QCRecord.objects.create(
            company=self.bev, template=duplicate, record_date=date(2026, 9, 4)
        )
        slot = RecordTimeSlot.objects.create(
            record=sheet, sequence=0, slot_time="08:10"
        )
        parameter = RecordTemplateParameter.objects.get(
            section__template=duplicate
        )
        RecordValue.objects.create(
            record=sheet, time_slot=slot, parameter=parameter, value="7.63"
        )

        self._run()

        self.assertTrue(RecordTemplate.objects.filter(id=duplicate.id).exists())
        self.assertEqual(RecordValue.objects.count(), 1)

    def test_forms_with_different_codes_are_not_merged(self):
        self._form(self.oil, code="NMW-DAILY-WATER")
        self._form(self.mart, code="QA-FRM-OTHER-01")

        self._run()

        self.assertEqual(RecordTemplate.objects.count(), 2)
        self.assertEqual(RecordTemplate.objects.filter(company__isnull=True).count(), 2)

    def test_running_twice_changes_nothing(self):
        self._form(self.oil)
        self._form(self.mart)
        self._run()
        self._run()
        self.assertEqual(RecordTemplate.objects.count(), 1)
        self.assertIsNone(RecordTemplate.objects.get().company)

    def test_a_retired_copy_is_not_touched(self):
        self._form(self.oil)
        retired = self._form(self.bev)
        retired.is_active = False
        retired.save(update_fields=["is_active"])

        self._run()

        self.assertTrue(RecordTemplate.objects.filter(id=retired.id).exists())
        self.assertEqual(retired.company, self.bev)
