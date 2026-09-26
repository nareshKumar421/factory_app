"""
The approvals queue: verify, reject, edit, list, detail, documents — the state
machine with the portal's bugs fixed, company scoping, and the rights each
endpoint checks.

    DEBUG=False python manage.py test partner_onboarding.tests_workflow --settings=config.sqlite_test_settings
"""

from django.utils import timezone
from rest_framework import status

from .constants import AddressType, AttachmentKind, EventKind, RegistrationStatus
from .testing import (
    CUSTOMER_RIGHTS,
    GSTIN,
    PNG,
    VENDOR_RIGHTS,
    InternalTestCase,
    attach,
    make_customer,
    make_vendor,
)


class VerifyTests(InternalTestCase):
    def test_a_pending_registration_is_verified_by_the_verifier(self):
        registration = make_customer(self.company)
        self.only("can_view_customer_registrations", "can_verify_customer_registrations")
        response = self.post(f"customers/{registration.pk}/verify/", {"note": "Checked the documents"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationStatus.VERIFIED)
        self.assertEqual(registration.verified_by, self.user)
        self.assertIsNotNone(registration.verified_at)
        event = registration.events.get(kind=EventKind.VERIFIED)
        self.assertEqual((event.actor, event.note), (self.user, "Checked the documents"))
        self.assertEqual(response.data["verified_by_name"], "Test Approver")

    def test_only_a_pending_registration_can_be_verified(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        response = self.post(f"customers/{registration.pk}/verify/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "invalid_state")

    def test_verifying_needs_the_verify_right(self):
        # The portal let any login verify (server.js:797).
        registration = make_customer(self.company)
        for rights in (("can_view_customer_registrations",), ("can_approve_customer_registrations",), VENDOR_RIGHTS):
            with self.subTest(rights=rights):
                self.only(*rights)
                response = self.post(f"customers/{registration.pk}/verify/")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationStatus.PENDING)


class RejectTests(InternalTestCase):
    def test_rejects_a_pending_or_verified_registration_with_a_reason(self):
        for start in (RegistrationStatus.PENDING, RegistrationStatus.VERIFIED):
            with self.subTest(start=start):
                registration = make_vendor(self.company, status=start)
                response = self.post(f"vendors/{registration.pk}/reject/", {"reason": "GST certificate unreadable"})
                self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
                registration.refresh_from_db()
                self.assertEqual(registration.status, RegistrationStatus.REJECTED)
                self.assertEqual(registration.rejected_by, self.user)
                self.assertEqual(registration.rejection_reason, "GST certificate unreadable")
                event = registration.events.get(kind=EventKind.REJECTED)
                self.assertEqual(event.data["from"], start)

    def test_a_reason_is_required(self):
        registration = make_customer(self.company)
        response = self.post(f"customers/{registration.pk}/reject/", {"reason": "  "})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_partner_already_created_in_sap_cannot_be_rejected(self):
        # The portal rejected it anyway (server.js:819, routes/vendors.js:343).
        registration = make_customer(
            self.company, status=RegistrationStatus.APPROVED, card_code="CUSTA000001", sap_card_code="CUSTA000001"
        )
        response = self.post(f"customers/{registration.pk}/reject/", {"reason": "Changed my mind"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("CUSTA000001", response.data["detail"])
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationStatus.APPROVED)
        self.assertFalse(registration.events.filter(kind=EventKind.REJECTED).exists())

    def test_a_rejected_registration_stays_rejected(self):
        registration = make_customer(self.company, status=RegistrationStatus.REJECTED)
        self.assertEqual(
            self.post(f"customers/{registration.pk}/reject/", {"reason": "again"}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.post(f"customers/{registration.pk}/verify/").status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_the_verifier_or_the_approver_may_reject_a_viewer_may_not(self):
        for rights, expected in (
            (("can_view_vendor_registrations",), status.HTTP_403_FORBIDDEN),
            (("can_verify_vendor_registrations",), status.HTTP_200_OK),
            (("can_approve_vendor_registrations",), status.HTTP_200_OK),
        ):
            with self.subTest(rights=rights):
                registration = make_vendor(self.company)
                self.only(*rights)
                response = self.post(f"vendors/{registration.pk}/reject/", {"reason": "Not a supplier"})
                self.assertEqual(response.status_code, expected)

    def test_nothing_can_happen_while_sap_is_creating_it(self):
        registration = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        registration.sap_posting_since = timezone.now()
        registration.save()
        for method, path, body in (
            (self.post, "reject/", {"reason": "x"}),
            (self.patch, "", {"industry": "RETAIL"}),
        ):
            response = method(f"customers/{registration.pk}/{path}", body)
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(response.data["code"], "sap_posting_in_progress")
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationStatus.VERIFIED)


class EditTests(InternalTestCase):
    def test_the_verifier_corrects_fields_and_it_is_recorded(self):
        registration = make_customer(self.company)
        self.only("can_view_customer_registrations", "can_verify_customer_registrations")
        response = self.patch(
            f"customers/{registration.pk}/",
            {"industry": "retail", "customer_type": "DISTRIBUTOR", "credit_limit": "50000", "main_group": "MG1"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        registration.refresh_from_db()
        self.assertEqual(registration.industry, "RETAIL")
        self.assertEqual(registration.customer_type, "DISTRIBUTOR")
        self.assertEqual(str(registration.credit_limit), "50000.00")
        event = registration.events.get(kind=EventKind.EDITED)
        self.assertEqual(set(event.data["fields"]), {"industry", "customer_type", "credit_limit", "main_group"})

    def test_edits_are_held_to_the_forms_rules(self):
        registration = make_customer(self.company)
        for body, field in (({"pan": "12345"}, "pan"), ({"gstin": ""}, "gstin"), ({"card_name": "N" * 101}, "card_name")):
            with self.subTest(body=body):
                response = self.patch(f"customers/{registration.pk}/", body)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)
        # A B2C customer may drop the GSTIN.
        response = self.patch(f"customers/{registration.pk}/", {"customer_type": "B2C", "gstin": ""})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_editing_needs_the_verify_right_and_an_open_registration(self):
        registration = make_customer(self.company)
        self.only("can_view_customer_registrations", "can_approve_customer_registrations")
        self.assertEqual(
            self.patch(f"customers/{registration.pk}/", {"industry": "X"}).status_code, status.HTTP_403_FORBIDDEN
        )
        self.only(*CUSTOMER_RIGHTS)
        registration.status = RegistrationStatus.APPROVED
        registration.save()
        self.assertEqual(
            self.patch(f"customers/{registration.pk}/", {"industry": "X"}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_addresses_sent_replace_the_ones_on_file(self):
        registration = make_customer(self.company)
        response = self.patch(
            f"customers/{registration.pk}/",
            {
                "addresses": [
                    {"address_type": "BILL_TO", "street": "NEW STREET", "city": "NEWTOWN", "state": "PB"},
                    {"address_type": "SHIP_TO", "street": "DEPOT ROAD", "city": "NEWTOWN", "state": "PB"},
                    {"address_type": "SHIP_TO", "street": "DEPOT ROAD 2", "city": "NEWTOWN", "state": "PB"},
                ]
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        names = [(a["address_type"], a["address_name"]) for a in response.data["addresses"]]
        self.assertEqual(
            names,
            [
                ("BILL_TO", "TEST TRADERS PVT LTD - PB"),
                ("SHIP_TO", "TEST TRADERS PVT LTD - PB"),
                ("SHIP_TO", "TEST TRADERS PVT LTD - PB 2"),
            ],
        )
        response = self.patch(
            f"customers/{registration.pk}/",
            {"addresses": [{"address_type": "SHIP_TO", "street": "X", "city": "Y", "state": "PB"}]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_corrected_name_renames_the_automatic_address_names(self):
        registration = make_customer(self.company, card_name="TEST TRADRES PVT LTD")
        response = self.patch(f"customers/{registration.pk}/", {"card_name": "Test Traders Pvt Ltd"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {a["address_name"] for a in response.data["addresses"]}, {"TEST TRADERS PVT LTD - HR"}
        )

    def test_a_vendors_bank_accounts_can_be_corrected(self):
        vendor = make_vendor(self.company)
        response = self.patch(
            f"vendors/{vendor.pk}/",
            {
                "bank_accounts": [
                    {"bank_name": "Other Bank", "account_number": "9999 8888", "ifsc": "OTHR0000001", "sap_bank_code": "OTH"}
                ]
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        bank = vendor.bank_accounts.get()
        self.assertEqual((bank.account_number, bank.sap_bank_code), ("99998888", "OTH"))


class ListAndDetailTests(InternalTestCase):
    def test_the_list_filters_searches_and_counts(self):
        make_customer(self.company, card_name="ALPHA FOODS")
        beta = make_customer(self.company, card_name="BETA OILS", status=RegistrationStatus.VERIFIED, gstin="")
        make_customer(self.company, card_name="GAMMA MART", status=RegistrationStatus.REJECTED)
        make_customer(self.other_company, card_name="ALPHA ELSEWHERE")

        everything = self.get("customers/")
        self.assertEqual(everything.status_code, status.HTTP_200_OK)
        self.assertEqual(everything.data["count"], 3)
        self.assertEqual(everything.data["counts"], {"PENDING": 1, "VERIFIED": 1, "APPROVED": 0, "REJECTED": 1})
        self.assertNotIn("ALPHA ELSEWHERE", [row["card_name"] for row in everything.data["results"]])

        verified = self.get("customers/?status=VERIFIED")
        self.assertEqual([row["card_name"] for row in verified.data["results"]], ["BETA OILS"])
        several = self.get("customers/?status=PENDING,REJECTED")
        self.assertEqual(several.data["count"], 2)
        self.assertEqual(self.get("customers/?status=NOPE").status_code, status.HTTP_400_BAD_REQUEST)

        by_name = self.get("customers/?search=alpha")
        self.assertEqual([row["card_name"] for row in by_name.data["results"]], ["ALPHA FOODS"])
        self.assertEqual(by_name.data["counts"]["PENDING"], 1)
        by_reference = self.get(f"customers/?search={beta.reference}")
        self.assertEqual([row["id"] for row in by_reference.data["results"]], [beta.pk])
        by_gstin = self.get(f"customers/?search={GSTIN[:6]}")
        self.assertEqual(by_gstin.data["count"], 2)
        row = by_name.data["results"][0]
        self.assertEqual((row["city"], row["state"], row["attachment_count"]), ("TESTNAGAR", "HR", 3))

    def test_another_companys_registration_is_a_404(self):
        theirs = make_customer(self.other_company)
        for path in (f"customers/{theirs.pk}/", f"customers/{theirs.pk}/attachments/{theirs.attachments.first().pk}/"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, status.HTTP_404_NOT_FOUND)
        for path, body in (("verify/", {}), ("reject/", {"reason": "x"})):
            self.assertEqual(self.post(f"customers/{theirs.pk}/{path}", body).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.patch(f"customers/{theirs.pk}/", {"industry": "X"}).status_code, status.HTTP_404_NOT_FOUND)
        # A vendor id is not reachable through the customer URL either.
        vendor = make_vendor(self.company)
        self.assertEqual(self.get(f"customers/{vendor.pk + 1000}/").status_code, status.HTTP_404_NOT_FOUND)

    def test_the_detail_says_what_this_user_may_do(self):
        pending = make_customer(self.company)
        verified = make_customer(self.company, status=RegistrationStatus.VERIFIED)
        self.only("can_view_customer_registrations", "can_verify_customer_registrations")
        actions = self.get(f"customers/{pending.pk}/").data["actions"]
        self.assertEqual(actions, {"can_edit": True, "can_verify": True, "can_reject": True, "can_approve": False})
        self.only("can_approve_customer_registrations")
        actions = self.get(f"customers/{verified.pk}/").data["actions"]
        self.assertEqual(actions, {"can_edit": False, "can_verify": False, "can_reject": True, "can_approve": True})
        self.only("can_view_customer_registrations")
        actions = self.get(f"customers/{verified.pk}/").data["actions"]
        self.assertEqual(set(actions.values()), {False})

    def test_the_detail_carries_everything_the_screen_shows(self):
        vendor = make_vendor(self.company)
        data = self.get(f"vendors/{vendor.pk}/").data
        self.assertEqual(data["reference"], vendor.reference)
        self.assertEqual(data["company_code"], "JIVO_OIL")
        self.assertEqual(data["default_card_code_prefix"], "VENDA")
        self.assertEqual(len(data["addresses"]), 2)
        self.assertEqual(data["bank_accounts"][0]["ifsc"], "TEST0001234")
        self.assertEqual({a["kind"] for a in data["attachments"]}, {"PAN", "GST"})
        self.assertIn("fssai_number", data)
        self.assertNotIn("customer_type", data)

    def test_documents_stream_through_the_permission_check(self):
        registration = make_customer(self.company)
        document = registration.attachments.get(kind=AttachmentKind.PAN)
        response = self.get(f"customers/{registration.pk}/attachments/{document.pk}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
        # A document of the other kind of registration is not reachable here.
        self.assertEqual(
            self.get(f"vendors/{registration.pk}/attachments/{document.pk}/").status_code, status.HTTP_404_NOT_FOUND
        )
        self.only(*VENDOR_RIGHTS)
        self.assertEqual(
            self.get(f"customers/{registration.pk}/attachments/{document.pk}/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_an_unexpected_file_type_downloads_instead_of_opening(self):
        registration = make_customer(self.company, with_documents=False)
        document = attach(registration, AttachmentKind.OTHER, "page.html", b"<script>x</script>", "text/html")
        response = self.get(f"customers/{registration.pk}/attachments/{document.pk}/")
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response["Content-Disposition"])
        image = attach(registration, AttachmentKind.OTHER, "photo.png", PNG, "image/png")
        response = self.get(f"customers/{registration.pk}/attachments/{image.pk}/")
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("inline", response["Content-Disposition"])

    def test_the_address_list_is_in_order(self):
        registration = make_customer(self.company)
        data = self.get(f"customers/{registration.pk}/").data
        self.assertEqual([a["address_type"] for a in data["addresses"]], [AddressType.BILL_TO, AddressType.SHIP_TO])
