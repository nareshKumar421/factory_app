"""Editing a GRPO already in SAP (grpo.sap_edit and its screen's API).

SAP is faked by a reader holding one document and a PATCH that applies a
payload to it -- or, where SAP is known to, takes the payload and saves
nothing, refuses it, or does not answer.
"""

import copy
import shutil
import tempfile
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from grpo import sap_edit
from grpo.models import GRPOSapEdit, GRPOSapEditStatus
from sap_client.exceptions import SAPUnavailable, SAPValidationError
from sap_postings.models import SapPosting, SapPostingStatus

User = get_user_model()

UDFS = [{"column": "U_VehicleNoM", "label": "Vehicle Number", "type": "text", "max_length": 12}]
MONTHS = [{"code": "10-2026", "name": "10-2026"}, {"code": "09-2026", "name": "09-2026"}]


def _line(line_num, amount, month="10-2026", status="O", ap=None):
    return {
        "line_num": line_num, "item_code": "", "description": "EDIBLE OIL",
        "quantity": Decimal("0"), "unit_price": Decimal(amount), "discount_percent": Decimal("0"),
        "amount": Decimal(amount), "tax_amount": Decimal("0"), "tax_code": "RIGST@5",
        "gl_account": "5670001", "variety": "MUSTARD", "month": month, "budget": "Del Bkhp",
        "line_status": status, "ap_invoice_doc_num": ap,
    }


def _doc(**over):
    doc = {
        "doc_entry": 501, "doc_num": 2026106504, "doc_type": "S", "doc_status": "O",
        "cancelled": False, "doc_date": date(2026, 10, 10), "delivery_date": date(2026, 10, 10),
        "document_date": date(2026, 10, 10), "card_code": "VENDA000956", "card_name": "ARNAV",
        "vendor_ref": "7320", "remarks": "BILTY NO 7320", "contact_person": None,
        "attachment_entry": None, "branch_id": 2, "branch_name": "FACTORY",
        "doc_total": Decimal("4125"), "vat_sum": Decimal("0"), "created_by_code": "USER19",
        "created_by_name": "GURCHARAN", "created_on": date(2026, 10, 10),
        "updated_on": date(2026, 10, 10), "udfs": {"U_VehicleNoM": None}, "attachments": [],
        "lines": [_line(0, "2769"), _line(1, "1356", status="C", ap=52410)],
    }
    doc.update(over)
    return doc


class FakeSap:
    """One GRPO in SAP: the reader's view of it, and a PATCH that changes it."""

    SL_TO_KEY = {"NumAtCard": "vendor_ref", "TaxDate": "document_date",
                 "DocDueDate": "delivery_date", "Comments": "remarks",
                 "ContactPersonCode": "contact_person"}

    def __init__(self, doc):
        self.doc = doc
        self.patches = []
        self.saves = True

    # reader
    def header_udfs(self):
        return UDFS

    def document(self, doc_entry, udfs=None):
        return copy.deepcopy(self.doc) if doc_entry == self.doc["doc_entry"] else None

    def month_codes(self):
        return MONTHS

    def contact_persons(self, card_code):
        return [{"code": 7, "name": "AMIT", "active": True}]

    def find(self, doc_num):
        return [{"doc_entry": self.doc["doc_entry"], "doc_num": doc_num, "doc_type": self.doc["doc_type"],
                 "doc_date": self.doc["doc_date"], "card_name": "ARNAV", "cancelled": False}]

    # the Service Layer
    def patch(self, company_code, doc_entry, payload):
        self.patches.append(payload)
        if not self.saves:
            return
        for key, value in payload.items():
            if key == "DocumentLines":
                for body in value:
                    line = next(l for l in self.doc["lines"] if l["line_num"] == body["LineNum"])
                    if "UnitPrice" in body:
                        line["unit_price"] = line["amount"] = Decimal(str(body["UnitPrice"]))
                        line["discount_percent"] = Decimal(body.get("DiscountPercent", 0))
                    if "CostingCode2" in body:
                        line["month"] = body["CostingCode2"]
            elif key == "AttachmentEntry":
                self.doc["attachment_entry"] = value
            elif key.startswith("U_"):
                self.doc["udfs"][key] = value
            else:
                value = date.fromisoformat(value) if key in ("TaxDate", "DocDueDate") else value
                self.doc[self.SL_TO_KEY[key]] = value


def _change(after, before=None, field="vendor_ref"):
    return {"header": {field: {"before": before, "after": after}}}


class RuleTests(SimpleTestCase):
    def test_back_date_floor_follows_each_companys_cutoff_day(self):
        self.assertEqual(sap_edit.back_date_floor("JIVO_OIL", date(2026, 10, 12)), date(2026, 9, 1))
        self.assertEqual(sap_edit.back_date_floor("JIVO_OIL", date(2026, 10, 18)), date(2026, 9, 1))
        self.assertEqual(sap_edit.back_date_floor("JIVO_OIL", date(2026, 10, 19)), date(2026, 10, 1))
        self.assertEqual(sap_edit.back_date_floor("JIVO_BEVERAGES", date(2026, 10, 17)), date(2026, 10, 1))
        self.assertEqual(sap_edit.back_date_floor("JIVO_MART", date(2027, 1, 5)), date(2026, 12, 1))
        self.assertIsNone(sap_edit.back_date_floor("TC001", date(2026, 10, 12)))

    def test_unchanged_values_are_dropped_and_changed_ones_kept(self):
        changes = sap_edit.build_changes(_doc(), UDFS, {"header": {
            "vendor_ref": {"before": "7320", "after": " 7320 "},
            "U_VehicleNoM": {"before": "", "after": "HR55X1"},
            "document_date": {"before": "2026-10-10", "after": "2026-10-09"},
        }})
        self.assertEqual(set(changes["header"]), {"U_VehicleNoM", "document_date"})
        self.assertEqual(changes["header"]["document_date"]["after"], "2026-10-09")

    def test_a_value_changed_in_sap_since_the_page_opened_refuses_the_edit(self):
        with self.assertRaises(sap_edit.EditConflict) as caught:
            sap_edit.build_changes(_doc(), UDFS, _change("7321", before="7000"))
        self.assertIn("it is now 7320", str(caught.exception))

    def test_a_cancelled_grpo_cannot_be_edited(self):
        with self.assertRaises(sap_edit.EditError):
            sap_edit.build_changes(_doc(cancelled=True), UDFS, _change("7321", before="7320"))

    def test_a_material_grpos_lines_are_fixed(self):
        data = {"lines": [{"line_num": 0, "field": "amount", "before": "2769", "after": "3000"}]}
        with self.assertRaises(sap_edit.EditError) as caught:
            sap_edit.build_changes(_doc(doc_type="I"), UDFS, data)
        self.assertIn("fixed", str(caught.exception))

    def test_a_line_closed_by_an_ap_invoice_cannot_change(self):
        data = {"lines": [{"line_num": 1, "field": "amount", "before": "1356", "after": "1400"}]}
        with self.assertRaises(sap_edit.EditError) as caught:
            sap_edit.build_changes(_doc(), UDFS, data)
        self.assertIn("A/P invoice 52410", str(caught.exception))

    def test_amount_must_be_positive_and_month_active(self):
        with self.assertRaises(sap_edit.EditError):
            sap_edit.build_changes(_doc(), UDFS, {"lines": [
                {"line_num": 0, "field": "amount", "before": "2769", "after": "0"}]})
        with self.assertRaises(sap_edit.EditError):
            sap_edit.build_changes(_doc(), UDFS, {"lines": [
                {"line_num": 0, "field": "month", "before": "10-2026", "after": "13-2026"}]},
                month_codes={"10-2026", "09-2026"})

    def test_fields_sap_keeps_fixed_are_not_offered(self):
        with self.assertRaises(sap_edit.EditError):
            sap_edit.build_changes(_doc(), UDFS, _change("2026-10-01", field="doc_date"))
        with self.assertRaises(sap_edit.EditError):
            sap_edit.build_changes(_doc(), UDFS, {"lines": [
                {"line_num": 0, "field": "gl_account", "before": "5670001", "after": "5670002"}]})

    def test_the_apps_gate_entry_stamp_stays_in_the_remarks(self):
        doc = _doc(remarks="App: FactoryApp v2 | PO: 220826039 | Gate Entry: GE-2026-6247 | NO 4")
        with self.assertRaises(sap_edit.EditError) as caught:
            sap_edit.build_changes(doc, UDFS, {"header": {"remarks": {
                "before": doc["remarks"], "after": "App: FactoryApp v2 | PO: 220826039"}}})
        self.assertIn("Gate Entry: GE-2026-6247", str(caught.exception))
        changes = sap_edit.build_changes(doc, UDFS, {"header": {"remarks": {
            "before": doc["remarks"], "after": "Gate Entry: GE-2026-6247 | bill came late"}}})
        self.assertIn("remarks", changes["header"])

    def test_text_longer_than_sap_takes_is_refused(self):
        with self.assertRaises(sap_edit.EditError):
            sap_edit.build_changes(_doc(), UDFS, {"header": {
                "U_VehicleNoM": {"before": "", "after": "X" * 13}}})

    def test_amount_goes_as_unit_price_with_the_discount_cleared(self):
        doc = _doc()
        fields = {f.key: f for f in sap_edit.header_fields(UDFS)}
        changes = sap_edit.build_changes(doc, UDFS, {
            "header": {"U_VehicleNoM": {"before": "", "after": "HR55X1"}},
            "lines": [{"line_num": 0, "field": "amount", "before": "2769.00", "after": "3100"},
                      {"line_num": 0, "field": "month", "before": "10-2026", "after": "09-2026"}],
        })
        payload = sap_edit.payload_for(changes, doc, fields)
        self.assertEqual(payload["U_VehicleNoM"], "HR55X1")
        self.assertEqual(payload["DocumentLines"], [
            {"LineNum": 0, "UnitPrice": 3100.0, "DiscountPercent": 0, "CostingCode2": "09-2026"}])

    def test_clearing_a_user_defined_field_sends_null(self):
        doc = _doc(udfs={"U_VehicleNoM": "HR55X1"})
        fields = {f.key: f for f in sap_edit.header_fields(UDFS)}
        changes = sap_edit.build_changes(doc, UDFS, {"header": {
            "U_VehicleNoM": {"before": "HR55X1", "after": ""}}})
        self.assertEqual(sap_edit.payload_for(changes, doc, fields), {"U_VehicleNoM": None})

    def test_a_back_date_refusal_says_what_to_do(self):
        message = sap_edit.explain_refusal(
            "(2011) 20!Back Date Entry not allowed for Goods Receipt PO on 2026-08-10 by user B1i",
            "JIVO_OIL",
        )
        self.assertIn("Gurcharan or manager", message)
        self.assertEqual(sap_edit.explain_refusal("(-5002) other", "JIVO_OIL"), "(-5002) other")


class _EditFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Company", code="TC001")
        cls.user = User.objects.create_user(
            email="edit@example.com", password="x", full_name="Edit User", employee_code="E1"
        )
        role = UserRole.objects.create(name="GRPO editor")
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=role, is_default=True, is_active=True
        )

    def setUp(self):
        self.sap = FakeSap(_doc())
        reader = patch("grpo.sap_edit.reader_for", return_value=self.sap)
        writer = patch("grpo.sap_edit.patch_grpo", side_effect=self.sap.patch)
        reader.start()
        writer.start()
        self.addCleanup(reader.stop)
        self.addCleanup(writer.stop)

    def submit(self, data, files=()):
        return sap_edit.submit(
            company=self.company, document=self.sap.document(501), udfs=UDFS,
            data=data, files=list(files), user=self.user,
        )

    def grant(self, codename):
        self.user.user_permissions.add(Permission.objects.get(codename=codename))
        self.user = User.objects.get(pk=self.user.pk)  # drop the permission cache


class SendTests(_EditFixtures):
    def test_an_edit_is_patched_read_back_and_saved(self):
        edit, outcome = self.submit({
            "header": {"vendor_ref": {"before": "7320", "after": "7320A"}},
            "lines": [{"line_num": 0, "field": "amount", "before": "2769", "after": "3100"}],
        })
        self.assertEqual(outcome.kind, "POSTED")
        self.assertEqual(edit.status, GRPOSapEditStatus.POSTED)
        self.assertEqual(self.sap.patches, [{
            "NumAtCard": "7320A",
            "DocumentLines": [{"LineNum": 0, "UnitPrice": 3100.0, "DiscountPercent": 0}],
        }])
        self.assertIn("Vendor Ref. No., Amount, line 1", edit.message)
        posting = SapPosting.objects.get(kind=sap_edit.KIND, source_id=edit.pk)
        self.assertEqual(posting.status, SapPostingStatus.POSTED)

    def test_a_retry_finding_the_change_already_in_sap_sends_nothing(self):
        edit = GRPOSapEdit.objects.create(
            company=self.company, doc_entry=501, doc_num=2026106504, doc_type="S",
            changes={"header": {"vendor_ref": {"label": "Vendor Ref. No.", "before": "7000",
                                               "after": "7320"}}, "lines": []},
        )
        outcome = sap_edit.GRPOEditHandler().send(MagicMock(source_id=edit.pk))
        self.assertEqual(outcome.kind, "POSTED")
        self.assertEqual(self.sap.patches, [])

    def test_a_change_made_in_sap_meanwhile_is_not_overwritten(self):
        edit = GRPOSapEdit.objects.create(
            company=self.company, doc_entry=501, doc_num=2026106504, doc_type="S",
            changes={"header": {"vendor_ref": {"label": "Vendor Ref. No.", "before": "7000",
                                               "after": "7001"}}, "lines": []},
        )
        outcome = sap_edit.GRPOEditHandler().send(MagicMock(source_id=edit.pk))
        self.assertEqual(outcome.kind, "REJECTED")
        self.assertIn("changed in SAP to 7320", outcome.message)
        self.assertEqual(self.sap.patches, [])

    def test_a_change_sap_takes_but_does_not_save_is_refused(self):
        self.sap.saves = False
        edit, outcome = self.submit(_change("7320A", before="7320"))
        self.assertEqual(outcome.kind, "REJECTED")
        self.assertIn("did not save Vendor Ref. No.", outcome.message)
        self.assertEqual(edit.status, GRPOSapEditStatus.REFUSED)

    def test_sap_not_answering_leaves_the_edit_waiting(self):
        with patch("grpo.sap_edit.patch_grpo", side_effect=SAPUnavailable("down")):
            edit, outcome = self.submit(_change("7320A", before="7320"))
        self.assertEqual(outcome.kind, "WAITING")
        self.assertEqual(edit.status, GRPOSapEditStatus.WAITING)
        posting = SapPosting.objects.get(kind=sap_edit.KIND, source_id=edit.pk)
        self.assertEqual(posting.status, SapPostingStatus.QUEUED)

    def test_another_edit_waits_until_the_first_has_gone(self):
        with patch("grpo.sap_edit.patch_grpo", side_effect=SAPUnavailable("down")):
            self.submit(_change("7320A", before="7320"))
        with self.assertRaises(sap_edit.EditConflict):
            self.submit(_change("7320B", before="7320"))

    def test_sap_refusing_keeps_its_words(self):
        refusal = "(2011) 20!Back Date Entry not allowed for Goods Receipt PO on 2026-08-10 by user B1i"
        with patch("grpo.sap_edit.patch_grpo", side_effect=SAPValidationError(refusal)):
            edit, outcome = self.submit(_change("7320A", before="7320"))
        self.assertEqual(outcome.kind, "REJECTED")
        self.assertTrue(edit.message.startswith(refusal))

    def test_nothing_changed_is_refused_before_anything_is_recorded(self):
        with self.assertRaises(sap_edit.EditError):
            self.submit(_change("7320", before="7320"))
        self.assertFalse(GRPOSapEdit.objects.exists())


class AttachmentTests(_EditFixtures):
    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        settings = override_settings(MEDIA_ROOT=media)
        settings.enable()
        self.addCleanup(settings.disable)
        self.client_sap = MagicMock()
        self.client_sap.upload_attachment.return_value = {"AbsoluteEntry": 900}
        sap_client = patch("sap_client.client.SAPClient", return_value=self.client_sap)
        sap_client.start()
        self.addCleanup(sap_client.stop)

    def _file(self, name="bill.pdf"):
        return SimpleUploadedFile(name, b"%PDF-1.4 test", content_type="application/pdf")

    def test_a_grpo_without_attachments_gets_the_new_entry_linked(self):
        edit, outcome = self.submit({}, files=[self._file(), self._file("lr.pdf")])
        self.assertEqual(outcome.kind, "POSTED")
        self.client_sap.upload_attachment.assert_called_once()
        self.client_sap.add_line_to_existing_attachment.assert_called_once()
        self.assertEqual(self.sap.patches, [{"AttachmentEntry": 900}])
        self.assertEqual(edit.sap_attachment_entry, 900)

    def test_a_grpo_with_attachments_gets_lines_added_and_no_patch(self):
        self.sap.doc["attachment_entry"] = 555
        edit, outcome = self.submit({}, files=[self._file()])
        self.assertEqual(outcome.kind, "POSTED")
        self.client_sap.upload_attachment.assert_not_called()
        self.assertEqual(
            self.client_sap.add_line_to_existing_attachment.call_args.kwargs["absolute_entry"], 555
        )
        self.assertEqual(self.sap.patches, [])

    def test_a_retry_does_not_upload_a_file_twice(self):
        with patch("grpo.sap_edit.patch_grpo", side_effect=SAPUnavailable("down")):
            edit, _ = self.submit({}, files=[self._file()])
        self.assertEqual(self.client_sap.upload_attachment.call_count, 1)
        outcome = sap_edit.GRPOEditHandler().send(MagicMock(source_id=edit.pk))
        self.assertEqual(outcome.kind, "POSTED")
        self.assertEqual(self.client_sap.upload_attachment.call_count, 1)
        self.assertEqual(self.sap.patches, [{"AttachmentEntry": 900}])


class ApiTests(_EditFixtures):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def get(self, url):
        return self.api.get(url, HTTP_COMPANY_CODE="TC001")

    def post(self, url, data):
        return self.api.post(url, data, format="json", HTTP_COMPANY_CODE="TC001")

    def test_the_screen_needs_one_of_the_two_rights(self):
        self.assertEqual(self.get("/api/v1/grpo/sap-edit/501/").status_code, 403)

    def test_viewing_a_grpo_of_the_other_type_is_allowed_but_not_changing_it(self):
        self.grant("can_edit_grpo_in_sap")
        self.api.force_authenticate(user=self.user)
        response = self.get("/api/v1/grpo/sap-edit/501/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["can_edit"])
        self.assertEqual([f["key"] for f in response.data["line_fields"]], ["amount", "month"])
        self.assertEqual(response.data["lines"][1]["closed_reason"], "Closed by A/P invoice 52410.")
        refused = self.post("/api/v1/grpo/sap-edit/501/", _change("7320A", before="7320"))
        self.assertEqual(refused.status_code, 403)

    def test_a_service_edit_through_the_api(self):
        self.grant("can_edit_service_grpo_in_sap")
        self.api.force_authenticate(user=self.user)
        page = self.get("/api/v1/grpo/sap-edit/501/").data
        self.assertTrue(page["can_edit"])
        before = next(f["value"] for f in page["header_fields"] if f["key"] == "vendor_ref")
        response = self.post("/api/v1/grpo/sap-edit/501/", _change("7320A", before=before))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["edit"]["status"], "POSTED")
        stale = self.post("/api/v1/grpo/sap-edit/501/", {"header": {
            "remarks": {"before": "old remarks", "after": "new"}}})
        self.assertEqual(stale.status_code, 409)

    def test_sap_refusing_is_a_400_with_its_message(self):
        self.grant("can_edit_service_grpo_in_sap")
        self.api.force_authenticate(user=self.user)
        with patch("grpo.sap_edit.patch_grpo", side_effect=SAPValidationError("(-5002) no")):
            response = self.post("/api/v1/grpo/sap-edit/501/", _change("7320A", before="7320"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("(-5002) no", response.data["detail"])

    def test_back_dated_grpos_are_flagged_up_front(self):
        self.grant("can_edit_service_grpo_in_sap")
        self.sap.doc["doc_date"] = date(2020, 1, 1)
        self.api.force_authenticate(user=self.user)
        with patch("grpo.sap_edit.back_date_floor", return_value=date(2026, 9, 1)):
            response = self.get("/api/v1/grpo/sap-edit/501/")
        self.assertIn("01 Sep 2026", response.data["back_date"]["message"])

    def test_find_by_number(self):
        self.grant("can_edit_grpo_in_sap")
        self.api.force_authenticate(user=self.user)
        self.assertEqual(self.get("/api/v1/grpo/sap-edit/find/?doc_num=abc").status_code, 400)
        response = self.get("/api/v1/grpo/sap-edit/find/?doc_num=2026106504")
        self.assertEqual(response.data["results"][0]["doc_entry"], 501)

    def test_each_screen_finds_only_its_own_type(self):
        self.grant("can_edit_service_grpo_in_sap")
        self.api.force_authenticate(user=self.user)
        service = self.get("/api/v1/grpo/sap-edit/find/?doc_num=2026106504&doc_type=S").data
        self.assertEqual([r["doc_entry"] for r in service["results"]], [501])
        material = self.get("/api/v1/grpo/sap-edit/find/?doc_num=2026106504&doc_type=I").data
        self.assertEqual(material["results"], [])
        self.assertEqual(material["other_type"][0]["doc_type_label"], "Service")
        self.assertEqual(
            self.get("/api/v1/grpo/sap-edit/find/?doc_num=1&doc_type=X").status_code, 400
        )

    def test_the_posting_log_links_each_type_to_its_own_screen(self):
        edit = GRPOSapEdit(doc_entry=501, doc_type="S")
        self.assertEqual(sap_edit.link_for(edit), "/dispatch/bilty-grpo/sap-edit/501")
        edit.doc_type = "I"
        self.assertEqual(sap_edit.link_for(edit), "/warehouse/grpo/sap-edit/501")
