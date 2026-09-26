"""Tests for the SAP plumbing ported from SAP Portal (backend_v1).

Covers the master-data lookup reader and its API, the shared pending-status
rule (with the portal's own cases), the Service Layer entity client, the new
writers (business partners, BOMs, budgets, production-order status), the
approval writer's typed-password and withdraw paths, and the attachment
download client. Every SAP call is mocked — nothing here reaches SAP.

    python manage.py test sap_client.tests_sap_portal --settings=config.sqlite_test_settings
"""

import io
import zipfile
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole

from . import approval_status
from .exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .hana.lookup_reader import HanaLookupReader
from .service_layer import entity_client as entity_client_module
from .service_layer.approval_writer import REQUEST_CANCELLED, ApprovalRequestWriter
from .service_layer.budget_writer import BudgetWriter
from .service_layer.business_partner_writer import BusinessPartnerWriter
from .service_layer.entity_client import ServiceLayerEntityClient, odata_string
from .service_layer.file_service_client import (
    SapFileServiceClient,
    compressed_variant,
    describe_unencodable_chars,
)
from .service_layer.product_tree_writer import ProductTreeWriter
from .service_layer.production_order_writer import ProductionOrderWriter
from .views_lookups import LOOKUPS

SL_CONFIG = {
    "base_url": "https://sl.test:50000",
    "company_db": "TEST_DB",
    "username": "svc",
    "password": "svc-pass",
    "approvers": {"USER37": "stored-pass"},
}


def _context():
    context = MagicMock()
    context.service_layer = dict(SL_CONFIG)
    context.hana = {"host": "h", "port": 1, "user": "u", "password": "p", "schema": "SCHEMA"}
    context.company_code = "JIVO_OIL"
    return context


def _response(status_code, json_body=None, headers=None, content=b"x"):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.content = content if json_body is not None or content else b""
    response.json.return_value = json_body if json_body is not None else {}
    response.text = ""
    return response


# ---------------------------------------------------------------------------
# Lookup reader
# ---------------------------------------------------------------------------


class LookupReaderTests(SimpleTestCase):
    def setUp(self):
        self.reader = HanaLookupReader(_context())
        self.cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = self.cursor
        patcher = patch.object(self.reader.connection, "connect", return_value=conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sql_and_params(self):
        sql, params = self.cursor.execute.call_args[0]
        return sql, params

    def test_item_search_binds_the_text_and_never_formats_it(self):
        self.cursor.fetchall.return_value = [("RM001", "Crude Oil", "KG", 102.5)]
        rows = self.reader.search_items("cru'de", limit=20)
        sql, params = self._sql_and_params()
        self.assertNotIn("cru'de", sql.lower())
        self.assertEqual(params, ("%CRU'DE%", "%CRU'DE%"))
        self.assertIn('"SCHEMA"."OITM"', sql)
        self.assertIn("TOP 20", sql)
        self.assertEqual(
            rows, [{"item_code": "RM001", "item_name": "Crude Oil", "uom": "KG", "last_purchase_price": 102.5}]
        )

    def test_item_search_needs_two_characters(self):
        self.assertEqual(self.reader.search_items("a"), [])
        self.cursor.execute.assert_not_called()

    def test_costing_codes_refuse_a_dimension_outside_one_to_five(self):
        with self.assertRaises(SAPValidationError):
            self.reader.costing_codes(9)
        with self.assertRaises(SAPValidationError):
            self.reader.costing_codes("x")

    def test_next_card_code_keeps_the_width(self):
        self.cursor.fetchall.return_value = [("VENDA000123",)]
        self.assertEqual(self.reader.next_card_code("venda", "S"), "VENDA000124")
        _, params = self._sql_and_params()
        self.assertEqual(params, ("VENDA%", "S"))

    def test_next_card_code_starts_at_six_digits_for_an_unused_prefix(self):
        self.cursor.fetchall.return_value = [(None,)]
        self.assertEqual(self.reader.next_card_code("CUSTA", "C"), "CUSTA000001")

    def test_next_card_code_refuses_a_bad_prefix_or_type(self):
        with self.assertRaises(SAPValidationError):
            self.reader.next_card_code("BAD PREFIX!", "C")
        with self.assertRaises(SAPValidationError):
            self.reader.next_card_code("CUSTA", "X")

    def test_a_missing_user_table_is_an_empty_list_with_a_warning(self):
        self.cursor.execute.side_effect = __import__("hdbcli").dbapi.Error("invalid table name")
        rows, warning = self.reader.user_table_values("chain")
        self.assertEqual(rows, [])
        self.assertIn("@CHAIN", warning)

    def test_an_unknown_user_table_is_refused(self):
        with self.assertRaises(SAPValidationError):
            self.reader.user_table_values("OUSR")

    def test_partners_with_tax_ids_merges_gstin_and_pan_matches(self):
        self.cursor.fetchall.side_effect = [
            [("VENDA000010", "Acme")],
            [("VENDA000010", "Acme"), ("VENDA000011", "Acme Two")],
        ]
        rows = self.reader.partners_with_tax_ids("S", gstin="06abcde1234f1z5", pan="abcde1234f")
        self.assertEqual(
            rows,
            [
                {"card_code": "VENDA000010", "card_name": "Acme", "matched_on": ["GSTIN", "PAN"]},
                {"card_code": "VENDA000011", "card_name": "Acme Two", "matched_on": ["PAN"]},
            ],
        )
        first_sql = self.cursor.execute.call_args_list[0][0][0]
        self.assertIn('"CRD1"', first_sql)
        self.assertIn('"GSTRegnNo"', first_sql)

    def test_connection_failure_is_an_outage_and_query_failure_a_data_error(self):
        from hdbcli import dbapi

        with patch.object(self.reader.connection, "connect", side_effect=dbapi.Error("down")):
            with self.assertRaises(SAPConnectionError):
                self.reader.tax_codes()
        self.cursor.execute.side_effect = dbapi.Error("bad sql")
        with self.assertRaises(SAPDataError):
            self.reader.tax_codes()


# ---------------------------------------------------------------------------
# Pending-status rule — the portal's own cases
# (backend_v1/tests/stale-approval-requests.test.js)
# ---------------------------------------------------------------------------


class ApprovalStatusRuleTests(SimpleTestCase):
    def test_a_pending_request_on_a_still_pending_draft_stays_pending(self):
        self.assertEqual(approval_status.effective_status("W", "Y", "W"), "W")

    def test_a_pending_request_whose_draft_approval_was_cancelled_is_cancelled(self):
        self.assertEqual(approval_status.effective_status("W", "Y", "C"), "C")

    def test_a_pending_request_takes_the_outcome_its_draft_reached(self):
        for draft in ("N", "Y", "P", "A"):
            self.assertEqual(approval_status.effective_status("W", "Y", draft), draft)

    def test_a_draft_that_is_gone_or_no_longer_under_approval_counts_as_cancelled(self):
        for draft in (None, "-"):
            self.assertEqual(approval_status.effective_status("W", "Y", draft), "C")

    def test_a_request_raised_on_an_existing_document_keeps_its_own_status(self):
        self.assertEqual(approval_status.effective_status("W", "N", None), "W")
        self.assertEqual(approval_status.effective_status("W", "N", "-"), "W")

    def test_the_service_layer_spelling_of_is_draft_is_understood(self):
        self.assertEqual(approval_status.effective_status("W", "tYES", "C"), "C")
        self.assertEqual(approval_status.effective_status("W", "tNO", "C"), "W")

    def test_decided_requests_are_never_rewritten(self):
        self.assertEqual(approval_status.effective_status("Y", "Y", "C"), "Y")
        self.assertEqual(approval_status.effective_status("N", "Y", "W"), "N")
        self.assertEqual(approval_status.effective_status("C", "Y", "W"), "C")

    def test_service_layer_authorization_status_maps_onto_wdd_status(self):
        cases = {
            "dasPending": "W", "dasCancelled": "C", "dasRejected": "N", "dasWithout": "-",
            "dasGeneratedbyAuthorizer": "A", "pasCancelled": "C", "pasApproved": "Y",
            "": None, "somethingElse": None,
        }
        for value, expected in cases.items():
            self.assertEqual(approval_status.draft_status_from_sl(value), expected, value)

    def test_any_filter_other_than_pending_also_scans_the_rows_still_at_w(self):
        self.assertEqual(approval_status.owdd_codes_to_scan(("W",)), ("W",))
        self.assertEqual(approval_status.owdd_codes_to_scan(("N",)), ("N", "W"))
        self.assertEqual(approval_status.owdd_codes_to_scan(("Y", "P", "A")), ("Y", "P", "A", "W"))

    def test_the_sql_rule_reads_the_alias_it_is_given(self):
        sql = approval_status.effective_status_sql("H")
        self.assertIn('H."Status"', sql)
        self.assertIn('H."IsDraft"', sql)
        self.assertIn("'N', 'Y', 'P', 'A'", sql)
        self.assertNotIn('W."Status"', sql)


# ---------------------------------------------------------------------------
# Service Layer entity client
# ---------------------------------------------------------------------------


class EntityClientTests(SimpleTestCase):
    def setUp(self):
        entity_client_module.clear_session_cache()
        self.addCleanup(entity_client_module.clear_session_cache)
        patcher = patch("sap_client.service_layer.entity_client.ServiceLayerSession")
        self.session_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.session_class.return_value.login.return_value = {"B1SESSION": "one"}
        self.client = ServiceLayerEntityClient(_context())

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_the_session_is_reused_between_calls(self, request):
        request.return_value = _response(200, {"value": []})
        self.client.get("Items")
        self.client.get("Items")
        self.assertEqual(self.session_class.return_value.login.call_count, 1)

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_an_expired_session_logs_in_again_once(self, request):
        request.side_effect = [_response(401, {"error": {"message": {"value": "expired"}}}), _response(200, {"ok": 1})]
        self.assertEqual(self.client.get("Items(1)"), {"ok": 1})
        self.assertEqual(self.session_class.return_value.login.call_count, 2)

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_a_repeated_401_is_a_refusal_not_an_outage(self, request):
        request.return_value = _response(401, {"error": {"code": -6006, "message": {"value": "not permitted"}}})
        with self.assertRaises(SAPValidationError) as ctx:
            self.client.patch("X(1)", {"a": 1})
        self.assertIn("-6006", str(ctx.exception))

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_sap_400_carries_sap_words(self, request):
        request.return_value = _response(400, {"error": {"code": -5002, "message": {"value": "Duplicate"}}})
        with self.assertRaises(SAPValidationError) as ctx:
            self.client.post("BusinessPartners", {"CardCode": "C1"})
        self.assertIn("Duplicate", str(ctx.exception))

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_a_write_timeout_says_to_check_sap_first(self, request):
        request.side_effect = requests.exceptions.Timeout("slow")
        with self.assertRaises(SAPConnectionError) as ctx:
            self.client.post("BusinessPartners", {"CardCode": "C1"})
        self.assertIn("check SAP", str(ctx.exception))

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_typed_credentials_never_ride_a_cached_session(self, request):
        request.return_value = _response(200, {})
        typed = ServiceLayerEntityClient(_context(), dict(SL_CONFIG, username="USER37"), cache_session=False)
        typed.get("X")
        typed.get("X")
        self.assertEqual(self.session_class.return_value.login.call_count, 2)
        self.assertEqual(entity_client_module._sessions, {})

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_get_all_follows_next_links(self, request):
        request.side_effect = [
            _response(200, {"value": [1, 2], "@odata.nextLink": "Banks?$skip=2"}),
            _response(200, {"value": [3]}),
        ]
        self.assertEqual(self.client.get_all("Banks", top=2), [1, 2, 3])
        self.assertTrue(request.call_args_list[1][0][1].endswith("/b1s/v2/Banks?$skip=2"))

    def test_odata_strings_double_their_quotes(self):
        self.assertEqual(odata_string("RM'01"), "'RM''01'")


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


class WriterTests(SimpleTestCase):
    def setUp(self):
        entity_client_module.clear_session_cache()
        self.addCleanup(entity_client_module.clear_session_cache)
        patcher = patch("sap_client.service_layer.entity_client.ServiceLayerSession")
        self.session_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.session_class.return_value.login.return_value = {"B1SESSION": "one"}

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_business_partner_create_reports_the_card_code(self, request):
        request.return_value = _response(201, {"CardCode": "VENDA000124", "CardName": "Acme"})
        result = BusinessPartnerWriter(_context()).create({"CardCode": "VENDA000124", "CardName": "Acme"})
        self.assertEqual(result["card_code"], "VENDA000124")
        method, url = request.call_args[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/b1s/v2/BusinessPartners"))

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_bom_replace_quotes_the_tree_code(self, request):
        request.return_value = _response(204, None, content=b"")
        ProductTreeWriter(_context()).replace("FG'01", {"TreeCode": "FG'01"})
        method, url = request.call_args[0]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/b1s/v2/ProductTrees('FG''01')"))

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_budget_update_replaces_the_line_collection(self, request):
        request.return_value = _response(204, None, content=b"")
        BudgetWriter(_context()).update(7, {"U_BUDGET": "B1", "BUDGET1Collection": []})
        kwargs = request.call_args[1]
        self.assertEqual(kwargs["headers"]["B1S-ReplaceCollectionsOnPatch"], "true")
        self.assertTrue(request.call_args[0][1].endswith("/b1s/v2/BUDGET(7)"))

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_production_order_release_and_close_patch_the_status(self, request):
        request.return_value = _response(204, None, content=b"")
        writer = ProductionOrderWriter(_context())
        writer.release(55)
        self.assertEqual(request.call_args[1]["json"], {"ProductionOrderStatus": "boposReleased"})
        writer.close(55)
        self.assertEqual(request.call_args[1]["json"], {"ProductionOrderStatus": "boposClosed"})
        self.assertTrue(request.call_args[0][1].endswith("/b1s/v2/ProductionOrders(55)"))


# ---------------------------------------------------------------------------
# Approval writer: typed password and withdraw
# ---------------------------------------------------------------------------


class ApprovalWriterPortalPathTests(SimpleTestCase):
    def setUp(self):
        self.writer = ApprovalRequestWriter(_context())
        patcher = patch("sap_client.service_layer.approval_writer.ServiceLayerSession")
        self.session_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.session_class.return_value.login.return_value = {"B1SESSION": "s"}

    @patch("sap_client.service_layer.approval_writer.requests.patch")
    @patch("sap_client.service_layer.approval_writer.requests.get")
    def test_a_typed_password_signs_instead_of_the_stored_one(self, get, patch_):
        get.return_value = _response(200, {"Status": "arsPending"})
        patch_.return_value = _response(204, None, content=b"")
        self.writer.decide(10, True, approver="USER37", password="typed-pass")
        session_config = self.session_class.call_args[0][0]
        self.assertEqual(session_config["password"], "typed-pass")
        decision = patch_.call_args[1]["json"]["ApprovalRequestDecisions"][0]
        self.assertEqual(decision["ApproverPassword"], "typed-pass")

    def test_a_typed_password_needs_the_account_it_belongs_to(self):
        with self.assertRaises(SAPValidationError):
            self.writer.decide(10, True, approver="", password="typed-pass")

    def test_an_empty_typed_password_is_refused(self):
        with self.assertRaises(SAPValidationError):
            self.writer.decide(10, True, approver="USER37", password="")

    @patch("sap_client.service_layer.approval_writer.requests.patch")
    @patch("sap_client.service_layer.approval_writer.requests.get")
    def test_without_a_typed_password_the_stored_one_is_used_as_before(self, get, patch_):
        get.return_value = _response(200, {"Status": "arsPending"})
        patch_.return_value = _response(204, None, content=b"")
        self.writer.decide(10, False, "reason", approver="USER37")
        self.assertEqual(self.session_class.call_args[0][0]["password"], "stored-pass")

    @patch("sap_client.service_layer.approval_writer.requests.patch")
    @patch("sap_client.service_layer.approval_writer.requests.get")
    def test_withdraw_cancels_as_the_originator(self, get, patch_):
        get.return_value = _response(200, {"Status": "arsPending"})
        patch_.return_value = _response(204, None, content=b"")
        result = self.writer.cancel(10, "USER12", password="orig-pass")
        self.assertEqual(patch_.call_args[1]["json"], {"Status": REQUEST_CANCELLED})
        self.assertEqual(self.session_class.call_args[0][0]["username"], "USER12")
        self.assertEqual(result["signed_as"], "USER12")

    @patch("sap_client.service_layer.approval_writer.requests.get")
    def test_withdraw_refuses_a_decided_request(self, get):
        get.return_value = _response(200, {"Status": "arsApproved"})
        with self.assertRaises(SAPValidationError):
            self.writer.cancel(10, "USER12", password="orig-pass")


# ---------------------------------------------------------------------------
# Attachment download client
# ---------------------------------------------------------------------------


def _file_response(status_code, body=b"", content_type="application/pdf", json_detail=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"Content-Type": content_type}
    response.iter_content.return_value = [body]
    response.content = (
        ('{"detail": "%s"}' % json_detail).encode() if json_detail is not None else body
    )
    return response


@override_settings(
    SAP_FILE_SERVICE_BASE_URL="http://files.test:8012",
    SAP_FILE_SERVICE_COMPANY_IDS={"JIVO_OIL": "1"},
)
class FileServiceClientTests(SimpleTestCase):
    @override_settings(SAP_FILE_SERVICE_BASE_URL="")
    def test_an_unconfigured_service_says_so(self):
        with self.assertRaises(SAPConnectionError):
            SapFileServiceClient("JIVO_OIL").fetch_by_entry(5, 1)

    @patch("sap_client.service_layer.file_service_client.requests.get")
    def test_by_entry_returns_the_raw_file(self, get):
        get.return_value = _file_response(200, b"%PDF-1.4")
        result = SapFileServiceClient("JIVO_OIL").fetch_by_entry(5, 1, "bill.pdf")
        self.assertEqual(result["data"], b"%PDF-1.4")
        self.assertEqual(result["file_name"], "bill.pdf")
        self.assertEqual(get.call_args[0][0], "http://files.test:8012/files/by-entry/5/1")
        self.assertEqual(get.call_args[1]["params"], {"company": "1"})

    @patch("sap_client.service_layer.file_service_client.requests.get")
    def test_a_zip_answer_is_unwrapped_to_the_matching_file(self, get):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("other.txt", b"no")
            archive.writestr("folder/bill.pdf", b"%PDF-yes")
        get.return_value = _file_response(200, buffer.getvalue(), content_type="application/zip")
        result = SapFileServiceClient("JIVO_OIL").fetch_by_entry(5, 1, "bill.pdf")
        self.assertEqual(result["data"], b"%PDF-yes")
        self.assertEqual(result["file_name"], "bill.pdf")

    @patch("sap_client.service_layer.file_service_client.requests.get")
    def test_by_name_falls_back_to_the_compressed_copy(self, get):
        get.side_effect = [_file_response(404, json_detail="missing"), _file_response(200, b"ok")]
        result = SapFileServiceClient("JIVO_OIL").fetch_by_name("2858.pdf")
        self.assertEqual(result["file_name"], "2858_compressed.pdf")
        self.assertTrue(get.call_args_list[1][0][0].endswith("/files/2858_compressed.pdf"))

    @patch("sap_client.service_layer.file_service_client.requests.get")
    def test_a_latin1_header_failure_names_the_character(self, get):
        get.return_value = _file_response(500, json_detail="'latin-1' codec can't encode")
        with self.assertRaises(SAPDataError) as ctx:
            SapFileServiceClient("JIVO_OIL").fetch_by_entry(5, 1, "bill copy.pdf")
        self.assertIn("U+202F NARROW NO-BREAK SPACE", str(ctx.exception))

    def test_helpers(self):
        self.assertEqual(compressed_variant("a.pdf"), "a_compressed.pdf")
        self.assertIsNone(compressed_variant("a_compressed.pdf"))
        self.assertEqual(describe_unencodable_chars("x₹y"), ["U+20B9 INDIAN RUPEE SIGN"])


# ---------------------------------------------------------------------------
# Lookup API
# ---------------------------------------------------------------------------


class LookupApiTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Oil")
        role = UserRole.objects.create(name="Clerk")
        self.user = User.objects.create_user(email="clerk@example.com", full_name="Clerk", password="x")
        UserCompany.objects.create(user=self.user, company=self.company, role=role)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_every_lookup_has_a_route(self):
        for name in LOOKUPS:
            self.assertTrue(reverse(f"sap-lookup-{name}").startswith("/api/v1/sap-lookups/"))

    def test_the_company_header_is_required(self):
        response = self.api.get("/api/v1/sap-lookups/tax-codes/")
        self.assertEqual(response.status_code, 403)

    @patch("sap_client.views_lookups.SAPClient")
    def test_a_lookup_reads_the_callers_company(self, sap_client):
        sap_client.return_value.lookup_bp_groups.return_value = [{"code": 101, "name": "Traders"}]
        response = self.api.get(
            "/api/v1/sap-lookups/bp-groups/?type=s", HTTP_COMPANY_CODE="JIVO_OIL"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"code": 101, "name": "Traders"}])
        sap_client.assert_called_once_with(company_code="JIVO_OIL")
        sap_client.return_value.lookup_bp_groups.assert_called_once_with("S")

    @patch("sap_client.views_lookups.SAPClient")
    def test_sap_errors_map_to_400_503_and_502(self, sap_client):
        cases = (
            (SAPValidationError("bad"), 400),
            (SAPConnectionError("down"), 503),
            (SAPDataError("broken"), 502),
        )
        for error, expected in cases:
            sap_client.return_value.lookup_tax_codes.side_effect = error
            response = self.api.get("/api/v1/sap-lookups/tax-codes/", HTTP_COMPANY_CODE="JIVO_OIL")
            self.assertEqual(response.status_code, expected, error)

    @patch("sap_client.views_lookups.SAPClient")
    def test_next_card_code_defaults_the_prefix_by_type(self, sap_client):
        sap_client.return_value.next_card_code.return_value = "VENDA000001"
        response = self.api.get(
            "/api/v1/sap-lookups/next-card-code/?type=S", HTTP_COMPANY_CODE="JIVO_OIL"
        )
        self.assertEqual(response.json(), {"card_code": "VENDA000001"})
        sap_client.return_value.next_card_code.assert_called_once_with("VENDA", "S")

    @patch("sap_client.views_lookups.SAPClient")
    def test_batches_need_an_item_and_a_warehouse(self, sap_client):
        response = self.api.get("/api/v1/sap-lookups/batches/?item_code=RM1", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# Finance reader
# ---------------------------------------------------------------------------


class FinanceReaderTests(SimpleTestCase):
    def setUp(self):
        from .hana.finance_reader import HanaFinanceReader

        self.reader = HanaFinanceReader(_context())
        self.cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = self.cursor
        patcher = patch.object(self.reader.connection, "connect", return_value=conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_chart_of_accounts_rolls_postable_balances_up_to_every_title(self):
        self.cursor.fetchall.return_value = [
            ("1000000", "ASSETS", None, 1, 1, "N", "N", "INR", 0, "N", ""),
            ("1100000", "Current Assets", "1000000", 1, 2, "N", "N", "INR", 0, "N", ""),
            ("1101001", "Debtors", "1100000", 1, 3, "Y", "N", "INR", 150.25, "N", ""),
            ("1101002", "Cash", "1100000", 1, 3, "Y", "N", "INR", 49.75, "N", ""),
            ("4000000", "REVENUES", None, 4, 1, "N", "I", "INR", 0, "N", ""),
        ]
        tree = self.reader.chart_of_accounts()
        by_code = {a["code"]: a for a in tree["accounts"]}
        self.assertEqual(by_code["1100000"]["rollup"], 200.0)
        self.assertEqual(by_code["1000000"]["rollup"], 200.0)
        self.assertEqual(by_code["1100000"]["children"], 2)
        self.assertEqual(tree["drawers"][0]["total"], 200.0)
        self.assertEqual(tree["postable"], 2)

    def test_a_search_keeps_the_path_to_each_hit(self):
        self.cursor.fetchall.return_value = [
            ("1000000", "ASSETS", None, 1, 1, "N", "N", "INR", 0, "N", ""),
            ("1100000", "Current Assets", "1000000", 1, 2, "N", "N", "INR", 0, "N", ""),
            ("1101001", "Debtors", "1100000", 1, 3, "Y", "N", "INR", 1, "N", ""),
            ("2000000", "LIABILITIES", None, 2, 1, "N", "N", "INR", 0, "N", ""),
        ]
        tree = self.reader.chart_of_accounts(search="debt")
        self.assertEqual([a["code"] for a in tree["accounts"]], ["1000000", "1100000", "1101001"])
        self.assertEqual([a["match"] for a in tree["accounts"]], [False, False, True])

    def test_the_ledger_balance_is_anchored_below_postings_after_the_range(self):
        """The portal walked back from today's balance even for a past range."""
        from datetime import date

        self.cursor.fetchall.side_effect = [
            [("Debtors", 1000)],        # OACT: name + today's balance
            [(2,)],                    # count in range
            [(300,)],                  # debit - credit posted after date_to
            [                          # the rows, newest first
                (2, date(2026, 6, 20), None, None, 50, 0, "m2", "", "C1", "Cust", "", 13),
                (1, date(2026, 6, 10), None, None, 0, 20, "m1", "", "C1", "Cust", "", 24),
            ],
        ]
        ledger = self.reader.general_ledger("1101001", date_to=date(2026, 6, 30))
        self.assertEqual(ledger["closing_balance"], 700.0)
        self.assertEqual([line["balance"] for line in ledger["lines"]], [700.0, 650.0])
        self.assertEqual(ledger["kind"], "G/L")

    def test_an_unknown_ledger_account_is_refused(self):
        self.cursor.fetchall.side_effect = [[], []]
        with self.assertRaises(SAPValidationError):
            self.reader.general_ledger("NOPE")

    def test_journal_entry_filters_are_bound(self):
        self.cursor.fetchall.return_value = []
        self.reader.journal_entries(reference="inv'1", trans_type="13", limit=5)
        sql, params = self.cursor.execute.call_args[0]
        self.assertNotIn("inv'1", sql.lower())
        self.assertEqual(params, ("%INV'1%",) * 4 + ("13",))
        self.assertIn("TOP 5", sql)


class SapUserCodesByIdTests(SimpleTestCase):
    """OUSR.USERID -> USER_CODE, which the portal-user importer needs."""

    def test_ids_are_bound_and_mapped_to_codes(self):
        from .hana.sap_user_reader import HanaSapUserReader

        reader = HanaSapUserReader(_context())
        cursor = MagicMock()
        cursor.fetchall.return_value = [(12, "user12 ", "Asha"), (30, "USER30", None)]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        with patch.object(reader.connection, "connect", return_value=conn):
            codes = reader.user_codes_by_id([30, "12", None, 12])
        sql, params = cursor.execute.call_args[0]
        self.assertEqual(params, (12, 30))
        self.assertIn('"SCHEMA"."OUSR"', sql)
        self.assertEqual(codes, {12: {"user_code": "user12", "user_name": "Asha"}, 30: {"user_code": "USER30", "user_name": ""}})

    def test_no_ids_asks_nothing(self):
        from .hana.sap_user_reader import HanaSapUserReader

        reader = HanaSapUserReader(_context())
        with patch.object(reader.connection, "connect") as connect:
            self.assertEqual(reader.user_codes_by_id([None]), {})
        connect.assert_not_called()
