"""
Creating the partner in SAP, with SAP faked where the workflow looks it up.

The payload for a customer and for a vendor (the portal's createCustomer /
createVendor mapping), the duplicate check (409), the card code reserved and
committed before SAP is asked, a refusal leaving the registration VERIFIED with
a SAP_FAILED event, adoption after a lost answer, and documents — fatal for a
customer, a warning for a vendor.

    DEBUG=False python manage.py test partner_onboarding.tests_approve --settings=config.sqlite_test_settings
"""

from unittest.mock import patch

from rest_framework import status

from sap_client.exceptions import SAPConnectionError, SAPValidationError

from .constants import AttachmentKind, EventKind, RegistrationStatus
from .models import CustomerRegistration, VendorRegistration
from .testing import GSTIN, PAN, FakeSAP, InternalTestCase, attach, make_customer, make_vendor, stale

MANAGER = {
    "card_code_prefix": "custa",
    "bp_group_code": 105,
    "bp_group_name": "DISTRIBUTORS",
    "payment_terms_code": 3,
    "payment_terms_name": "30 DAYS",
    "sales_employee_code": 12,
    "sales_employee_name": "TEST SALES",
    "control_account": "1101002",
    "credit_limit": "250000.00",
    "main_group": "MG01",
    "chain": "CH07",
}


class ApproveTestCase(InternalTestCase):
    def setUp(self):
        super().setUp()
        self.sap = FakeSAP()
        patcher = patch("partner_onboarding.services.workflow.SAPClient", return_value=self.sap)
        self.sap_client = patcher.start()
        self.addCleanup(patcher.stop)

    def approve(self, registration, body=None):
        family = "customers" if registration.family == "customer" else "vendors"
        return self.post(f"{family}/{registration.pk}/approve/", body if body is not None else {})


class CustomerApproveTests(ApproveTestCase):
    def test_creates_the_customer_in_its_companys_sap(self):
        registration = make_customer(
            self.company,
            status=RegistrationStatus.VERIFIED,
            has_msme=True,
            msme_number="UDYAM-HR-18-0040140",
            msme_type="MICRO",
            msme_business_type="TRADING",
            website="www.example.com",
            contact_mobile="",
        )
        attach(registration, AttachmentKind.OTHER, "extra.pdf")
        response = self.approve(registration, MANAGER)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.sap_client.assert_called_with(company_code="JIVO_OIL")

        payload = self.sap.created[0]
        self.assertEqual(payload["CardCode"], "CUSTA000124")
        self.assertEqual(payload["CardName"], "TEST TRADERS PVT LTD")
        self.assertEqual(payload["CardType"], "cCustomer")
        self.assertEqual(payload["Currency"], "INR")
        self.assertEqual(payload["Phone1"], "+91 90000 00001")
        self.assertEqual(payload["EmailAddress"], "buyer@example.com")
        self.assertEqual(payload["Website"], "www.example.com")
        self.assertEqual(payload["CreditLimit"], 250000.0)
        self.assertEqual(payload["Notes"], "TEST")
        self.assertEqual(
            (payload["GroupCode"], payload["PayTermsGrpCode"], payload["SalesPersonCode"]), (105, 3, 12)
        )
        self.assertEqual(payload["DebitorAccount"], "1101002")
        self.assertEqual(payload["AttachmentEntry"], 555)
        self.assertEqual(
            payload["ContactEmployees"],
            [
                {
                    "Name": "ASHA EXAMPLE",
                    "FirstName": "ASHA",
                    "LastName": "EXAMPLE",
                    "MobilePhone": "+91 90000 00001",
                    "E_Mail": "buyer@example.com",
                    "Active": "tYES",
                }
            ],
        )
        bill, ship = payload["BPAddresses"]
        self.assertEqual(
            bill,
            {
                "AddressName": "TEST TRADERS PVT LTD - HR",
                "AddressType": "bo_BillTo",
                "Street": "PLOT 1",
                "Block": "",
                "City": "TESTNAGAR",
                "ZipCode": "123456",
                "State": "HR",
                "Country": "IN",
                "GSTIN": GSTIN,
                "GstType": "gstRegularTDSISD",
            },
        )
        self.assertEqual(ship["AddressType"], "bo_ShipTo")
        self.assertNotIn("GSTIN", ship)
        self.assertEqual(
            payload["BPFiscalTaxIDCollection"],
            [{"Address": "TEST TRADERS PVT LTD - HR", "AddrType": "bo_BillTo", "TaxId0": PAN}],
        )
        self.assertEqual((payload["U_Main_Group"], payload["U_Chain"]), ("MG01", "CH07"))
        self.assertEqual(
            (payload["U_MSME"], payload["U_MSME_Type"], payload["U_MSME_BType"]),
            ("UDYAM-HR-18-0040140", "MICRO", "TRADING"),
        )
        self.assertNotIn("U_Fssai", payload)
        self.assertNotIn("BPBankAccounts", payload)

        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationStatus.APPROVED)
        self.assertEqual((registration.card_code, registration.sap_card_code), ("CUSTA000124", "CUSTA000124"))
        self.assertEqual(registration.approved_by, self.user)
        self.assertIsNone(registration.sap_posting_since)
        self.assertEqual(registration.sap_attachment_entry, 555)
        self.assertEqual(registration.card_code_prefix, "CUSTA")
        kinds = list(registration.events.values_list("kind", flat=True))
        self.assertEqual(kinds[-2:], [EventKind.SAP_CREATED, EventKind.APPROVED])
        self.assertEqual(response.data["sap_card_code"], "CUSTA000124")

        # One Attachments2 entry: the first file creates it, the rest are lines.
        # The Aadhaar card never goes to SAP (the portal never sent it either).
        self.assertEqual([entry for entry, _ in self.sap.uploads], ["new", 555, 555])
        sent = " ".join(name for _, name in self.sap.uploads)
        self.assertIn("_PAN_", sent)
        self.assertIn("_CHEQUE_", sent)
        self.assertNotIn("AADHAAR", sent)
        self.assertIsNone(registration.attachments.get(kind=AttachmentKind.AADHAAR).sent_to_sap_at)
        self.assertIsNotNone(registration.attachments.get(kind=AttachmentKind.PAN).sent_to_sap_at)

    def test_only_a_verified_registration_goes_to_sap(self):
        registration = make_customer(self.company)
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.sap.calls, [])

    def test_approving_needs_the_approve_right(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        self.only("can_view_customer_registrations", "can_verify_customer_registrations")
        self.assertEqual(self.approve(registration).status_code, status.HTTP_403_FORBIDDEN)
        self.only("can_approve_vendor_registrations")
        self.assertEqual(self.approve(registration).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.sap.calls, [])

    def test_existing_partners_with_the_same_tax_ids_stop_it_until_confirmed(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        self.sap.tax_matches = [{"card_code": "CUSTA000007", "card_name": "TEST TRADERS", "matched_on": ["GSTIN"]}]
        response = self.approve(registration, MANAGER)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "possible_duplicate")
        self.assertEqual(response.data["matches"], self.sap.tax_matches)
        self.assertIn(("partners_with_tax_ids", "C", GSTIN, PAN), self.sap.calls)
        registration.refresh_from_db()
        self.assertEqual((registration.status, registration.card_code), (RegistrationStatus.VERIFIED, ""))
        self.assertEqual(self.sap.created, [])
        self.assertFalse(registration.events.filter(kind=EventKind.SAP_FAILED).exists())

        response = self.approve(registration, {**MANAGER, "confirm_duplicate": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(self.sap.created), 1)

    def test_the_card_code_is_reserved_and_committed_before_sap_is_asked(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        seen = {}

        def at_post(payload):
            row = CustomerRegistration.objects.get(pk=registration.pk)
            seen.update(card_code=row.card_code, posting=row.sap_posting_since, status=row.status)

        self.sap.on_create = at_post
        self.approve(registration)
        self.assertEqual(seen["card_code"], "CUSTA000124")
        self.assertIsNotNone(seen["posting"])
        self.assertEqual(seen["status"], RegistrationStatus.VERIFIED)

    def test_a_code_sap_or_another_registration_holds_is_skipped(self):
        make_vendor(self.company, status=RegistrationStatus.VERIFIED, card_code="CUSTA000124")  # reserved here
        self.sap.partners["CUSTA000125"] = {"card_code": "CUSTA000125", "card_name": "SOMEONE", "card_type": "S"}
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.sap.created[0]["CardCode"], "CUSTA000126")

    def test_sap_refusal_is_a_400_with_sap_words_and_leaves_it_verified(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        self.sap.create_error = SAPValidationError("[SAP -10] Invalid 'GroupCode'")
        response = self.approve(registration, MANAGER)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "[SAP -10] Invalid 'GroupCode'")
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationStatus.VERIFIED)
        self.assertEqual(registration.sap_error, "[SAP -10] Invalid 'GroupCode'")
        self.assertIsNone(registration.sap_posting_since)
        # Recorded after the transaction that tried unwound, so it survived.
        failure = registration.events.get(kind=EventKind.SAP_FAILED)
        self.assertEqual(failure.data["card_code"], "CUSTA000124")
        # The reservation and the approver's fields were committed before SAP.
        self.assertEqual(registration.card_code, "CUSTA000124")
        self.assertEqual(registration.bp_group_code, 105)

    def test_a_lost_answer_is_adopted_on_retry_not_created_twice(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)

        def commits_then_times_out(payload):
            self.sap.partners[payload["CardCode"]] = {
                "card_code": payload["CardCode"],
                "card_name": payload["CardName"],
                "card_type": "C",
            }
            raise SAPConnectionError("read timed out - check SAP before posting again")

        self.sap.on_create = commits_then_times_out
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        registration.refresh_from_db()
        self.assertEqual((registration.status, registration.card_code), (RegistrationStatus.VERIFIED, "CUSTA000124"))

        self.sap.on_create = None
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        registration.refresh_from_db()
        self.assertEqual((registration.status, registration.sap_card_code), (RegistrationStatus.APPROVED, "CUSTA000124"))
        self.assertEqual(self.sap.created, [])
        created = registration.events.get(kind=EventKind.SAP_CREATED)
        self.assertTrue(created.data["adopted"])
        # Adopted before the duplicate check could flag the partner as its own twin:
        # only the first attempt asked about tax IDs.
        self.assertEqual([call[0] for call in self.sap.calls].count("partners_with_tax_ids"), 1)

    def test_a_lost_answer_is_adopted_even_after_the_name_was_corrected(self):
        """SAP created the partner, the app never heard, and a verifier then fixed
        the name. The partner under the reserved code still carries this
        registration's GSTIN, so it is adopted, not created a second time."""
        registration = make_customer(
            self.company, status=RegistrationStatus.VERIFIED, card_code="CUSTA000124",
            card_name="ACME TRADERS", gstin="03AAACA1234A1Z5",
        )
        self.sap.partners["CUSTA000124"] = {"card_code": "CUSTA000124", "card_name": "ACME TRADRES", "card_type": "C"}
        self.sap.tax_matches = [{"card_code": "CUSTA000124", "card_name": "ACME TRADRES", "matched_on": ["GSTIN"]}]
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        registration.refresh_from_db()
        self.assertEqual((registration.status, registration.sap_card_code), (RegistrationStatus.APPROVED, "CUSTA000124"))
        self.assertEqual(self.sap.created, [])
        self.assertTrue(registration.events.get(kind=EventKind.SAP_CREATED).data["adopted"])

    def test_a_reserved_code_now_held_by_someone_else_is_replaced(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED, card_code="CUSTA000124")
        self.sap.partners["CUSTA000124"] = {"card_code": "CUSTA000124", "card_name": "SOMEBODY ELSE", "card_type": "C"}
        self.sap.next_codes["CUSTA"] = "CUSTA000125"
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.sap.created[0]["CardCode"], "CUSTA000125")

    def test_a_customers_documents_failing_stops_the_customer(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        self.sap.upload_error = SAPValidationError("Attachments folder not defined")
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "documents_not_sent")
        self.assertEqual(self.sap.created, [])
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationStatus.VERIFIED)
        self.assertTrue(registration.events.filter(kind=EventKind.SAP_FAILED).exists())

    def test_documents_resume_where_they_stopped(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        self.sap.add_line_error = SAPConnectionError("SAP went away")
        self.assertEqual(self.approve(registration).status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        registration.refresh_from_db()
        self.assertEqual(registration.sap_attachment_entry, 555)
        self.sap.add_line_error = None
        self.sap.uploads.clear()
        self.assertEqual(self.approve(registration).status_code, status.HTTP_200_OK)
        # Only the file not yet sent, into the entry already recorded.
        self.assertEqual(len(self.sap.uploads), 1)
        self.assertEqual(self.sap.uploads[0][0], 555)
        self.assertEqual(self.sap.created[0]["AttachmentEntry"], 555)

    def test_a_second_click_while_sap_is_working_waits(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        CustomerRegistration.objects.filter(pk=registration.pk).update(sap_posting_since=stale(1))
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "sap_posting_in_progress")
        self.assertEqual(self.sap.calls, [])
        # An abandoned attempt (a killed worker) does not block for ever.
        CustomerRegistration.objects.filter(pk=registration.pk).update(sap_posting_since=stale(11))
        self.assertEqual(self.approve(registration).status_code, status.HTTP_200_OK)

    def test_an_approved_registration_is_not_created_again(self):
        registration = make_customer(
            self.company, status=RegistrationStatus.APPROVED, card_code="CUSTA000001", sap_card_code="CUSTA000001"
        )
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.sap.calls, [])

    def test_remarks_too_long_for_sap_are_left_out_with_a_warning(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED, remarks="R" * 150)
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("Notes", self.sap.created[0])
        self.assertIn("Remarks", response.data["warnings"][0])
        registration.refresh_from_db()
        self.assertIn("Remarks", registration.sap_warning)

    def test_a_prefix_that_makes_the_code_too_long_is_refused(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        response = self.approve(registration, {"card_code_prefix": "ABCDEFGHIJ"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("15", response.data["detail"])
        self.assertEqual(self.sap.created, [])

    def test_sap_unreadable_before_posting_posts_nothing(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        self.sap.lookup_error = SAPConnectionError("HANA down")
        response = self.approve(registration)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        registration.refresh_from_db()
        self.assertEqual((registration.card_code, registration.sap_posting_since), ("", None))
        self.assertEqual(self.sap.created, [])
        self.assertTrue(registration.events.filter(kind=EventKind.SAP_FAILED).exists())


class VendorApproveTests(ApproveTestCase):
    def test_creates_the_vendor_with_its_bank_account(self):
        vendor = make_vendor(self.company, status=RegistrationStatus.VERIFIED, fssai_number="10020042001234", control_account="")
        bank = vendor.bank_accounts.get()
        response = self.approve(vendor, {"bank_accounts": [{"id": bank.pk, "sap_bank_code": "tst"}]})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        payload = self.sap.created[0]
        self.assertEqual(payload["CardCode"], "VENDA000124")
        self.assertEqual(payload["CardType"], "cSupplier")
        self.assertEqual(payload["Phone1"], "9000000002")
        # createVendor's fallback AP account.
        self.assertEqual(payload["DebitorAccount"], "2110005")
        self.assertEqual(payload["U_Fssai"], "10020042001234")
        self.assertEqual(
            payload["BPBankAccounts"],
            [
                {
                    "BankCode": "TST",
                    "AccountNo": "000111222333",
                    "Branch": "MAIN BRANCH",
                    "AccountName": "TEST BANK",
                    "BICSwiftCode": "TEST0001234",
                    "UserNo1": "TEST0001234",
                    "UserNo2": "Current",
                    "IBAN": "",
                }
            ],
        )
        self.assertEqual(payload["ContactEmployees"][0]["MobilePhone"], "9000000002")
        self.assertIn(("partners_with_tax_ids", "S", GSTIN, PAN), self.sap.calls)
        self.assertNotIn("Website", payload)
        vendor.refresh_from_db()
        self.assertEqual(vendor.status, RegistrationStatus.APPROVED)
        self.assertEqual(vendor.bank_accounts.get().sap_bank_code, "TST")

    def test_every_bank_account_needs_a_sap_bank_code(self):
        # createVendor dropped such an account from the partner silently.
        vendor = make_vendor(self.company, status=RegistrationStatus.VERIFIED)
        response = self.approve(vendor)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("bank", response.data["detail"])
        self.assertEqual(self.sap.calls, [])

    def test_a_bank_account_of_another_vendor_is_refused(self):
        vendor = make_vendor(self.company, status=RegistrationStatus.VERIFIED, bank_code="TST")
        other = make_vendor(self.company, card_name="OTHER LLP")
        response = self.approve(vendor, {"bank_accounts": [{"id": other.bank_accounts.get().pk, "sap_bank_code": "X"}]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(other.bank_accounts.get().sap_bank_code, "")

    def test_a_vendors_documents_failing_is_only_a_warning(self):
        vendor = make_vendor(self.company, status=RegistrationStatus.VERIFIED, bank_code="TST")
        self.sap.upload_error = SAPConnectionError("share unreachable")
        response = self.approve(vendor)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("AttachmentEntry", self.sap.created[0])
        self.assertTrue(response.data["warnings"])
        vendor.refresh_from_db()
        self.assertEqual(vendor.status, RegistrationStatus.APPROVED)
        self.assertIn("share unreachable", vendor.sap_warning)
        self.assertIsNone(vendor.sap_attachment_entry)

    def test_the_vendor_is_created_in_its_own_companys_sap(self):
        vendor = make_vendor(self.other_company, status=RegistrationStatus.VERIFIED, bank_code="TST")
        response = self.client.post(
            f"/api/v1/partner-onboarding/vendors/{vendor.pk}/approve/",
            {},
            format="json",
            HTTP_COMPANY_CODE="JIVO_MART",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.sap_client.assert_called_with(company_code="JIVO_MART")
        self.assertEqual(VendorRegistration.objects.get(pk=vendor.pk).status, RegistrationStatus.APPROVED)
