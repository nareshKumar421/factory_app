"""QA Reports ("production QC" in code): saving an entry, approving it, and the groups."""

import importlib
from datetime import timedelta

from django.apps import apps as global_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from quality_control.models import (
    QCPrintDocument,
    ProductionParameter,
    ProductionParameterType,
    ProductionQCEntry,
    ProductionQCStatus,
)
from quality_control.services import production_qc as service
from quality_control.services.production_qc import ProductionQCError

User = get_user_model()

VIEW = "can_view_production_qc_entries"
FILL = "can_fill_production_qc_entries"
APPROVE = "can_approve_production_qc_entries"
MANAGE = "can_manage_production_qc_parameters"


def _user(company, *codenames):
    n = User.objects.count()
    user = User.objects.create_user(
        email=f"pqc{n}@t.com", password="x", full_name=f"PQC {n}", employee_code=f"PQC{n}"
    )
    role = UserRole.objects.create(name=f"PQCR{UserRole.objects.count()}")
    UserCompany.objects.create(user=user, company=company, role=role, is_active=True)
    user.user_permissions.set(
        Permission.objects.filter(content_type__app_label="quality_control", codename__in=codenames)
    )
    return User.objects.get(pk=user.pk)


def _client(company, *codenames):
    client = APIClient()
    client.force_authenticate(user=_user(company, *codenames))
    client.credentials(HTTP_COMPANY_CODE=company.code)
    return client


class ProductionQCBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Oil")
        self.user = _user(self.company, VIEW, FILL)
        self.type = ProductionParameterType.objects.create(
            company=self.company, code="PET_1L", name="PET 1 L"
        )
        self.weight = ProductionParameter.objects.create(
            parameter_type=self.type, parameter_code="WT", parameter_name="Fill weight",
            standard_value="910±5", value_type="NUMERIC", uom="g", sequence=1,
        )
        self.seal = ProductionParameter.objects.create(
            parameter_type=self.type, parameter_code="SEAL", parameter_name="Seal",
            standard_value="No leak", value_type="BOOLEAN", sequence=2,
        )
        self.note = ProductionParameter.objects.create(
            parameter_type=self.type, parameter_code="NOTE", parameter_name="Coding",
            standard_value="Legible", value_type="TEXT", sequence=3, is_mandatory=False,
        )

    def _readings(self, weight="911", seal="Pass", note=""):
        return {
            self.weight.id: {"result_value": weight},
            self.seal.id: {"result_value": seal},
            self.note.id: {"result_value": note},
        }

    def _create(self, user=None, **kwargs):
        kwargs.setdefault("readings", self._readings())
        return service.create_entry(
            self.company, user or self.user, parameter_type_id=self.type.id, **kwargs
        )


class CreateEntryTests(ProductionQCBase):
    def test_saving_sends_it_for_approval_with_the_spec_snapshotted(self):
        entry = self._create()
        self.assertEqual(entry.status, ProductionQCStatus.PENDING)
        self.assertEqual(entry.parameter_type, self.type)
        weight = entry.results.get(parameter_master=self.weight)
        self.assertEqual(weight.standard_value, "910±5")
        self.assertEqual(weight.parameter_type, "NUMERIC")
        self.assertTrue(weight.is_within_spec)

    def test_any_of_the_companys_documents_can_be_filled(self):
        # A document is not tied to production: there is no line or run to pick.
        self._create()
        other = ProductionParameterType.objects.create(
            company=self.company, code="BACKWASH", name="Backwashing"
        )
        equipment = ProductionParameter.objects.create(
            parameter_type=other, parameter_code="EQUIPMENT", parameter_name="Equipment",
            standard_value="-", value_type="TEXT", sequence=1,
        )
        entry = service.create_entry(
            self.company, self.user, parameter_type_id=other.id,
            readings={equipment.id: {"result_value": "Micron filter"}},
        )
        self.assertEqual(entry.parameter_type, other)

    def test_an_inactive_type_cannot_be_checked(self):
        self.type.is_active = False
        self.type.save()
        with self.assertRaises(ProductionQCError) as caught:
            self._create()
        self.assertEqual(caught.exception.field, "parameter_type_id")

    def test_a_mandatory_parameter_needs_a_value(self):
        with self.assertRaises(ProductionQCError) as caught:
            self._create(readings=self._readings(weight=""))
        self.assertIn("Fill weight", str(caught.exception))
        self.assertFalse(ProductionQCEntry.objects.exists())

    def test_out_of_spec_needs_a_remark(self):
        with self.assertRaises(ProductionQCError) as caught:
            self._create(readings=self._readings(weight="890"))
        self.assertEqual(caught.exception.field, "remarks")
        self.assertFalse(ProductionQCEntry.objects.exists())

        entry = self._create(readings=self._readings(weight="890"), remarks="Filler 3 low")
        self.assertFalse(entry.results.get(parameter_master=self.weight).is_within_spec)

    def test_a_fail_is_out_of_spec(self):
        entry = self._create(readings=self._readings(seal="Fail"), remarks="Leak on head 2")
        self.assertFalse(entry.results.get(parameter_master=self.seal).is_within_spec)

    def test_a_type_without_parameters_cannot_be_used(self):
        empty = ProductionParameterType.objects.create(company=self.company, code="E", name="E")
        with self.assertRaises(ProductionQCError):
            service.create_entry(
                self.company, self.user, parameter_type_id=empty.id,
                readings={},
            )

    def test_a_removed_parameter_is_not_asked_for(self):
        self.note.is_active = False
        self.note.save()
        readings = self._readings()
        readings.pop(self.note.id)
        entry = self._create(readings=readings)
        self.assertEqual(entry.results.count(), 2)


class DecisionTests(ProductionQCBase):
    def setUp(self):
        super().setUp()
        self.lead = _user(self.company, VIEW, APPROVE)
        self.entry = self._create()

    def test_approve(self):
        entry = service.approve_entry(self.entry, self.lead, "ok")
        self.assertEqual(entry.status, ProductionQCStatus.APPROVED)
        self.assertEqual(entry.approved_by, self.lead)

    def test_send_back_needs_a_remark_and_a_correction_resubmits(self):
        with self.assertRaises(ProductionQCError):
            service.send_back_entry(self.entry, self.lead, "  ")
        entry = service.send_back_entry(self.entry, self.lead, "Recheck weight")
        self.assertEqual(entry.status, ProductionQCStatus.SENT_BACK)

        entry = service.update_entry(entry, self.user, readings=self._readings(weight="912"))
        self.assertEqual(entry.status, ProductionQCStatus.PENDING)
        self.assertEqual(entry.results.get(parameter_master=self.weight).result_value, "912")

    def test_an_approved_entry_is_final(self):
        service.approve_entry(self.entry, self.lead)
        with self.assertRaises(ProductionQCError):
            service.update_entry(self.entry, self.user, readings=self._readings())
        with self.assertRaises(ProductionQCError):
            service.send_back_entry(self.entry, self.lead, "late")

    def test_a_correction_keeps_the_spec_it_was_checked_against(self):
        self.weight.standard_value = "920±5"
        self.weight.save()
        entry = service.update_entry(self.entry, self.user, readings=self._readings())
        self.assertEqual(entry.results.get(parameter_master=self.weight).standard_value, "910±5")


class ProductionQCAPITests(ProductionQCBase):
    def _payload(self, **overrides):
        payload = {
            "parameter_type_id": self.type.id,
            "remarks": "",
            "results": [
                {"parameter_id": pid, **data} for pid, data in self._readings().items()
            ],
        }
        payload.update(overrides)
        return payload

    def test_entries_and_types_carry_no_production(self):
        client = _client(self.company, VIEW, FILL)
        resp = client.post(reverse("production-qc-entries"), self._payload(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        for field in ("line_id", "line_name", "run_id", "run_number", "item_code", "product"):
            self.assertNotIn(field, resp.data)

        resp = _client(self.company, MANAGE).get(reverse("production-qc-parameter-types"))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertNotIn("items", resp.data[0])

    def test_entries_are_filtered_by_document(self):
        self._create()
        other = ProductionParameterType.objects.create(
            company=self.company, code="BACKWASH", name="Backwashing"
        )
        resp = _client(self.company, VIEW).get(
            reverse("production-qc-entries"), {"parameter_type_id": other.id}
        )
        self.assertEqual(resp.data, [])

    def test_a_filler_saves_and_cannot_approve(self):
        client = _client(self.company, VIEW, FILL)
        resp = client.post(reverse("production-qc-entries"), self._payload(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["status"], "PENDING")
        self.assertEqual(len(resp.data["results"]), 3)

        resp = client.post(reverse("production-qc-entry-approve", args=[resp.data["id"]]), {})
        self.assertEqual(resp.status_code, 403)

    def test_a_lead_approves_but_cannot_make_an_entry(self):
        entry = self._create()
        lead = _client(self.company, VIEW, APPROVE)
        resp = lead.post(reverse("production-qc-entries"), self._payload(), format="json")
        self.assertEqual(resp.status_code, 403)
        resp = lead.post(reverse("production-qc-entry-approve", args=[entry.id]), {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["status"], "APPROVED")

    def test_validation_errors_come_back_by_field(self):
        resp = _client(self.company, FILL).post(
            reverse("production-qc-entries"),
            self._payload(results=[{"parameter_id": self.weight.id, "result_value": "880"},
                                   {"parameter_id": self.seal.id, "result_value": "Pass"}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("remarks", resp.data)

    def test_waiting_entries_are_listed_whatever_the_date(self):
        entry = self._create()
        ProductionQCEntry.objects.filter(pk=entry.pk).update(
            checked_at=timezone.now() - timedelta(days=30)
        )
        today = timezone.localdate().isoformat()
        client = _client(self.company, VIEW)
        resp = client.get(reverse("production-qc-entries"), {"from_date": today, "to_date": today})
        self.assertEqual([row["id"] for row in resp.data], [entry.id])

        service.approve_entry(entry, self.user)
        resp = client.get(reverse("production-qc-entries"), {"from_date": today, "to_date": today})
        self.assertEqual(resp.data, [])

    def test_counts(self):
        self._create()
        resp = _client(self.company, VIEW).get(reverse("production-qc-entry-counts"))
        self.assertEqual(resp.data, {"pending": 1, "sent_back": 0, "approved": 0})

    def test_another_companys_entries_are_not_visible(self):
        entry = self._create()
        other = Company.objects.create(code="JIVO_BEVERAGES", name="Bev")
        resp = _client(other, VIEW).get(reverse("production-qc-entry-detail", args=[entry.id]))
        self.assertEqual(resp.status_code, 404)

    def test_masters_need_the_manage_permission_to_change(self):
        url = reverse("production-qc-parameter-types")
        self.assertEqual(_client(self.company, FILL).get(url).status_code, 200)
        self.assertEqual(
            _client(self.company, FILL).post(url, {"code": "N", "name": "N"}).status_code, 403
        )
        resp = _client(self.company, MANAGE).post(url, {"code": "new", "name": "New"})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["code"], "NEW")
        self.assertEqual(resp.data["parameter_count"], 0)

    def test_a_type_carries_its_paper_forms_revision(self):
        client = _client(self.company, MANAGE)
        resp = client.post(
            reverse("production-qc-parameter-types"),
            {"code": "oil_online", "name": "Oil Plant On-line Monitoring",
             "revision": "02", "revision_date": "2026-05-22"},
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["revision"], "02")
        self.assertEqual(resp.data["revision_date"], "2026-05-22")
        # Its number lives in Print Documents; none set yet.
        self.assertEqual(resp.data["print_document_id"], "")

        resp = client.patch(
            reverse("production-qc-parameter-type-detail", args=[resp.data["id"]]),
            {"revision": "03"}, format="json",
        )
        self.assertEqual(resp.data["revision"], "03")
        self.assertEqual(resp.data["revision_date"], "2026-05-22")

    def test_parameters_are_added_and_soft_removed(self):
        client = _client(self.company, MANAGE)
        resp = client.post(
            reverse("production-qc-parameters", args=[self.type.id]),
            {"parameter_code": "cap", "parameter_name": "Cap torque", "standard_value": "12-18",
             "value_type": "RANGE", "min_value": "12", "max_value": "18", "uom": "kgf.cm"},
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        resp = client.delete(reverse("production-qc-parameter-detail", args=[resp.data["id"]]))
        self.assertEqual(resp.status_code, 204)
        resp = client.get(reverse("production-qc-parameters", args=[self.type.id]))
        self.assertEqual([p["parameter_code"] for p in resp.data], ["WT", "SEAL", "NOTE"])


class ProductionQCDayTests(ProductionQCBase):
    """The dashboard is one day at a time, like the paper record."""

    def setUp(self):
        super().setUp()
        self.client_ = _client(self.company, VIEW)
        self.today_entry = self._create()
        self.old_entry = self._create()
        ProductionQCEntry.objects.filter(pk=self.old_entry.pk).update(
            checked_at=timezone.now() - timedelta(days=3)
        )
        self.today = timezone.localdate().isoformat()
        self.old_day = (timezone.localdate() - timedelta(days=3)).isoformat()

    def test_a_day_lists_only_that_day_whatever_the_status(self):
        resp = self.client_.get(reverse("production-qc-entries"), {"date": self.today})
        self.assertEqual([row["id"] for row in resp.data], [self.today_entry.id])

        service.approve_entry(self.today_entry, self.user)
        resp = self.client_.get(reverse("production-qc-entries"), {"date": self.today})
        self.assertEqual([row["id"] for row in resp.data], [self.today_entry.id])

        resp = self.client_.get(reverse("production-qc-entries"), {"date": self.old_day})
        self.assertEqual([row["id"] for row in resp.data], [self.old_entry.id])

    def test_a_search_stays_on_the_day(self):
        resp = self.client_.get(
            reverse("production-qc-entries"), {"date": self.today, "search": "PET"}
        )
        self.assertEqual([row["id"] for row in resp.data], [self.today_entry.id])

    def test_a_search_reads_the_entered_values(self):
        # The paper header (product, batch...) is entered as readings.
        tagged = self._create(readings=self._readings(note="Batch L2-0917"))
        resp = self.client_.get(
            reverse("production-qc-entries"), {"date": self.today, "search": "l2-09"}
        )
        self.assertEqual([row["id"] for row in resp.data], [tagged.id])
        # One matching reading is one row, and its out-of-spec count is not inflated.
        self.assertEqual(resp.data[0]["out_of_spec_count"], 0)

    def test_the_sheet_gets_the_readings(self):
        resp = self.client_.get(
            reverse("production-qc-entries"), {"date": self.today, "include": "results"}
        )
        self.assertEqual(len(resp.data[0]["results"]), 3)
        self.assertNotIn("results", self.client_.get(reverse("production-qc-entries")).data[0])

    def test_counts_for_a_day_and_what_waits_on_other_days(self):
        resp = self.client_.get(reverse("production-qc-entry-counts"), {"date": self.today})
        self.assertEqual(resp.data["pending"], 1)
        self.assertEqual(resp.data["approved"], 0)
        self.assertEqual(resp.data["waiting_elsewhere"], 1)
        self.assertEqual(str(resp.data["waiting_elsewhere_first_date"]), self.old_day)

        service.approve_entry(self.old_entry, self.user)
        resp = self.client_.get(reverse("production-qc-entry-counts"), {"date": self.today})
        self.assertEqual(resp.data["waiting_elsewhere"], 0)
        self.assertIsNone(resp.data["waiting_elsewhere_first_date"])

    def test_a_bad_date_is_refused(self):
        for url in ("production-qc-entries", "production-qc-entry-counts"):
            resp = self.client_.get(reverse(url), {"date": "29-09-2026"})
            self.assertEqual(resp.status_code, 400, url)
            self.assertIn("date", resp.data)


class ProductionFormNumberTests(ProductionQCBase):
    """Master Data > Print Documents holds every form's number, production sheets included."""

    def setUp(self):
        super().setUp()
        # Print Documents is kept by whoever manages the QC parameters.
        self.admin = _client(self.company, "can_manage_qc_parameters")

    def _set(self, **data):
        return self.admin.post(reverse("qc-print-document-list-create"), data, format="json")

    def test_the_forms_on_offer_include_each_production_type(self):
        ProductionParameterType.objects.create(
            company=self.company, code="OLD", name="Old", is_active=False
        )
        resp = self.admin.get(reverse("qc-print-document-options"))
        self.assertEqual(resp.status_code, 200)
        labels = [option["label"] for option in resp.data]
        self.assertEqual(
            labels,
            ["Arrival Slip Inspection Print", "Arrival Slip QC Parameters Print", "QA Report — PET 1 L"],
        )
        self.assertEqual(resp.data[2]["production_parameter_type"], self.type.id)

    def test_a_production_sheet_gets_its_number_and_the_type_reports_it(self):
        resp = self._set(document_key="PRODUCTION_QC_SHEET",
                         production_parameter_type=self.type.id, document_id="QA-FRM-14-01-05-02")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["document_key_label"], "QA Report — PET 1 L")

        resp = _client(self.company, VIEW).get(
            reverse("production-qc-parameter-type-detail", args=[self.type.id])
        )
        self.assertEqual(resp.data["print_document_id"], "QA-FRM-14-01-05-02")

    def test_the_type_edits_the_same_number_print_documents_shows(self):
        manage = _client(self.company, MANAGE)
        url = reverse("production-qc-parameter-type-detail", args=[self.type.id])

        resp = manage.patch(url, {"print_document_id": " QA-FRM-14-01-05-02 "}, format="json")
        self.assertEqual(resp.data["print_document_id"], "QA-FRM-14-01-05-02")
        listed = self.admin.get(reverse("qc-print-document-list-create")).data
        self.assertEqual(
            [(row["document_key_label"], row["document_id"]) for row in listed],
            [("QA Report — PET 1 L", "QA-FRM-14-01-05-02")],
        )

        # Changed in Print Documents, the type reads the change.
        self._set(document_key="PRODUCTION_QC_SHEET", production_parameter_type=self.type.id,
                  document_id="QA-FRM-14-01-05-03")
        self.assertEqual(manage.get(url).data["print_document_id"], "QA-FRM-14-01-05-03")

        # Left out of an edit, it stays; blank removes it.
        manage.patch(url, {"name": "PET 1 L Oil"}, format="json")
        self.assertEqual(manage.get(url).data["print_document_id"], "QA-FRM-14-01-05-03")
        manage.patch(url, {"print_document_id": ""}, format="json")
        self.assertEqual(manage.get(url).data["print_document_id"], "")
        self.assertEqual(self.admin.get(reverse("qc-print-document-list-create")).data, [])
        self.assertEqual(QCPrintDocument.objects.filter(production_parameter_type=self.type).count(), 1)

    def test_a_new_type_can_come_with_its_number(self):
        resp = _client(self.company, MANAGE).post(
            reverse("production-qc-parameter-types"),
            {"code": "jar", "name": "Jar", "print_document_id": "QA-FRM-X"}, format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["print_document_id"], "QA-FRM-X")

    def test_one_number_per_form(self):
        self._set(document_key="PRODUCTION_QC_SHEET", production_parameter_type=self.type.id,
                  document_id="A")
        resp = self._set(document_key="PRODUCTION_QC_SHEET", production_parameter_type=self.type.id,
                         document_id="B")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            list(QCPrintDocument.objects.filter(production_parameter_type=self.type)
                 .values_list("document_id", flat=True)),
            ["B"],
        )
        # The arrival-slip reports keep one number each, beside it.
        resp = self._set(document_key="RAW_MATERIAL_INSPECTION", document_id="R")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_a_production_sheet_needs_its_form(self):
        resp = self._set(document_key="PRODUCTION_QC_SHEET", document_id="X")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("production_parameter_type", resp.data)

    def test_an_arrival_report_ignores_a_type_sent_with_it(self):
        resp = self._set(document_key="QC_PARAMETERS", production_parameter_type=self.type.id,
                         document_id="Q")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(resp.data["production_parameter_type"])

    def test_another_companys_form_is_refused(self):
        other = Company.objects.create(code="JIVO_BEVERAGES", name="Bev")
        theirs = ProductionParameterType.objects.create(company=other, code="T", name="T")
        resp = self._set(document_key="PRODUCTION_QC_SHEET", production_parameter_type=theirs.id,
                         document_id="X")
        self.assertEqual(resp.status_code, 400)


class ProductionQCGroupsMigrationTests(TestCase):
    class _Editor:
        connection = connection

    migration = importlib.import_module("quality_control.migrations.0064_production_qc_groups")

    def test_groups_get_their_permissions_and_running_twice_changes_nothing(self):
        self.migration.forwards(global_apps, self._Editor())
        self.migration.forwards(global_apps, self._Editor())
        for name, codenames in self.migration.GROUPS.items():
            granted = set(Group.objects.get(name=name).permissions.values_list("codename", flat=True))
            self.assertEqual(granted, set(codenames), name)

    def test_the_line_qc_group_fills_and_the_lead_approves(self):
        self.migration.forwards(global_apps, self._Editor())
        filler = Group.objects.get(name="Production QC").permissions
        lead = Group.objects.get(name="Production QC Lead").permissions
        self.assertTrue(filler.filter(codename=FILL).exists())
        self.assertFalse(filler.filter(codename=APPROVE).exists())
        self.assertTrue(lead.filter(codename=APPROVE).exists())
        self.assertFalse(lead.filter(codename=FILL).exists())

    def test_reverse_removes_the_new_empty_group_only(self):
        self.migration.forwards(global_apps, self._Editor())
        self.migration.backwards(global_apps, self._Editor())
        self.assertFalse(Group.objects.filter(name="Production QC Lead").exists())
        self.assertFalse(
            Group.objects.get(name="Production QC").permissions.filter(codename=FILL).exists()
        )


class DocumentsRenameMigrationTests(TestCase):
    """0071: the lead's group and the permission labels say Documents now."""

    class _Editor:
        connection = connection

    migration = importlib.import_module(
        "quality_control.migrations.0071_rename_production_qc_to_documents"
    )

    def test_the_lead_group_is_renamed_and_keeps_its_members(self):
        Group.objects.filter(name="QC Documents Lead").delete()
        lead = Group.objects.create(name="Production QC Lead")
        member = _user(Company.objects.create(code="JIVO_BEV", name="Bev"))
        member.groups.add(lead)
        line_qc, _ = Group.objects.get_or_create(name="Production QC")

        self.migration.forwards(global_apps, self._Editor())
        lead.refresh_from_db()
        self.assertEqual(lead.name, "QC Documents Lead")
        self.assertTrue(lead.user_set.filter(pk=member.pk).exists())
        # The line QC staff's own group keeps its name.
        self.assertTrue(Group.objects.filter(pk=line_qc.pk, name="Production QC").exists())
        self.assertEqual(
            Permission.objects.get(codename=VIEW).name, "Can view QC document entries"
        )

        self.migration.backwards(global_apps, self._Editor())
        lead.refresh_from_db()
        self.assertEqual(lead.name, "Production QC Lead")
        self.assertEqual(
            Permission.objects.get(codename=VIEW).name, "Can view production QC entries"
        )


class QAReportsRenameMigrationTests(TestCase):
    """0072: the lead's group and the permission labels say QA Reports now."""

    class _Editor:
        connection = connection

    migration = importlib.import_module(
        "quality_control.migrations.0072_rename_documents_to_qa_reports"
    )

    def test_the_lead_group_is_renamed_and_keeps_its_members(self):
        Group.objects.filter(name="QA Reports Lead").delete()
        lead = Group.objects.create(name="QC Documents Lead")
        member = _user(Company.objects.create(code="JIVO_BEV", name="Bev"))
        member.groups.add(lead)

        self.migration.forwards(global_apps, self._Editor())
        lead.refresh_from_db()
        self.assertEqual(lead.name, "QA Reports Lead")
        self.assertTrue(lead.user_set.filter(pk=member.pk).exists())
        self.assertEqual(Permission.objects.get(codename=APPROVE).name, "Can approve QA report entries")

        self.migration.backwards(global_apps, self._Editor())
        lead.refresh_from_db()
        self.assertEqual(lead.name, "QC Documents Lead")
        self.assertEqual(
            Permission.objects.get(codename=APPROVE).name, "Can approve QC document entries"
        )
