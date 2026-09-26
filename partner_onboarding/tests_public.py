"""
The public registration forms: no login, throttled, files checked, company
whitelisted, and the portal forms' rules enforced on the server.

    DEBUG=False python manage.py test partner_onboarding.tests_public --settings=config.sqlite_test_settings
"""

from unittest.mock import patch

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from sap_client.exceptions import SAPConnectionError

from .constants import AddressType, AttachmentKind, EventKind, RegistrationStatus
from .models import CustomerRegistration, RegistrationAttachment, VendorRegistration
from .testing import (
    BASE,
    GSTIN,
    PNG,
    FakeSAP,
    address,
    company,
    customer_files,
    customer_payload,
    multipart,
    pdf,
    vendor_files,
    vendor_payload,
)
from .throttles import PublicReadThrottle, PublicSubmitThrottle

CUSTOMERS = f"{BASE}public/customers/"
VENDORS = f"{BASE}public/vendors/"


class PublicTestCase(APITestCase):
    def setUp(self):
        cache.clear()  # the throttle counts live in the cache
        self.company = company("JIVO_OIL", "Jivo Oil")
        self.client = APIClient()

    def submit_customer(self, payload=None, files=None, **extra):
        return self.client.post(
            CUSTOMERS,
            multipart(payload or customer_payload(), customer_files() if files is None else files),
            format="multipart",
            **extra,
        )

    def submit_vendor(self, payload=None, files=None, **extra):
        return self.client.post(
            VENDORS,
            multipart(payload or vendor_payload(), vendor_files() if files is None else files),
            format="multipart",
            **extra,
        )


class PublicCompaniesTests(PublicTestCase):
    def test_lists_only_the_sap_companies_that_are_active_here(self):
        company("JIVO_BEVERAGES", "Jivo Beverages")
        mart = company("JIVO_MART", "Jivo Mart")
        mart.is_active = False
        mart.save()
        company("SOME_OTHER_UNIT", "Somewhere Else")
        response = self.client.get(f"{BASE}public/companies/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            [{"code": "JIVO_OIL", "name": "Jivo Oil"}, {"code": "JIVO_BEVERAGES", "name": "Jivo Beverages"}],
        )


@patch("partner_onboarding.views_public.SAPClient")
class PublicStatesTests(PublicTestCase):
    def test_reads_the_companys_own_states_once_then_from_cache(self, sap_client):
        fake = FakeSAP()
        sap_client.return_value = fake
        first = self.client.get(f"{BASE}public/states/?company=JIVO_OIL")
        second = self.client.get(f"{BASE}public/states/?company=jivo_oil")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data, [{"code": "HR", "name": "Haryana"}, {"code": "PB", "name": "Punjab"}])
        self.assertEqual(second.data, first.data)
        sap_client.assert_called_once_with(company_code="JIVO_OIL")

    def test_an_unlisted_company_is_refused_without_asking_sap(self, sap_client):
        response = self.client.get(f"{BASE}public/states/?company=JIVO_MART")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        sap_client.assert_not_called()

    def test_sap_down_is_a_plain_503(self, sap_client):
        fake = FakeSAP()
        fake.lookup_error = SAPConnectionError("hana://secret-host refused")
        sap_client.return_value = fake
        response = self.client.get(f"{BASE}public/states/?company=JIVO_OIL")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn("secret-host", str(response.data))


class CustomerSubmitTests(PublicTestCase):
    def test_an_anonymous_submission_becomes_a_pending_registration(self):
        response = self.submit_customer()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        registration = CustomerRegistration.objects.get()
        self.assertEqual(response.data["reference"], registration.reference)
        self.assertEqual(registration.company, self.company)
        self.assertEqual(registration.status, RegistrationStatus.PENDING)
        self.assertEqual(registration.card_name, "TEST TRADERS PVT LTD")
        self.assertEqual(registration.email, "buyer@example.com")
        # The portal's starting values for the approver.
        self.assertEqual(registration.card_code_prefix, "CUSTA")
        self.assertEqual(registration.control_account, "1101001")
        # "Same as billing" makes the shipping address a copy; the address name is
        # built from the business name and the SAP state code.
        bill, ship = registration.addresses.order_by("address_type")
        self.assertEqual((bill.address_type, ship.address_type), (AddressType.BILL_TO, AddressType.SHIP_TO))
        self.assertEqual(bill.address_name, "TEST TRADERS PVT LTD - HR")
        self.assertEqual(ship.street, bill.street)
        # The first billing address carries the registration's GSTIN.
        self.assertEqual(bill.gstin, GSTIN)
        # Documents are files, never base64 in the database.
        documents = list(registration.attachments.order_by("kind"))
        self.assertEqual(
            [d.kind for d in documents], [AttachmentKind.AADHAAR, AttachmentKind.CHEQUE, AttachmentKind.PAN]
        )
        for document in documents:
            self.assertTrue(document.file.name.startswith("partner_onboarding/customer/"))
            self.assertNotIn("TEST", document.file.name.upper().split("/")[-1])
            self.assertEqual(document.content_type, "application/pdf")
            with document.file.open("rb") as handle:
                self.assertTrue(handle.read().startswith(b"%PDF-"))
        event = registration.events.get()
        self.assertEqual(event.kind, EventKind.SUBMITTED)
        self.assertIsNone(event.actor)

    def test_a_stale_token_in_the_browser_does_not_break_the_form(self):
        response = self.submit_customer(HTTP_AUTHORIZATION="Bearer not-a-real-token")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_only_the_three_sap_companies_are_accepted(self):
        response = self.submit_customer(customer_payload(company="SOME_OTHER_UNIT"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("company", response.data)
        # A SAP company that does not exist on this server is refused too.
        response = self.submit_customer(customer_payload(company="JIVO_MART"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(CustomerRegistration.objects.exists())

    def test_gstin_is_required_for_b2b_but_not_b2c(self):
        response = self.submit_customer(customer_payload(gstin=""))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("gstin", response.data)
        response = self.submit_customer(customer_payload(gstin="", customer_type="B2C"))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_the_forms_formats_are_checked(self):
        for field, value in (("pan", "ABCDE12345"), ("gstin", "06ABCDE1234F1X5"), ("mobile", "call me")):
            with self.subTest(field=field):
                response = self.submit_customer(customer_payload(**{field: value}))
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)

    def test_msme_needs_a_udyam_number_and_its_certificate(self):
        payload = customer_payload(has_msme=True, msme_number="UDYAM-12345")
        response = self.submit_customer(payload, customer_files(msme=pdf("msme.pdf")))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("msme_number", response.data)
        payload = customer_payload(has_msme=True, msme_number="UDYAM-HR-18-0040140", msme_type="micro")
        response = self.submit_customer(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("msme", response.data["documents"])
        response = self.submit_customer(payload, customer_files(msme=pdf("msme.pdf")))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        registration = CustomerRegistration.objects.get()
        self.assertEqual(registration.msme_type, "MICRO")

    def test_the_required_documents(self):
        response = self.submit_customer(files=customer_files(aadhaar=None))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("aadhaar", response.data["documents"])
        self.assertFalse(CustomerRegistration.objects.exists())

    def test_files_are_judged_by_their_bytes_not_their_name(self):
        disguised = pdf("pan.pdf", content=PNG)
        response = self.submit_customer(files=customer_files(pan=disguised))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pan", response.data["documents"])
        self.assertNotIn("pan", response.data)  # the PAN number field is fine
        response = self.submit_customer(files=customer_files(pan=pdf("pan.exe")))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.submit_customer(files=customer_files(pan=pdf("pan.png", content=PNG)))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            RegistrationAttachment.objects.get(kind=AttachmentKind.PAN).content_type, "image/png"
        )

    def test_files_over_the_cap_are_refused(self):
        with patch("partner_onboarding.validators.MAX_ATTACHMENT_BYTES", 10):
            response = self.submit_customer()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("MB", str(response.data))

    def test_a_single_slot_takes_one_file_and_other_takes_several(self):
        response = self.submit_customer(files=customer_files(pan=[pdf("a.pdf"), pdf("b.pdf")]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.submit_customer(files=customer_files(other=[pdf("a.pdf"), pdf("b.pdf")]))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(RegistrationAttachment.objects.filter(kind=AttachmentKind.OTHER).count(), 2)

    def test_a_slot_the_form_does_not_have_is_refused(self):
        response = self.submit_customer(files=customer_files(gst=pdf("gst.pdf")))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("files", response.data["documents"])

    def test_values_sap_would_refuse_are_refused_here(self):
        response = self.submit_customer(customer_payload(card_name="X" * 101))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("card_name", response.data)
        response = self.submit_customer(
            customer_payload(contact_first_name="A" * 30, contact_last_name="B" * 30)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("contact_last_name", response.data)
        response = self.submit_customer(customer_payload(bill_addresses=[address(street="S" * 101)]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(CustomerRegistration.objects.exists())

    def test_two_billing_addresses_in_one_state_get_distinct_names(self):
        payload = customer_payload(
            bill_addresses=[address(), address(street="SECOND UNIT")],
            ship_same_as_bill=False,
            ship_addresses=[address(state="PB")],
        )
        response = self.submit_customer(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        registration = CustomerRegistration.objects.get()
        names = list(
            registration.addresses.filter(address_type=AddressType.BILL_TO)
            .order_by("position")
            .values_list("address_name", flat=True)
        )
        self.assertEqual(names, ["TEST TRADERS PVT LTD - HR", "TEST TRADERS PVT LTD - HR 2"])
        ship = registration.addresses.get(address_type=AddressType.SHIP_TO)
        self.assertEqual(ship.address_name, "TEST TRADERS PVT LTD - PB")

    def test_a_shipping_address_is_needed_unless_same_as_billing(self):
        response = self.submit_customer(customer_payload(ship_same_as_bill=False, ship_addresses=[]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ship_addresses", response.data)

    def test_an_unreadable_payload_is_a_400(self):
        response = self.client.post(CUSTOMERS, {"payload": "{not json", **customer_files()}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VendorSubmitTests(PublicTestCase):
    def test_a_vendor_registration_with_its_bank_account(self):
        response = self.submit_vendor()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        vendor = VendorRegistration.objects.get()
        self.assertEqual(vendor.reference, response.data["reference"])
        self.assertTrue(vendor.reference.startswith("VEND-"))
        # routes/vendors.js keeps a vendor's mobile as ten digits.
        self.assertEqual(vendor.mobile, "9000000001")
        self.assertEqual(vendor.card_code_prefix, "VENDA")
        self.assertEqual(vendor.control_account, "2110005")
        bank = vendor.bank_accounts.get()
        self.assertEqual((bank.account_number, bank.ifsc, bank.is_primary), ("000111222333", "TEST0001234", True))
        # The SAP bank code is the approver's to choose, never the public's.
        self.assertEqual(bank.sap_bank_code, "")
        self.assertEqual(
            sorted(vendor.attachments.values_list("kind", flat=True)),
            [AttachmentKind.CHEQUE, AttachmentKind.GST, AttachmentKind.PAN],
        )

    def test_gstin_pan_and_a_bank_account_are_required(self):
        for override in ({"gstin": ""}, {"pan": ""}, {"bank_accounts": []}):
            with self.subTest(override=override):
                response = self.submit_vendor(vendor_payload(**override))
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(next(iter(override)), response.data)

    def test_vendor_documents_include_the_gst_certificate(self):
        response = self.submit_vendor(files=vendor_files(gst=None))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("gst", response.data["documents"])
        response = self.submit_vendor(files=vendor_files(aadhaar=pdf("aadhaar.pdf")))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_vendor_formats(self):
        bad_bank = vendor_payload()
        bad_bank["bank_accounts"][0]["ifsc"] = "BAD"
        for payload, field in (
            (vendor_payload(fssai_number="123"), "fssai_number"),
            (vendor_payload(tan="NOTATAN"), "tan"),
            (bad_bank, "bank_accounts"),
        ):
            with self.subTest(field=field):
                response = self.submit_vendor(payload)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)

    def test_msme_vendors_need_type_and_business_type(self):
        payload = vendor_payload(has_msme=True, msme_number="UDYAM-HR-18-0040140")
        response = self.submit_vendor(payload, vendor_files(msme=pdf("msme.pdf")))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("msme_type", response.data)
        self.assertIn("msme_business_type", response.data)
        payload.update(msme_type="SMALL", msme_business_type="service")
        response = self.submit_vendor(payload, vendor_files(msme=pdf("msme.pdf")))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(VendorRegistration.objects.get().msme_business_type, "Service")


class ThrottleTests(PublicTestCase):
    def test_submissions_are_limited_per_client_address(self):
        with patch.object(PublicSubmitThrottle, "rate", "2/hour"):
            first = self.submit_customer(customer_payload(pan="bad"))  # invalid attempts count too
            second = self.submit_customer()
            third = self.submit_customer()
        self.assertEqual(first.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(third.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(CustomerRegistration.objects.count(), 1)

    def test_a_forged_forwarded_for_prefix_does_not_buy_a_new_allowance(self):
        with patch.object(PublicSubmitThrottle, "rate", "2/hour"):
            answers = [
                self.submit_customer(HTTP_X_FORWARDED_FOR=f"10.9.9.{n}, 203.0.113.7").status_code
                for n in range(3)
            ]
            other_client = self.submit_customer(HTTP_X_FORWARDED_FOR="198.51.100.4")
        self.assertEqual(answers[-1], status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(other_client.status_code, status.HTTP_201_CREATED)

    def test_the_forms_reads_have_their_own_limit(self):
        with patch.object(PublicReadThrottle, "rate", "1/hour"):
            self.assertEqual(self.client.get(f"{BASE}public/companies/").status_code, status.HTTP_200_OK)
            self.assertEqual(
                self.client.get(f"{BASE}public/companies/").status_code, status.HTTP_429_TOO_MANY_REQUESTS
            )
