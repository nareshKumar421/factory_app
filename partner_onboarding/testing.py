"""
Shared fixtures for the Partner Onboarding tests. Every name, GSTIN and PAN
here is made up (the GSTIN/PAN are the portal forms' own placeholder examples),
and SAP is always the ``FakeSAP`` below — nothing leaves the process.
"""

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from .constants import AddressType, AttachmentKind, RegistrationStatus
from .models import (
    CustomerRegistration,
    PartnerAddress,
    RegistrationAttachment,
    VendorBankAccount,
    VendorRegistration,
)

BASE = "/api/v1/partner-onboarding/"
GSTIN = "06ABCDE1234F1Z5"
PAN = "ABCDE1234F"
PDF = b"%PDF-1.4\n% partner onboarding test document\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16

CUSTOMER_RIGHTS = (
    "can_view_customer_registrations",
    "can_verify_customer_registrations",
    "can_approve_customer_registrations",
)
VENDOR_RIGHTS = (
    "can_view_vendor_registrations",
    "can_verify_vendor_registrations",
    "can_approve_vendor_registrations",
)


def pdf(name="document.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def address(**overrides):
    data = {
        "street": "PLOT 1, TEST INDUSTRIAL AREA",
        "block": "SECTOR 9",
        "city": "TESTNAGAR",
        "zip_code": "123456",
        "state": "HR",
        "country": "IN",
        "gstin": "",
    }
    data.update(overrides)
    return data


def customer_payload(**overrides):
    data = {
        "company": "JIVO_OIL",
        "customer_type": "B2B",
        "card_name": "Test Traders Pvt Ltd",
        "foreign_name": "",
        "type_of_business": "Company",
        "industry": "Trading",
        "contact_first_name": "Asha",
        "contact_last_name": "Example",
        "contact_title": "Owner",
        "mobile": "+91 90000 00001",
        "email": "Buyer@Example.com",
        "currency": "INR",
        "gstin": GSTIN,
        "pan": PAN,
        "has_msme": False,
        "remarks": "Test registration",
        "bill_addresses": [address()],
        "ship_same_as_bill": True,
        "ship_addresses": [],
    }
    data.update(overrides)
    return data


def vendor_payload(**overrides):
    data = customer_payload(card_name="Test Supplies Llp", industry="Manufacturing")
    data.pop("customer_type")
    data.update(
        vendor_type="SUPPLIER",
        tan="",
        fssai_number="",
        bank_accounts=[
            {
                "bank_name": "Test Bank",
                "branch": "Main Branch",
                "account_number": "000111222333",
                "ifsc": "TEST0001234",
                "account_type": "Current",
                "swift_code": "",
                "sap_bank_code": "SHOULD-BE-IGNORED",
            }
        ],
    )
    data.update(overrides)
    return data


def customer_files(**overrides):
    files = {"pan": pdf("pan.pdf"), "aadhaar": pdf("aadhaar.pdf"), "cheque": pdf("cheque.pdf")}
    files.update(overrides)
    return {key: value for key, value in files.items() if value is not None}


def vendor_files(**overrides):
    files = {"pan": pdf("pan.pdf"), "cheque": pdf("cheque.pdf"), "gst": pdf("gst.pdf")}
    files.update(overrides)
    return {key: value for key, value in files.items() if value is not None}


def multipart(payload, files):
    return {"payload": json.dumps(payload), **files}


def company(code="JIVO_OIL", name=None):
    return Company.objects.get_or_create(code=code, defaults={"name": name or code.replace("_", " ").title()})[0]


def make_customer(company, status=RegistrationStatus.PENDING, with_documents=True, **fields):
    values = {
        "card_name": "TEST TRADERS PVT LTD",
        "industry": "TRADING",
        "type_of_business": "Company",
        "contact_first_name": "ASHA",
        "contact_last_name": "EXAMPLE",
        "mobile": "+91 90000 00001",
        "email": "buyer@example.com",
        "gstin": GSTIN,
        "pan": PAN,
        "remarks": "TEST",
        "card_code_prefix": "CUSTA",
        "control_account": "1101001",
        "control_account_name": "SUNDRY DEBTORS GT",
    }
    values.update(fields)
    registration = CustomerRegistration.objects.create(company=company, status=status, **values)
    PartnerAddress.objects.create(
        customer=registration,
        address_type=AddressType.BILL_TO,
        address_name=f"{registration.card_name} - HR",
        street="PLOT 1",
        city="TESTNAGAR",
        zip_code="123456",
        state="HR",
    )
    PartnerAddress.objects.create(
        customer=registration,
        address_type=AddressType.SHIP_TO,
        address_name=f"{registration.card_name} - HR",
        street="PLOT 1",
        city="TESTNAGAR",
        zip_code="123456",
        state="HR",
    )
    if with_documents:
        for kind, name in (
            (AttachmentKind.PAN, "pan.pdf"),
            (AttachmentKind.AADHAAR, "aadhaar.pdf"),
            (AttachmentKind.CHEQUE, "cheque.pdf"),
        ):
            attach(registration, kind, name)
    return registration


def make_vendor(company, status=RegistrationStatus.PENDING, with_documents=True, bank_code="", **fields):
    values = {
        "card_name": "TEST SUPPLIES LLP",
        "industry": "MANUFACTURING",
        "type_of_business": "LLP",
        "contact_first_name": "RAVI",
        "contact_last_name": "SAMPLE",
        "mobile": "9000000002",
        "email": "vendor@example.com",
        "gstin": GSTIN,
        "pan": PAN,
        "remarks": "TEST",
        "card_code_prefix": "VENDA",
        "control_account": "2110005",
    }
    values.update(fields)
    registration = VendorRegistration.objects.create(company=company, status=status, **values)
    for kind in (AddressType.BILL_TO, AddressType.SHIP_TO):
        PartnerAddress.objects.create(
            vendor=registration,
            address_type=kind,
            address_name="TEST SUPPLIES LLP - HR",
            street="PLOT 2",
            city="TESTNAGAR",
            zip_code="123456",
            state="HR",
        )
    VendorBankAccount.objects.create(
        vendor=registration,
        bank_name="TEST BANK",
        branch="MAIN BRANCH",
        account_number="000111222333",
        ifsc="TEST0001234",
        is_primary=True,
        sap_bank_code=bank_code,
    )
    if with_documents:
        for kind, name in ((AttachmentKind.PAN, "pan.pdf"), (AttachmentKind.GST, "gst.pdf")):
            attach(registration, kind, name)
    return registration


def attach(registration, kind, name="document.pdf", content=PDF, content_type="application/pdf"):
    attachment = RegistrationAttachment(
        **{registration.family: registration},
        kind=kind,
        original_name=name,
        content_type=content_type,
        size=len(content),
    )
    attachment.file.save(name, SimpleUploadedFile(name, content), save=False)
    attachment.save()
    return attachment


class FakeSAP:
    """Stands in for ``SAPClient``: records every call, answers as configured."""

    def __init__(self, company_code="JIVO_OIL"):
        self.company_code = company_code
        self.partners = {}
        self.tax_matches = []
        self.next_codes = {}
        self.created = []
        self.uploads = []
        self.calls = []
        self.create_error = None
        self.upload_error = None
        self.add_line_error = None
        self.lookup_error = None
        self.on_create = None
        self.entry = 555

    def business_partner(self, card_code):
        self.calls.append(("business_partner", card_code))
        if self.lookup_error:
            raise self.lookup_error
        return self.partners.get(card_code)

    def partners_with_tax_ids(self, card_type, gstin="", pan=""):
        self.calls.append(("partners_with_tax_ids", card_type, gstin, pan))
        if self.lookup_error:
            raise self.lookup_error
        return self.tax_matches

    def next_card_code(self, prefix, card_type):
        self.calls.append(("next_card_code", prefix, card_type))
        return self.next_codes.get(prefix, f"{prefix}000124")

    def upload_attachment(self, file_path, filename, **kwargs):
        self.calls.append(("upload_attachment", filename))
        if self.upload_error:
            raise self.upload_error
        with open(file_path, "rb") as handle:
            assert handle.read(5) == b"%PDF-"
        self.uploads.append(("new", filename))
        return {"AbsoluteEntry": self.entry}

    def add_line_to_existing_attachment(self, absolute_entry, file_path, filename, **kwargs):
        self.calls.append(("add_line", absolute_entry, filename))
        if self.add_line_error:
            raise self.add_line_error
        self.uploads.append((absolute_entry, filename))
        return {"AbsoluteEntry": absolute_entry}

    def create_business_partner(self, payload):
        self.calls.append(("create_business_partner", payload["CardCode"]))
        if self.on_create:
            self.on_create(payload)
        if self.create_error:
            raise self.create_error
        self.created.append(payload)
        card_type = "C" if payload["CardType"] == "cCustomer" else "S"
        self.partners[payload["CardCode"]] = {
            "card_code": payload["CardCode"],
            "card_name": payload["CardName"],
            "card_type": card_type,
            "active": True,
        }
        return {"card_code": payload["CardCode"], "card_name": payload["CardName"], "attachment_entry": None}

    def lookup_states(self, country="IN"):
        self.calls.append(("lookup_states", country))
        if self.lookup_error:
            raise self.lookup_error
        return [{"code": "HR", "name": "Haryana"}, {"code": "PB", "name": "Punjab"}]


class InternalTestCase(APITestCase):
    """A company, a user in it, and whatever rights a test grants."""

    rights: tuple = CUSTOMER_RIGHTS + VENDOR_RIGHTS

    def setUp(self):
        self.company = company("JIVO_OIL", "Jivo Oil")
        self.other_company = company("JIVO_MART", "Jivo Mart")
        self.user = get_user_model().objects.create_user(
            email="approver@example.com", password="x", full_name="Test Approver", employee_code="PO-T1"
        )
        role = UserRole.objects.create(name="Staff")
        UserCompany.objects.create(user=self.user, company=self.company, role=role, is_default=True)
        UserCompany.objects.create(user=self.user, company=self.other_company, role=role)
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}
        self.grant(*self.rights)

    def grant(self, *codenames, reset=False):
        """Set rights, then re-fetch the user: has_perm() caches per instance."""
        if reset:
            self.user.user_permissions.clear()
        if codenames:
            self.user.user_permissions.add(
                *Permission.objects.filter(content_type__app_label="partner_onboarding", codename__in=codenames)
            )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def only(self, *codenames):
        self.grant(*codenames, reset=True)

    def get(self, path, **extra):
        return self.client.get(f"{BASE}{path}", **self.headers, **extra)

    def post(self, path, data=None, **extra):
        return self.client.post(f"{BASE}{path}", data or {}, format="json", **self.headers, **extra)

    def patch(self, path, data, **extra):
        return self.client.patch(f"{BASE}{path}", data, format="json", **self.headers, **extra)


def stale(minutes=11):
    return timezone.now() - timedelta(minutes=minutes)

