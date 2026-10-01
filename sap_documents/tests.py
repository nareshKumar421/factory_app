"""
SAP Documents through the real permission stack.

    python manage.py test sap_documents --settings=config.sqlite_test_settings

Every request goes through IsAuthenticated + HasCompanyContext + the app's own
right(s), so these also pin a missing Company-Code header and each right. SAP is
mocked where the service looks ``SAPClient`` up (``sap_documents.services``);
the HANA reader itself is tested in ``sap_client.tests_document_reader``.
"""

from datetime import date
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from . import permissions as guards
from . import services
from .constants import DOCUMENT_TYPES
from .management.commands.setup_sap_documents_groups import SAP_DOCUMENTS_GROUPS
from .models import SapAttachmentDownload

BASE = "/api/v1/sap-documents/"
ALL_PERMISSIONS = ["can_view_sap_documents", "can_download_sap_attachments"]
SAP = "sap_documents.services.SAPClient"

# The portal's DOC_TYPES whitelist, routes/sap.js:1402-1425.
PORTAL_TYPES = {
    "Drafts", "PurchaseOrders", "PurchaseDeliveryNotes", "PurchaseInvoices", "PurchaseCreditNotes",
    "PurchaseReturns", "Invoices", "CreditNotes", "Returns", "StockTransfers", "InventoryTransferRequests",
    "JournalEntries", "VendorPayments", "PaymentDrafts",
}


def empty_lookups(**overrides):
    lookups = services._empty_lookups()
    lookups.update(overrides)
    return lookups


# An A/P invoice as the Service Layer returns it, trimmed to what matters.
AP_INVOICE = {
    "DocEntry": 5001,
    "DocNum": 626074136,
    "DocObjectCode": "oPurchaseInvoices",
    "DocDate": "2026-07-03T00:00:00Z",
    "DocDueDate": "2026-08-02T00:00:00Z",
    "TaxDate": "2026-07-01T00:00:00Z",
    "CardCode": "VENDA000010",
    "CardName": "SHIV SHAMBHU TRADERS",
    "NumAtCard": "1825",
    "ShipToCode": "DELHI",
    "PayToCode": "MP OFFICE",
    "SalesPersonCode": 2,
    "PaymentGroupCode": 3,
    "TransportationCode": -1,
    "BPL_IDAssignedToInvoice": 1,
    "BPLName": "Jivo Wellness - HR",
    "VATRegNum": "06AAAAA0000A1Z5",
    "TaxExtension": {"TaxId0": "BBBBB1111B"},
    "DocCurrency": "INR",
    "DocTotal": 1660.0,
    "VatSum": 180.0,
    "WTAmount": 20.0,
    "RoundingDiffAmount": 0.0,
    "DocumentStatus": "bost_Open",
    "Cancelled": "tNO",
    "DocumentSubType": "bod_None",
    "AttachmentEntry": 165165,
    "Comments": "Freight July",
    "DocumentLines": [
        {"LineNum": 0, "ItemCode": "RM001", "ItemDescription": "Crude Oil", "Quantity": 10, "UnitPrice": 100,
         "LineTotal": 1000, "AccountCode": "5010101", "WarehouseCode": "BH-FG", "CostingCode": "DL",
         "LocationCode": 5, "TaxCode": "IGST18", "BaseType": 20, "BaseEntry": 7001},
        {"LineNum": 1, "ItemCode": "", "ItemDescription": "Freight", "Quantity": 0, "LineTotal": 500,
         "AccountCode": "", "WarehouseCode": "BH-FG", "SACEntry": -482, "BaseType": -1},
    ],
}

AP_LOOKUPS = empty_lookups(
    line_rows=[
        {"LineNum": 0, "AcctCode": "5010101", "BaseRef": "626070001"},
        {"LineNum": 1, "AcctCode": "5010102", "U_Recvd_Qty": 12, "U_UNE_LTS": 0},
    ],
    header={"paid_to_date": 400, "gross_profit": 0, "trans_id": 90001, "obj_type": "18"},
    tds=[{"code": "C2", "rate": 2, "amount": 20, "taxable": 1000, "name": "Contractor 194C", "section": ""}],
    accounts={"5010101": "Purchase - Oil", "5010102": "Freight Inward"},
    sac={"-482": {"code": "00996519", "name": "Goods transport"}},
    dimensions={"DL": "Delhi"},
    locations={"5": "DELHI ISD"},
    header_names={"sales_person": "KAMALDEEP SINGH", "payment_terms": "30 DAYS"},
    warehouses={"BH-FG": {"code": "BH-FG", "name": "Bahadurgarh FG", "gstin": "06AAAAA0000A1Z5",
                          "branch": "Jivo Wellness - HR", "state": "HR", "address": "Plot 1"}},
    partners=[{
        "card_code": "VENDA000010", "card_name": "SHIV SHAMBHU TRADERS", "card_type": "S", "valid_for": "Y",
        "lic_trad_num": "",
        "addresses": [
            {"address": "MP OFFICE", "gstin": "23BBBBB1111B1Z1", "state": "MP", "street": "", "block": "",
             "city": "Indore", "zip": "", "country": "IN"},
            {"address": "DELHI", "gstin": "07CCCCC2222C1Z2", "state": "DL", "street": "Naraina", "block": "",
             "city": "Delhi", "zip": "110028", "country": "IN"},
        ],
    }],
    base_documents=[{"base_type": "20", "base_entry": 7001, "type_label": "Goods Receipt PO", "doc_num": 626070001,
                     "doc_date": "2026-07-01", "base_ref": "626070001", "attachment_entry": 88831, "trans_id": 80001}],
    journal_entry={"trans_id": 90001, "number": 1234, "lines": []},
    in_transit_journal_entries=[{"trans_id": 80001, "number": 1200, "lines": []}],
)


class SapDocumentsTestCase(APITestCase):
    """One company, one user, and whatever rights a test grants."""

    permissions = ["can_view_sap_documents"]

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="sap_documents@example.com",
            password="testpass",
            full_name="SAP Documents User",
            employee_code="SDOC001",
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Staff")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}
        self.grant(*self.permissions)

    def grant(self, *codenames):
        """Add rights, then re-fetch the user: has_perm() caches per instance."""
        if codenames:
            self.user.user_permissions.add(
                *Permission.objects.filter(
                    content_type__app_label="sap_documents", codename__in=codenames
                )
            )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def get(self, path, **extra):
        return self.client.get(f"{BASE}{path}", **self.headers, **extra)


# ---------------------------------------------------------------------------
# Types and lists
# ---------------------------------------------------------------------------


class DocumentTypeTests(SapDocumentsTestCase):
    def test_the_whitelist_is_the_portals_fourteen(self):
        response = self.get("types/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({row["key"] for row in response.data}, PORTAL_TYPES)
        by_key = {row["key"]: row for row in response.data}
        self.assertEqual(by_key["PurchaseDeliveryNotes"]["label"], "GRPO")
        self.assertEqual(
            [s["value"] for s in by_key["Invoices"]["filters"]["statuses"]], ["O", "C", "L"]
        )
        self.assertFalse(by_key["JournalEntries"]["filters"]["partner"])
        self.assertEqual(by_key["StockTransfers"]["filters"]["statuses"], [])

    @patch(SAP)
    def test_an_unknown_type_is_refused_before_sap(self, sap):
        for path in ("documents/Items/", "documents/Items/1/", "documents/BusinessPartners/"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("Unknown document type", response.data["detail"])
        sap.assert_not_called()


@patch(SAP)
class DocumentListTests(SapDocumentsTestCase):
    def test_the_filters_reach_the_service_layer(self, sap):
        sap.return_value.list_sap_documents.return_value = [
            {"DocEntry": 5001, "DocNum": 626074136, "DocDate": "2026-07-03T00:00:00Z", "CardCode": "VENDA000010",
             "CardName": "SHIV", "DocTotal": 1660, "DocumentStatus": "bost_Close", "Cancelled": "tYES",
             "AttachmentEntry": 165165},
        ]
        response = self.get(
            "documents/PurchaseInvoices/?number=626074136&partner=o'brien&date_from=2026-07-01"
            "&date_to=2026-07-31&status=C&top=50&skip=100"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        sap.assert_called_once_with(company_code="JIVO_OIL")
        args, kwargs = sap.return_value.list_sap_documents.call_args
        self.assertEqual(args, ("PurchaseInvoices",))
        self.assertEqual(kwargs["orderby"], "DocEntry desc")
        self.assertEqual((kwargs["top"], kwargs["skip"]), (50, 100))
        self.assertIn("AttachmentEntry", kwargs["select"])
        self.assertEqual(
            kwargs["filter"],
            "DocNum eq 626074136 and (contains(CardCode,'o''brien') or contains(CardCode,'O''BRIEN') "
            "or contains(CardName,'o''brien') or contains(CardName,'O''BRIEN')) "
            "and DocDate ge '2026-07-01' and DocDate le '2026-07-31' "
            "and DocumentStatus eq 'bost_Close' and Cancelled eq 'tNO'",
        )
        row = response.data["results"][0]
        self.assertEqual(
            (row["doc_entry"], row["doc_date"], row["status"], row["attachment_entry"]),
            (5001, "2026-07-03", "cancelled", 165165),
        )
        self.assertFalse(response.data["has_more"])

    def test_journal_entries_filter_on_their_own_fields(self, sap):
        sap.return_value.list_sap_documents.return_value = [
            {"JdtNum": 90001, "Number": 1234, "ReferenceDate": "2026-07-03", "Memo": "AP Inv", "Reference": "1825"}
        ]
        response = self.get("documents/JournalEntries/?number=1234&date_from=2026-07-01")
        kwargs = sap.return_value.list_sap_documents.call_args.kwargs
        self.assertEqual(kwargs["filter"], "Number eq 1234 and ReferenceDate ge '2026-07-01'")
        self.assertEqual(kwargs["orderby"], "JdtNum desc")
        self.assertEqual(response.data["results"][0]["doc_entry"], 90001)

    def test_cancelled_means_cancelled_not_the_portals_bost_cancel(self, sap):
        sap.return_value.list_sap_documents.return_value = []
        self.get("documents/Invoices/?status=L")
        self.assertEqual(sap.return_value.list_sap_documents.call_args.kwargs["filter"], "Cancelled eq 'tYES'")

    def test_bad_filters_are_refused_before_sap(self, sap):
        for query in (
            "documents/Invoices/?date_from=2026-08-01&date_to=2026-07-01",
            "documents/StockTransfers/?status=O",
            "documents/Drafts/?status=L",
            "documents/JournalEntries/?partner=acme",
            "documents/Invoices/?top=500",
            "documents/Invoices/?number=abc",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.get(query).status_code, status.HTTP_400_BAD_REQUEST)
        sap.return_value.list_sap_documents.assert_not_called()

    def test_a_full_page_offers_more(self, sap):
        sap.return_value.list_sap_documents.return_value = [{"DocEntry": n} for n in range(20)]
        self.assertTrue(self.get("documents/Invoices/").data["has_more"])

    def test_transfers_show_the_route_and_payments_their_parts(self, sap):
        sap.return_value.list_sap_documents.return_value = [
            {"DocEntry": 1, "FromWarehouse": "BH-RM", "ToWarehouse": "BH-FG", "CashSum": 100, "TransferSum": 50.5,
             "AuthorizationStatus": "pasPending"}
        ]
        transfer = self.get("documents/StockTransfers/").data["results"][0]
        self.assertEqual((transfer["from_warehouse"], transfer["to_warehouse"]), ("BH-RM", "BH-FG"))
        payment = self.get("documents/PaymentDrafts/").data["results"][0]
        self.assertEqual((payment["total"], payment["approval_status"]), (150.5, "Pending approval"))

    def test_sap_errors_map_to_400_503_502(self, sap):
        for error, expected in (
            (SAPValidationError("Invalid property 'X'"), status.HTTP_400_BAD_REQUEST),
            (SAPConnectionError("down"), status.HTTP_503_SERVICE_UNAVAILABLE),
            (SAPDataError("broken"), status.HTTP_502_BAD_GATEWAY),
        ):
            with self.subTest(error=type(error).__name__):
                sap.return_value.list_sap_documents.side_effect = error
                self.assertEqual(self.get("documents/Invoices/").status_code, expected)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@patch(SAP)
class DocumentDetailTests(SapDocumentsTestCase):
    def open_ap_invoice(self, sap, lookups=AP_LOOKUPS):
        sap.return_value.get_sap_document.return_value = AP_INVOICE
        sap.return_value.sap_document_lookups.return_value = lookups
        response = self.get("documents/PurchaseInvoices/5001/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_lines_are_backfilled_from_hana_and_named(self, sap):
        doc = self.open_ap_invoice(sap)
        first, freight = doc["lines"]
        self.assertEqual((first["account_code"], first["account_name"]), ("5010101", "Purchase - Oil"))
        self.assertEqual(first["warehouse_name"], "Bahadurgarh FG")
        self.assertEqual(first["location_name"], "DELHI ISD")
        self.assertEqual(first["dimensions"][0], {"code": "DL", "name": "Delhi"})
        self.assertEqual((first["base_label"], first["base_ref"]), ("Goods Receipt PO", "626070001"))
        # The Service Layer left the freight line's account and UDFs out.
        self.assertEqual((freight["account_code"], freight["account_name"]), ("5010102", "Freight Inward"))
        self.assertEqual((freight["sac_code"], freight["received_qty"]), ("00996519", 12.0))

    def test_the_header_names_codes_and_attributes_gstins_honestly(self, sap):
        header = self.open_ap_invoice(sap)["header"]
        self.assertEqual(header["party_role"], "vendor")
        self.assertEqual((header["sales_person"], header["payment_terms"]), ("KAMALDEEP SINGH", "30 DAYS"))
        # The ship-to address's GSTIN, never our branch's (VATRegNum).
        self.assertEqual(header["party_gstin"], "07CCCCC2222C1Z2")
        self.assertEqual(header["branch_gstin"], "06AAAAA0000A1Z5")
        self.assertEqual(header["party_pan"], "BBBBB1111B")
        self.assertEqual(header["subtype"], "")
        self.assertEqual(header["doc_date"], "2026-07-03")

    def test_a_purchase_ships_from_the_vendor_address_the_document_names(self, sap):
        ship_from = self.open_ap_invoice(sap)["ship_from"]
        # DELHI (the document's ship-to code), not the MP address first in CRD1 (5b5f762).
        self.assertEqual(
            ship_from,
            [{"code": "SHIV SHAMBHU TRADERS", "name": "", "gstin": "07CCCCC2222C1Z2", "branch": "", "state": "DL",
              "address": "Naraina, Delhi, DL, 110028, IN"}],
        )

    def test_totals_settlement_tds_base_documents_and_journals(self, sap):
        doc = self.open_ap_invoice(sap)
        self.assertEqual(doc["totals"]["net"], 1500.0)  # 1660 - 180 + 20 withheld
        self.assertEqual((doc["totals"]["paid_to_date"], doc["totals"]["balance_due"]), (400.0, 1260.0))
        self.assertEqual(doc["tds_section"], "194C")
        self.assertEqual(doc["base_documents"][0]["attachment_entry"], 88831)
        self.assertNotIn("trans_id", doc["base_documents"][0])
        self.assertEqual(doc["attachment_entry"], 165165)
        self.assertEqual(doc["journal_entry"]["trans_id"], 90001)
        self.assertEqual(doc["in_transit_journal_entries"][0]["trans_id"], 80001)
        self.assertEqual(doc["warnings"], [])

    def test_hana_is_asked_for_every_code_on_the_document(self, sap):
        self.open_ap_invoice(sap)
        request = sap.return_value.sap_document_lookups.call_args.kwargs
        self.assertEqual(request["line_table"], "PCH1")
        self.assertEqual(request["tds_table"], "PCH5")
        self.assertIn("5010101", request["account_codes"])
        self.assertIn(-482, request["sac_entries"])
        self.assertEqual(request["base_refs"], [("20", 7001, "")])
        self.assertEqual(request["in_transit_base_type"], "20")
        self.assertEqual(request["branch_ids"], [])  # the document names its branch

    def test_a_missing_document_is_404(self, sap):
        sap.return_value.get_sap_document.return_value = None
        self.assertEqual(self.get("documents/Invoices/99/").status_code, status.HTTP_404_NOT_FOUND)

    def test_without_hana_the_document_still_opens_with_a_warning(self, sap):
        sap.return_value.get_sap_document.return_value = AP_INVOICE
        sap.return_value.sap_document_lookups.side_effect = SAPConnectionError("down")
        response = self.get("documents/PurchaseInvoices/5001/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["lines"][0]["account_code"], "5010101")
        self.assertEqual(response.data["lines"][0]["account_name"], "")
        self.assertIn("HANA could not be read", response.data["warnings"][0])

    def test_a_skipped_lookup_is_named(self, sap):
        doc = self.open_ap_invoice(sap, empty_lookups(warnings=["warehouse names"]))
        self.assertEqual(doc["warnings"], ["Could not read the warehouse names from SAP."])

    def test_service_layer_errors_map(self, sap):
        sap.return_value.get_sap_document.side_effect = SAPConnectionError("down")
        self.assertEqual(self.get("documents/Invoices/1/").status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_a_draft_names_what_it_will_become_and_owes_nothing(self, sap):
        sap.return_value.get_sap_document.return_value = {
            "DocEntry": 51165, "DocNum": 51165, "DocObjectCode": "oPurchaseInvoices", "CardCode": "",
            "CardName": "UFLEX LIMITED", "DocTotal": 1000, "DocumentStatus": "bost_Open", "DocumentLines": [],
        }
        sap.return_value.sap_document_lookups.return_value = empty_lookups(
            header={"paid_to_date": 0, "obj_type": "18"},
            partners=[
                {"card_code": "CUSTA1", "card_name": "UFLEX LIMITED", "card_type": "C", "valid_for": "Y",
                 "lic_trad_num": "", "addresses": []},
                {"card_code": "VENDA9", "card_name": "UFLEX LIMITED", "card_type": "S", "valid_for": "Y",
                 "lic_trad_num": "", "addresses": [{"address": "HQ", "gstin": "09UUUUU", "state": "UP"}]},
            ],
            journal_preview={"preview": True, "lines": []},
        )
        doc = self.get("documents/Drafts/51165/").data
        self.assertEqual(doc["kind"], "draft")
        self.assertEqual(doc["header"]["object_label"], "AP Invoice")
        self.assertEqual(doc["header"]["party_role"], "vendor")
        self.assertIsNone(doc["totals"]["balance_due"])
        self.assertTrue(doc["journal_preview"]["preview"])
        # No CardCode on the draft: the vendor of that exact name is the ship-from.
        self.assertEqual(doc["ship_from"][0]["gstin"], "09UUUUU")
        request = sap.return_value.sap_document_lookups.call_args.kwargs
        self.assertTrue(request["draft"])
        self.assertEqual((request["card_code"], request["card_name"]), ("", "UFLEX LIMITED"))

    def test_a_journal_entry_opens_with_its_lines(self, sap):
        sap.return_value.get_sap_document.return_value = {
            "JdtNum": 90001, "Number": 1234, "ReferenceDate": "2026-07-03", "Memo": "Rent",
            "JournalEntryLines": [{"AccountCode": "6101", "Debit": 100, "Credit": 0},
                                  {"AccountCode": "1102", "Debit": 0, "Credit": 100}],
        }
        sap.return_value.sap_document_lookups.side_effect = SAPConnectionError("down")
        doc = self.get("documents/JournalEntries/90001/").data
        self.assertEqual(doc["header"]["doc_num"], 1234)
        # Without HANA the Service Layer's own lines stand in.
        self.assertEqual(doc["journal_entry"]["total_debit"], 100.0)
        sap.return_value.sap_document_lookups.side_effect = None
        sap.return_value.sap_document_lookups.return_value = empty_lookups(journal_entry={"trans_id": 90001})
        self.get("documents/JournalEntries/90001/")
        self.assertEqual(sap.return_value.sap_document_lookups.call_args.kwargs["journal_trans_ids"], [90001])

    def test_a_transfer_ships_from_its_from_warehouse(self, sap):
        sap.return_value.get_sap_document.return_value = {
            "DocEntry": 77, "DocNum": 77, "FromWarehouse": "BH-RM", "ToWarehouse": "BH-FG",
            "StockTransferLines": [{"LineNum": 0, "ItemCode": "RM1", "Quantity": 5, "WarehouseCode": "BH-FG",
                                    "FromWarehouseCode": "BH-RM"}],
        }
        sap.return_value.sap_document_lookups.return_value = empty_lookups(
            warehouses={"BH-RM": {"code": "BH-RM", "name": "Raw Material", "gstin": "", "branch": "", "state": "",
                                  "address": ""},
                        "BH-FG": {"code": "BH-FG", "name": "Finished Goods", "gstin": "", "branch": "", "state": "",
                                  "address": ""}},
        )
        doc = self.get("documents/StockTransfers/77/").data
        self.assertEqual([s["code"] for s in doc["ship_from"]], ["BH-RM"])
        self.assertEqual(doc["header"]["to_warehouse_name"], "Finished Goods")
        self.assertEqual(doc["lines"][0]["from_warehouse_name"], "Raw Material")
        self.assertEqual(sap.return_value.sap_document_lookups.call_args.kwargs["journal_created_by"], ("67", 77))

    def test_an_outgoing_payment_totals_its_parts(self, sap):
        sap.return_value.get_sap_document.return_value = {
            "DocEntry": 900, "DocNum": 900, "CardCode": "VENDA000010", "CashSum": 0, "TransferSum": 1000,
            "PaymentChecks": [{"CheckNumber": 11, "CheckSum": 250}],
            "PaymentInvoices": [{"DocEntry": 5001, "InvoiceType": "it_PurchaseInvoice", "SumApplied": 1250}],
            "AuthorizationStatus": "pasWithout",
        }
        sap.return_value.sap_document_lookups.return_value = empty_lookups(
            payment_invoices={"18-5001": {"doc_num": 626074136, "doc_date": "2026-07-03", "doc_total": 1250}}
        )
        doc = self.get("documents/VendorPayments/900/").data
        self.assertEqual(doc["totals"]["total"], 1250.0)
        self.assertEqual(doc["payment"]["invoices"][0]["doc_num"], 626074136)
        self.assertEqual(doc["header"]["approval_status"], "No approval")
        request = sap.return_value.sap_document_lookups.call_args.kwargs
        self.assertEqual(request["journal_created_by"], ("46", 900))
        self.assertEqual(request["payment_invoices"], [("18", 5001)])


@patch(SAP)
class PaymentDraftTests(SapDocumentsTestCase):
    DRAFT = {
        "header": {"doc_entry": 812, "doc_num": 812},
        "payment": {"transfer_sum": 980},
        "totals": {"total": 980},
        "tds": [],
        "tds_section": "",
        "attachment_entry": 170001,
        "journal_entry": None,
        "journal_preview": {"preview": True},
        "warnings": ["cheques"],
    }

    def test_the_payment_draft_endpoint_and_the_document_route_agree(self, sap):
        sap.return_value.sap_payment_draft.return_value = dict(self.DRAFT)
        direct = self.get("payment-drafts/812/")
        via_documents = self.get("documents/PaymentDrafts/812/")
        self.assertEqual(direct.status_code, status.HTTP_200_OK)
        self.assertEqual(direct.data, via_documents.data)
        self.assertEqual(direct.data["kind"], "payment_draft")
        self.assertEqual(direct.data["attachment_entry"], 170001)
        self.assertEqual(direct.data["warnings"], ["Could not read the cheques from SAP."])
        sap.return_value.get_sap_document.assert_not_called()

    def test_a_missing_payment_draft_is_404(self, sap):
        sap.return_value.sap_payment_draft.return_value = None
        self.assertEqual(self.get("payment-drafts/9/").status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

ATC_LINES = [
    {"line": 1, "file_name": "SN.jpeg", "stem": "SN", "extension": "jpeg", "attached_on": "2026-07-18"},
    {"line": 2, "file_name": "DocScanner 18 Jul 2026 5-58 pm.pdf", "stem": "", "extension": "pdf",
     "attached_on": None},
    {"line": 3, "file_name": "invoice.html", "stem": "invoice", "extension": "html", "attached_on": None},
]


@patch(SAP)
class AttachmentTests(SapDocumentsTestCase):
    permissions = ALL_PERMISSIONS

    def test_the_attachment_list(self, sap):
        sap.return_value.sap_attachment_lines.return_value = ATC_LINES
        response = self.get("attachments/53121/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["abs_entry"], 53121)
        self.assertEqual([line["line"] for line in response.data["lines"]], [1, 2, 3])
        sap.return_value.sap_attachment_lines.assert_called_once_with(53121)

    def test_a_download_streams_the_file_by_entry_and_line_and_is_recorded(self, sap):
        sap.return_value.sap_attachment_lines.return_value = ATC_LINES
        sap.return_value.download_attachment.return_value = {
            "data": b"%PDF-1.4 scan", "file_name": "stored.pdf", "content_type": "application/pdf",
        }
        response = self.get("attachments/53121/2/download/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"%PDF-1.4 scan")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Length"], "13")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        disposition = response["Content-Disposition"]
        self.assertTrue(disposition.startswith('inline; filename="DocScanner 18 Jul 2026 5-58?pm.pdf"'))
        self.assertIn("filename*=UTF-8''DocScanner%2018%20Jul%202026%205-58%E2%80%AFpm.pdf", disposition)
        disposition.encode("latin-1")  # a header the server can always send
        sap.return_value.download_attachment.assert_called_once_with(
            53121, 2, "DocScanner 18 Jul 2026 5-58 pm.pdf"
        )
        row = SapAttachmentDownload.objects.get()
        self.assertEqual((row.company, row.abs_entry, row.line, row.size_bytes), (self.company, 53121, 2, 13))
        self.assertEqual(row.created_by, self.user)
        self.assertEqual(row.file_name, "DocScanner 18 Jul 2026 5-58 pm.pdf")

    def test_html_is_never_served_inline(self, sap):
        sap.return_value.sap_attachment_lines.return_value = ATC_LINES
        sap.return_value.download_attachment.return_value = {
            "data": b"<script>x</script>", "file_name": "invoice.html", "content_type": "text/html",
        }
        response = self.get("attachments/53121/3/download/")
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="invoice.html"')

    def test_a_line_sap_does_not_have_is_404_and_not_recorded(self, sap):
        sap.return_value.sap_attachment_lines.return_value = ATC_LINES
        response = self.get("attachments/53121/9/download/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        sap.return_value.download_attachment.assert_not_called()
        self.assertFalse(SapAttachmentDownload.objects.exists())

    def test_a_file_the_file_service_cannot_find_is_404(self, sap):
        sap.return_value.sap_attachment_lines.return_value = ATC_LINES
        error = SAPValidationError("File service returned HTTP 404")
        error.status = 404
        sap.return_value.download_attachment.side_effect = error
        response = self.get("attachments/53121/1/download/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("no copy of this file", response.data["detail"])
        self.assertFalse(SapAttachmentDownload.objects.exists())

    def test_file_service_off_or_down_is_503_and_a_bad_name_502(self, sap):
        sap.return_value.sap_attachment_lines.return_value = ATC_LINES
        for error, expected in (
            (SAPConnectionError("Attachment downloads are not configured on this server"), 503),
            (SAPConnectionError("The attachment file service is unreachable."), 503),
            (SAPDataError('"x" cannot be served by the file server: its name contains U+202F'), 502),
        ):
            with self.subTest(error=str(error)):
                sap.return_value.download_attachment.side_effect = error
                self.assertEqual(self.get("attachments/53121/1/download/").status_code, expected)
        self.assertFalse(SapAttachmentDownload.objects.exists())


class DownloadHeaderTests(SimpleTestCase):
    def test_ascii_names_need_no_extended_form(self):
        self.assertEqual(services.content_disposition("1825.pdf", True), 'inline; filename="1825.pdf"')

    def test_quotes_breaks_and_slashes_cannot_break_the_header(self):
        header = services.content_disposition('a"b\r\nc/d\\e.pdf', False)
        self.assertEqual(header, "attachment; filename=\"a'b  c_d_e.pdf\"")

    def test_rupee_and_en_dash_names_get_a_utf8_form(self):
        header = services.content_disposition("Bill ₹500 – July.pdf", False)
        self.assertIn("filename*=UTF-8''Bill%20%E2%82%B9500%20%E2%80%93%20July.pdf", header)
        header.encode("latin-1")

    def test_only_safe_types_open_inline(self):
        self.assertEqual(services.inline_content_type("application/pdf", "x.pdf"), "application/pdf")
        self.assertEqual(services.inline_content_type("image/jpeg; charset=binary", "x.jpg"), "image/jpeg")
        # The file service said only octet-stream: the name decides.
        self.assertEqual(services.inline_content_type("application/octet-stream", "scan.png"), "image/png")
        self.assertIsNone(services.inline_content_type("image/svg+xml", "x.svg"))
        self.assertIsNone(services.inline_content_type("text/html", "x.html"))
        self.assertIsNone(services.inline_content_type("application/octet-stream", "x.xlsx"))
        self.assertIsNone(services.inline_content_type("", "page.html"))


class FilterBuilderTests(SimpleTestCase):
    def test_an_empty_filter_is_empty(self):
        self.assertEqual(services.build_filter(DOCUMENT_TYPES["Invoices"], {}), "")

    def test_open_and_a_date_range(self):
        self.assertEqual(
            services.build_filter(
                DOCUMENT_TYPES["InventoryTransferRequests"],
                {"status": "O", "date_from": date(2026, 4, 1), "date_to": date(2026, 4, 30)},
            ),
            "DocDate ge '2026-04-01' and DocDate le '2026-04-30' and DocumentStatus eq 'bost_Open'",
        )

    def test_closed_on_a_type_without_cancellation(self):
        self.assertEqual(
            services.build_filter(DOCUMENT_TYPES["Drafts"], {"status": "C"}), "DocumentStatus eq 'bost_Close'"
        )


# ---------------------------------------------------------------------------
# Rights
# ---------------------------------------------------------------------------

VIEW_PATHS = (
    "types/",
    "documents/Invoices/",
    "documents/Invoices/1/",
    "payment-drafts/1/",
    "attachments/1/",
)
DOWNLOAD_PATH = "attachments/53121/1/download/"


@patch(SAP)
class PermissionTests(SapDocumentsTestCase):
    permissions = []

    def arm(self, sap):
        sap.return_value.list_sap_documents.return_value = []
        sap.return_value.get_sap_document.return_value = {"DocEntry": 1, "DocumentLines": []}
        sap.return_value.sap_document_lookups.return_value = empty_lookups()
        sap.return_value.sap_payment_draft.return_value = dict(PaymentDraftTests.DRAFT)
        sap.return_value.sap_attachment_lines.return_value = ATC_LINES
        sap.return_value.download_attachment.return_value = {"data": b"x", "content_type": "application/pdf"}

    def test_a_missing_company_header_is_refused_everywhere(self, sap):
        self.arm(sap)
        self.grant(*ALL_PERMISSIONS)
        for path in (*VIEW_PATHS, DOWNLOAD_PATH):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(f"{BASE}{path}").status_code, status.HTTP_403_FORBIDDEN)

    def test_another_companys_header_is_refused(self, sap):
        self.arm(sap)
        self.grant(*ALL_PERMISSIONS)
        Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        response = self.client.get(f"{BASE}types/", HTTP_COMPANY_CODE="JIVO_MART")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_rights_is_refused_everywhere(self, sap):
        self.arm(sap)
        for path in (*VIEW_PATHS, DOWNLOAD_PATH):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, status.HTTP_403_FORBIDDEN)
        sap.assert_not_called()

    def test_viewers_browse_but_cannot_download(self, sap):
        self.arm(sap)
        self.grant("can_view_sap_documents")
        for path in VIEW_PATHS:
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, status.HTTP_200_OK)
        self.assertEqual(self.get(DOWNLOAD_PATH).status_code, status.HTTP_403_FORBIDDEN)
        sap.return_value.download_attachment.assert_not_called()

    def test_the_download_right_alone_opens_nothing(self, sap):
        self.arm(sap)
        self.grant("can_download_sap_attachments")
        for path in (*VIEW_PATHS, DOWNLOAD_PATH):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, status.HTTP_403_FORBIDDEN)

    def test_both_rights_download(self, sap):
        self.arm(sap)
        self.grant(*ALL_PERMISSIONS)
        self.assertEqual(self.get(DOWNLOAD_PATH).status_code, status.HTTP_200_OK)


class PermissionSurfaceTests(TestCase):
    def test_only_the_declared_rights_exist(self):
        codenames = set(
            Permission.objects.filter(content_type__app_label="sap_documents").values_list("codename", flat=True)
        )
        self.assertEqual(codenames, set(ALL_PERMISSIONS))


class GroupCommandTests(TestCase):
    """Every group right exists, and every right the API checks is handed out."""

    def setUp(self):
        call_command("setup_sap_documents_groups", stdout=StringIO())

    def test_every_group_is_created_with_its_rights(self):
        for name, codes in SAP_DOCUMENTS_GROUPS.items():
            with self.subTest(group=name):
                held = {
                    f"sap_documents.{codename}"
                    for codename in Group.objects.get(name=name).permissions.values_list(
                        "codename", flat=True
                    )
                }
                self.assertEqual(held, set(codes))

    def test_every_right_the_api_checks_is_granted_by_some_group(self):
        granted = {code for codes in SAP_DOCUMENTS_GROUPS.values() for code in codes}
        checked = {guards.VIEW_PERMISSION, guards.DOWNLOAD_PERMISSION}
        self.assertEqual(checked - granted, set())

    def test_a_rerun_changes_nothing(self):
        before = {g.name: set(g.permissions.values_list("codename", flat=True)) for g in Group.objects.all()}
        call_command("setup_sap_documents_groups", stdout=StringIO())
        after = {g.name: set(g.permissions.values_list("codename", flat=True)) for g in Group.objects.all()}
        self.assertEqual(before, after)


class LineFieldTests(SimpleTestCase):
    """What SAP Portal's approval lines showed beyond the document browser's:
    whether a line is liable to withholding tax, the bilty's date, and the
    line's discount."""

    def _line(self, line, row=None):
        from sap_documents.services import _empty_lookups, shape_line

        return shape_line(line, row, _empty_lookups())

    def test_wtax_liable_reads_sap_and_hana_spellings(self):
        self.assertTrue(self._line({"WTLiable": "tYES"})["wtax_liable"])
        self.assertFalse(self._line({}, {"WtLiable": "N"})["wtax_liable"])
        self.assertIsNone(self._line({})["wtax_liable"])

    def test_the_bilty_date_comes_from_its_udf(self):
        self.assertEqual(self._line({"U_BiltyDate": "2026-09-14T00:00:00Z"})["bilty_date"], "2026-09-14")
        self.assertEqual(self._line({"U_Bilty_Date": "2026-09-15"})["bilty_date"], "2026-09-15")
        self.assertIsNone(self._line({})["bilty_date"])

    def test_the_discount_reads_the_service_layer_and_hana_columns(self):
        self.assertEqual(self._line({"DiscountPercent": 2.5})["discount_percent"], 2.5)
        self.assertEqual(self._line({}, {"DiscPrcnt": "10.000000"})["discount_percent"], 10)
        self.assertIsNone(self._line({})["discount_percent"])
