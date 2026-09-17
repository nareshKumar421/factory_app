"""
universal_search/tests.py

Tests for the universal search.

Nothing here touches SAP: ``UniversalSapReader`` is mocked everywhere, and the
one place its SQL matters -- the fifteen-way union -- is checked by reading the
statement the reader builds rather than by running it.

Covered:
  1. terms            -- what is worth searching for and what is not
  2. companies        -- who may be searched, and in what order
  3. app records      -- company scoping, permission filtering, matched-on
  4. SQL shape        -- one branch per document type, one bind per branch
  5. degradation      -- one company's SAP failing does not lose the others
  6. API views        -- auth, permission, the detail endpoints' guards
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError
from warehouse.models_bst import BSTTransfer
from warehouse.models_transfer import WarehouseTransferRequest

from universal_search.documents import DOC_TYPES, status_label, DOC_TYPES_BY_KIND
from universal_search.models import UniversalSearchPermission
from universal_search.services import search as search_service
from universal_search.services.app_lookup import search_app_records
from universal_search.services.sap_lookup import UniversalSapReader, is_doc_num

SEARCH_URL = "/api/v1/universal-search/search/"
DOCUMENT_URL = "/api/v1/universal-search/document/"
ITEM_STOCK_URL = "/api/v1/universal-search/item-stock/"


# ---------------------------------------------------------------------------
# 1. Terms
# ---------------------------------------------------------------------------


class TestTerms(TestCase):

    def test_a_term_is_trimmed(self):
        self.assertEqual(search_service.clean_term("  626090411 "), "626090411")

    def test_one_character_is_refused(self):
        with self.assertRaises(search_service.SearchTermError):
            search_service.clean_term("7")

    def test_an_empty_term_is_refused(self):
        with self.assertRaises(search_service.SearchTermError):
            search_service.clean_term("   ")

    def test_a_pasted_essay_is_refused(self):
        with self.assertRaises(search_service.SearchTermError):
            search_service.clean_term("X" * 200)

    def test_a_plain_number_is_a_doc_num(self):
        self.assertTrue(is_doc_num("626090411"))

    def test_an_item_code_is_not_a_doc_num(self):
        self.assertFalse(is_doc_num("FG-1LTR-OLIVE"))

    def test_a_number_too_big_for_sap_is_not_a_doc_num(self):
        # SAP keeps DocNum in a 32-bit column; a longer run of digits is a
        # barcode, and asking about it would cost a round trip for nothing.
        self.assertFalse(is_doc_num("89012345678901234567"))

    def test_zero_is_not_a_doc_num(self):
        self.assertFalse(is_doc_num("0"))


# ---------------------------------------------------------------------------
# 2. Companies
# ---------------------------------------------------------------------------


class TestSearchableCompanies(TestCase):

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="searcher@test.com",
            password="testpass123",
            full_name="Searcher",
            employee_code="EMP900",
        )
        self.role = UserRole.objects.create(name="Searcher")
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")

    def _join(self, company):
        UserCompany.objects.create(
            user=self.user, company=company, role=self.role, is_active=True
        )

    def test_only_the_companies_the_user_belongs_to_are_searched(self):
        self._join(self.oil)
        self._join(self.mart)

        codes = [
            company.code
            for company in search_service.searchable_companies(self.user, "JIVO_OIL")
        ]

        self.assertEqual(sorted(codes), ["JIVO_MART", "JIVO_OIL"])

    def test_the_current_company_is_searched_first(self):
        self._join(self.oil)
        self._join(self.mart)
        self._join(self.bev)

        codes = [
            company.code
            for company in search_service.searchable_companies(self.user, "JIVO_MART")
        ]

        self.assertEqual(codes[0], "JIVO_MART")

    def test_an_inactive_membership_is_not_searched(self):
        self._join(self.oil)
        UserCompany.objects.create(
            user=self.user, company=self.mart, role=self.role, is_active=False
        )

        codes = [
            company.code
            for company in search_service.searchable_companies(self.user, "JIVO_OIL")
        ]

        self.assertEqual(codes, ["JIVO_OIL"])


# ---------------------------------------------------------------------------
# 3. App records
# ---------------------------------------------------------------------------


class AppRecordTestCase(TestCase):
    """A transfer request and a BST in Oil, and the same numbers in Mart."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="warehouse@test.com",
            password="testpass123",
            full_name="Warehouse",
            employee_code="EMP901",
        )
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")

        self.request = WarehouseTransferRequest.objects.create(
            company=self.oil,
            entry_no="TR-0001",
            from_warehouse="BH-PC",
            to_warehouse="BH-BT",
            sap_transfer_doc_num="4455",
        )
        self.bst = BSTTransfer.objects.create(
            company=self.oil,
            entry_no="BST-0001",
            sap_doc_num="4455",
            sap_from_warehouse="BH-BT",
            sap_to_warehouse="DP-HR",
        )
        # The same number in another company, which must never be mixed in.
        BSTTransfer.objects.create(
            company=self.mart,
            entry_no="BST-9001",
            sap_doc_num="4455",
            sap_from_warehouse="MT-01",
            sap_to_warehouse="MT-02",
        )

    def grant(self, *codenames):
        for codename in codenames:
            app_label, name = codename.split(".")
            permission = Permission.objects.get(codename=name)
            self.user.user_permissions.add(permission)
        self.user = get_user_model().objects.get(pk=self.user.pk)


class TestAppRecordSearch(AppRecordTestCase):

    def test_a_number_finds_every_record_that_carries_it(self):
        self.grant(
            "warehouse.view_warehousetransferrequest", "warehouse.view_bsttransfer"
        )

        hits = search_app_records("4455", company=self.oil, user=self.user)

        self.assertEqual(
            sorted(hit["entry_no"] for hit in hits), ["BST-0001", "TR-0001"]
        )

    def test_a_sibling_company_record_is_not_returned(self):
        self.grant("warehouse.view_bsttransfer")

        hits = search_app_records("4455", company=self.oil, user=self.user)

        self.assertNotIn("BST-9001", [hit["entry_no"] for hit in hits])

    def test_a_source_the_user_cannot_view_is_left_out(self):
        # The whole point of the per-result filter: rights on one module do not
        # open another.
        self.grant("warehouse.view_warehousetransferrequest")

        hits = search_app_records("4455", company=self.oil, user=self.user)

        self.assertEqual([hit["kind"] for hit in hits], ["TRANSFER_REQUEST"])

    def test_an_entry_number_finds_its_own_record(self):
        self.grant("warehouse.view_bsttransfer")

        hits = search_app_records("BST-0001", company=self.oil, user=self.user)

        self.assertEqual([hit["entry_no"] for hit in hits], ["BST-0001"])

    def test_a_hit_says_which_field_matched(self):
        self.grant("warehouse.view_warehousetransferrequest")

        hits = search_app_records("4455", company=self.oil, user=self.user)

        self.assertEqual(hits[0]["matched_on"], "Inventory transfer")

    def test_a_hit_carries_a_route_to_the_record(self):
        self.grant("warehouse.view_bsttransfer")

        hits = search_app_records("BST-0001", company=self.oil, user=self.user)

        self.assertEqual(hits[0]["route"], f"/warehouse/bst/{self.bst.id}")

    def test_a_non_numeric_term_does_not_break_integer_columns(self):
        # Comparing "FG-OLIVE" with an IntegerField raises in Django, so those
        # fields must simply not be asked.
        self.grant("warehouse.view_bsttransfer")

        hits = search_app_records("FG-OLIVE", company=self.oil, user=self.user)

        self.assertEqual(hits, [])

    def test_nothing_comes_back_with_no_permissions_at_all(self):
        hits = search_app_records("4455", company=self.oil, user=self.user)

        self.assertEqual(hits, [])


# ---------------------------------------------------------------------------
# 4. The SQL the reader builds
# ---------------------------------------------------------------------------


class TestDocumentSql(TestCase):

    def _reader(self):
        context = MagicMock()
        context.hana = {"schema": "JIVO_OIL_HANADB"}
        return UniversalSapReader(context)

    def test_every_document_type_is_asked(self):
        reader = self._reader()
        with patch.object(UniversalSapReader, "_read", return_value=[]) as read:
            reader.search_documents(1001)

        sql, params = read.call_args[0]
        for doc in DOC_TYPES:
            self.assertIn(f'"JIVO_OIL_HANADB"."{doc.header}"', sql)
        self.assertEqual(sql.count("UNION ALL"), len(DOC_TYPES) - 1)

    def test_the_number_is_bound_once_per_branch(self):
        reader = self._reader()
        with patch.object(UniversalSapReader, "_read", return_value=[]) as read:
            reader.search_documents(1001)

        _, params = read.call_args[0]
        self.assertEqual(params, [1001] * len(DOC_TYPES))
        # Bound, never interpolated.
        self.assertNotIn("1001", read.call_args[0][0])

    def test_the_detail_is_fetched_by_doc_entry_not_doc_num(self):
        # DocNum repeats across numbering series; DocEntry is the actual key.
        reader = self._reader()
        with patch.object(UniversalSapReader, "_read", return_value=[]) as read:
            reader.document_detail("AR_INVOICE", 80341)

        sql, params = read.call_args[0]
        self.assertIn('T."DocEntry" = ?', sql)
        self.assertNotIn('T."DocNum" = ?', sql)
        self.assertEqual(params, [80341])

    def test_an_unknown_document_type_is_not_queried(self):
        reader = self._reader()
        with patch.object(UniversalSapReader, "_read") as read:
            self.assertIsNone(reader.document_detail("NOT_A_DOC", 1))
        read.assert_not_called()

    def test_a_missing_document_reads_as_missing(self):
        reader = self._reader()
        with patch.object(UniversalSapReader, "_read", return_value=[]):
            self.assertIsNone(reader.document_detail("AR_INVOICE", 80341))

    def test_a_session_opens_one_connection_for_several_reads(self):
        # Opening a connection costs about half as much as the query it
        # carries, and a search asks each company three questions.
        reader = self._reader()
        connection = MagicMock()
        with patch.object(UniversalSapReader, "_connect", return_value=connection) as connect:
            with reader.session():
                reader.search_documents(1001)
                reader.search_items("FG-OLIVE")
                reader.search_batches("L1")

        self.assertEqual(connect.call_count, 1)
        connection.close.assert_called_once()

    def test_a_read_outside_a_session_closes_its_own_connection(self):
        reader = self._reader()
        connection = MagicMock()
        with patch.object(UniversalSapReader, "_connect", return_value=connection):
            reader.search_items("FG-OLIVE")

        connection.close.assert_called_once()


class TestStatusLabels(TestCase):

    def test_a_marketing_document_status_is_spelled_out(self):
        self.assertEqual(status_label(DOC_TYPES_BY_KIND["AR_INVOICE"], "O"), "Open")

    def test_a_production_order_has_its_own_codes(self):
        # 'L' is Closed on a production order and means nothing on an invoice.
        self.assertEqual(
            status_label(DOC_TYPES_BY_KIND["PRODUCTION_ORDER"], "L"), "Closed"
        )

    def test_an_unknown_code_comes_back_as_itself(self):
        self.assertEqual(status_label(DOC_TYPES_BY_KIND["AR_INVOICE"], "Z"), "Z")

    def test_no_code_is_blank(self):
        self.assertEqual(status_label(DOC_TYPES_BY_KIND["AR_INVOICE"], ""), "")


# ---------------------------------------------------------------------------
# 5. One company's SAP failing
# ---------------------------------------------------------------------------


class TestDegradation(AppRecordTestCase):

    def setUp(self):
        super().setUp()
        role = UserRole.objects.create(name="Searcher")
        for company in (self.oil, self.mart):
            UserCompany.objects.create(
                user=self.user, company=company, role=role, is_active=True
            )
        self.grant("warehouse.view_bsttransfer")

    @patch("universal_search.services.search.UniversalSapReader")
    def test_a_company_whose_sap_is_down_still_reports_its_app_records(self, reader):
        reader.return_value.search_documents.side_effect = SAPConnectionError("down")
        reader.return_value.search_items.side_effect = SAPConnectionError("down")
        reader.return_value.search_batches.side_effect = SAPConnectionError("down")

        result = search_service.search(
            "4455", user=self.user, current_company_code="JIVO_OIL"
        )

        oil = next(c for c in result["companies"] if c["company_code"] == "JIVO_OIL")
        self.assertTrue(oil["error"])
        self.assertEqual([hit["entry_no"] for hit in oil["app_records"]], ["BST-0001"])

    @patch("universal_search.services.search.UniversalSapReader")
    def test_every_company_is_asked(self, reader):
        reader.return_value.search_documents.return_value = []
        reader.return_value.search_items.return_value = []
        reader.return_value.search_batches.return_value = []

        result = search_service.search(
            "4455", user=self.user, current_company_code="JIVO_OIL"
        )

        self.assertEqual(
            [c["company_code"] for c in result["companies"]],
            ["JIVO_OIL", "JIVO_MART"],
        )

    @patch("universal_search.services.search.UniversalSapReader")
    def test_the_total_counts_both_halves_of_the_answer(self, reader):
        reader.return_value.search_documents.return_value = [{"kind": "AR_INVOICE"}]
        reader.return_value.search_items.return_value = []
        reader.return_value.search_batches.return_value = []

        result = search_service.search(
            "4455", user=self.user, current_company_code="JIVO_OIL"
        )

        # The same 4455 is a document in both companies and a BST in both --
        # which is the whole reason the search spans them instead of guessing.
        self.assertEqual(result["total"], 4)
        self.assertEqual(
            [company["total"] for company in result["companies"]], [2, 2]
        )

    @patch("universal_search.services.search.UniversalSapReader")
    def test_an_item_code_never_asks_for_a_document(self, reader):
        reader.return_value.search_items.return_value = []
        reader.return_value.search_batches.return_value = []

        search_service.search(
            "FG-OLIVE", user=self.user, current_company_code="JIVO_OIL"
        )

        reader.return_value.search_documents.assert_not_called()


# ---------------------------------------------------------------------------
# 6. The API
# ---------------------------------------------------------------------------


class UniversalSearchAPITestCase(APITestCase):

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="user@test.com",
            password="testpass123",
            full_name="User",
            employee_code="EMP902",
        )
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="User")
        UserCompany.objects.create(
            user=self.user, company=self.oil, role=role, is_default=True, is_active=True
        )

        self.client = APIClient()
        self._authenticate()

    def _authenticate(self):
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}",
            HTTP_COMPANY_CODE="JIVO_OIL",
        )

    def grant_search(self):
        content_type = ContentType.objects.get_for_model(UniversalSearchPermission)
        permission, _ = Permission.objects.get_or_create(
            codename="can_use_universal_search",
            content_type=content_type,
            defaults={"name": "Can use universal search"},
        )
        self.user.user_permissions.add(permission)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self._authenticate()


class TestSearchAPI(UniversalSearchAPITestCase):

    def test_anonymous_access_is_refused(self):
        client = APIClient()
        self.assertEqual(
            client.get(SEARCH_URL, {"q": "4455"}).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_a_user_without_the_permission_is_refused(self):
        self.assertEqual(
            self.client.get(SEARCH_URL, {"q": "4455"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_short_term_is_refused_with_an_explanation(self):
        self.grant_search()

        response = self.client.get(SEARCH_URL, {"q": "4"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("at least", response.data["detail"])

    @patch("universal_search.services.search.UniversalSapReader")
    def test_a_search_answers_per_company(self, reader):
        reader.return_value.search_documents.return_value = []
        reader.return_value.search_items.return_value = []
        reader.return_value.search_batches.return_value = []
        self.grant_search()

        response = self.client.get(SEARCH_URL, {"q": "4455"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["term"], "4455")
        self.assertEqual(
            [c["company_code"] for c in response.data["companies"]], ["JIVO_OIL"]
        )


class TestDocumentAPI(UniversalSearchAPITestCase):

    def test_an_unknown_document_type_is_refused(self):
        self.grant_search()

        response = self.client.get(
            DOCUMENT_URL, {"kind": "NOT_A_DOC", "doc_entry": "1"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_missing_key_is_refused(self):
        self.grant_search()

        response = self.client.get(DOCUMENT_URL, {"kind": "AR_INVOICE"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_company_the_user_does_not_belong_to_is_refused(self):
        # The search spans companies, so the detail cannot simply trust the
        # company it is handed.
        self.grant_search()

        response = self.client.get(
            DOCUMENT_URL,
            {"kind": "AR_INVOICE", "doc_entry": "1", "company": "JIVO_MART"},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("universal_search.services.search.UniversalSapReader")
    def test_a_document_that_is_gone_reads_as_gone(self, reader):
        reader.return_value.document_detail.return_value = None
        self.grant_search()

        response = self.client.get(
            DOCUMENT_URL, {"kind": "AR_INVOICE", "doc_entry": "80341"}
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("universal_search.services.search.UniversalSapReader")
    def test_a_document_comes_back_with_its_lines(self, reader):
        reader.return_value.document_detail.return_value = {
            "kind": "AR_INVOICE",
            "doc_num": 626090411,
            "lines": [{"item_code": "FG-OLIVE", "quantity": 12.0}],
        }
        self.grant_search()

        response = self.client.get(
            DOCUMENT_URL, {"kind": "AR_INVOICE", "doc_entry": "80341"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["document"]["lines"]), 1)


class TestItemStockAPI(UniversalSearchAPITestCase):

    def test_a_missing_item_code_is_refused(self):
        self.grant_search()

        self.assertEqual(
            self.client.get(ITEM_STOCK_URL).status_code, status.HTTP_400_BAD_REQUEST
        )

    @patch("universal_search.services.search.UniversalSapReader")
    def test_stock_comes_back_per_warehouse(self, reader):
        reader.return_value.item_stock.return_value = [
            {"warehouse": "BH-BT", "on_hand": 120.0}
        ]
        self.grant_search()

        response = self.client.get(ITEM_STOCK_URL, {"item_code": "FG-OLIVE"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["warehouses"][0]["warehouse"], "BH-BT")
