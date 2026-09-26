"""
The SAP Portal importers, on small made-up exports keyed like the portal's
ZCUST_PORTAL / ZVENDOR_PORTAL columns.

    DEBUG=False python manage.py test partner_onboarding.tests_import --settings=config.sqlite_test_settings
"""

import base64
import json
import os
import tempfile
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .constants import AddressType, AttachmentKind, EventKind, RegistrationStatus
from .families import CUSTOMER_FAMILY
from .models import CustomerRegistration, RegistrationAttachment, VendorRegistration
from .services import workflow
from .testing import GSTIN, PAN, PDF, PNG, company


def data_url(content, mime):
    return f"data:{mime};base64,{base64.b64encode(content).decode()}"


def customer_rows():
    bill = [
        {
            "addrName": "TEST TRADERS PVT LTD - HR",
            "street": "Plot 1",
            "block": "",
            "city": "Testnagar",
            "zip": "123456",
            "state": "Haryana",
            "country": "India",
            "gstin": "",
        }
    ]
    return [
        {
            "ID": 11,
            "COMPANY": "JIVO_OIL_HANADB",
            "STATUS": "PENDING",
            "CARD_NAME": "Test Traders Pvt Ltd",
            "CUSTOMER_TYPE": "B2B",
            "TYPE_OF_BUSINESS": "Company",
            "INDUSTRY": "Trading",
            "MOBILE": "+91 90000 00001",
            "EMAIL": "Buyer@Example.com",
            "CONTACT_FIRST": "ASHA",
            "CONTACT_LAST": "EXAMPLE",
            "GSTIN": GSTIN,
            "PAN": PAN,
            "CURRENCY": "Indian Rupee",
            "REMARKS": "TEST",
            "HAS_MSME": 0,
            "SUBMITTED_AT": "2026-03-01 09:30:15.123000000",
            "SAME_AS_BILL": 1,
            "ALL_BILL_ADDRS": json.dumps(bill),
            "ALL_SHIP_ADDRS": json.dumps(bill),
            "ATTACHMENTS": json.dumps(
                {
                    "pan": {"name": "pan.pdf", "size": 10, "type": "application/pdf", "data": data_url(PDF, "application/pdf")},
                    "aadhaar": {"name": "aadhaar.pdf", "type": "application/pdf", "data": data_url(PDF, "application/pdf")},
                    "cheque": None,
                    "other": [{"name": "photo", "type": "image/png", "data": data_url(PNG, "image/png")}],
                }
            ),
            "MGR_PREFIX": "CUSTA",
            "MGR_AR_ACCOUNT": "1101001",
            "MGR_AR_ACC_NAME": "SUNDRY DEBTORS GT",
            "MGR_CURRENCY": "Indian Rupee",
            "MGR_LANGUAGE": "English (UK)",
            "MGR_ZONE": "NORTH",
            "MGR_CREDIT_LMT": 0,
        },
        {
            "ID": 12,
            "COMPANY": "JIVO_OIL_HANADB",
            "STATUS": "APPROVED",
            "CARD_NAME": "Test Foods",
            "CUSTOMER_TYPE": "DISTRIBUTO",
            "MOBILE": "9000000003",
            "EMAIL": "foods@example.com",
            "CONTACT_FIRST": "RAVI",
            "CONTACT_LAST": "SAMPLE",
            "GSTIN": "",
            "PAN": PAN,
            "SUBMITTED_AT": "2026-02-01T08:00:00.000Z",
            "VERIFIED_AT": "2026-02-02 10:00:00",
            "APPROVED_AT": "2026-02-03 11:00:00",
            "APPROVED_BY": "sapadder",
            "SAP_CARD_CODE": "CUSTA000900",
            "SAP_ATT_ENTRY": 77,
            "ALL_BILL_ADDRS": "[]",
            "ALL_SHIP_ADDRS": "",
            "BILL_ADDR_NAME": "TEST FOODS",
            "BILL_STREET": "Shop 4",
            "BILL_CITY": "Samplepur",
            "BILL_STATE": "Punjab",
            "BILL_COUNTRY": "India",
            "MGR_GROUP_CODE": "105",
            "MGR_SLP_CODE": "12",
            "MGR_CREDIT_LMT": 1000.5,
            "MGR_CURRENCY": "US Dollar",
            "ATTACHMENTS": "{}",
        },
        {"ID": 13, "COMPANY": "SOMEWHERE_HANADB", "STATUS": "PENDING", "CARD_NAME": "Far Away Stores"},
        {"ID": 14, "COMPANY": "JIVO_OIL_HANADB", "STATUS": "REVIEWED", "CARD_NAME": "Odd Status Co"},
    ]


def vendor_rows():
    return [
        {
            "ID": 21,
            "COMPANY": "JIVO_MART_HANADB",
            "STATUS": "REJECTED",
            "VENDOR_TYPE": "SERVICE",
            "CARD_NAME": "Test Supplies Llp",
            "MOBILE": "9000000002",
            "EMAIL": "vendor@example.com",
            "CONTACT_FIRST": "MEERA",
            "CONTACT_LAST": "SAMPLE",
            "BILL_STREET": "Plot 2",
            "BILL_CITY": "Testnagar",
            "BILL_STATE": "Haryana",
            "BILL_COUNTRY": "India",
            "GSTIN": GSTIN,
            "PAN": PAN,
            "HAS_MSME": "Y",
            "MSME_NO": "UDYAM-HR-18-0040140",
            "MSME_TYPE": "SMALL",
            "MSME_BTYPE": "Service",
            "HAS_TDS": "N",
            "TDS_RATE": 2,
            "BANK_ACCOUNTS": json.dumps(
                [
                    {
                        "bankName": "TEST BANK",
                        "accNo": "0001 112",
                        "ifsc": "TEST0001234",
                        "accountType": "Savings",
                        "branch": "MAIN",
                        "swiftCode": "",
                        "isPrimary": True,
                        "bankCode": "TST",
                    },
                    {"bankName": "EMPTY", "accNo": ""},
                ]
            ),
            "ATTACHMENTS": json.dumps({"gst": {"name": "gst.pdf", "data": data_url(PDF, "application/pdf")}}),
            "SUBMITTED_AT": "2026-01-05 07:00:00",
            "REJECTED_BY": "manager1",
            "REJECTED_AT": "2026-01-06 07:00:00",
            "MGR_CARD_CODE_PREFIX": "VENDA",
            "MGR_PURCHASE_ACCOUNT": "2110005",
            "MGR_SALES_PERSON_CODE": 7,
            "MGR_BRANCH": "HQ",
        },
        {
            "ID": 22,
            "COMPANY": "JIVO_MART_HANADB",
            "STATUS": "VERIFIED",
            "CARD_NAME": "Second Supplier",
            "BILL_STREET": "Unit 9",
            "BILL_CITY": "Testnagar",
            "BILL_STATE": "PB",
            "VERIFIED_BY": "manager1",
            "VERIFIED_AT": "2026-01-07 07:00:00",
            "ATTACHMENTS": json.dumps({"pan": {"name": "pan.pdf", "data": "@@@"}}),
        },
    ]


class ImportTestCase(TestCase):
    def setUp(self):
        self.oil = company("JIVO_OIL", "Jivo Oil")
        self.mart = company("JIVO_MART", "Jivo Mart")
        self.operator = get_user_model().objects.create_user(
            email="ops@example.com", password="x", full_name="Import Operator", employee_code="PO-OPS"
        )
        self.directory = tempfile.mkdtemp(prefix="partner-import-")

    def export(self, rows, name="export.json"):
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        return path

    def run_import(self, command, path, *args):
        out = StringIO()
        call_command(command, "--from-file", path, *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def real(self, command, path, *args):
        return self.run_import(command, path, "--yes", "--actor", "ops@example.com", *args)


class CustomerImportTests(ImportTestCase):
    def test_a_dry_run_reports_and_writes_nothing(self):
        output = self.run_import("import_portal_customers", self.export(customer_rows()), "--dry-run")
        self.assertIn("ZCUST_PORTAL: 4 row(s)", output)
        self.assertIn("ID 13: skipped - unknown company SOMEWHERE_HANADB", output)
        self.assertIn("ID 14: cannot import - unknown status 'REVIEWED'", output)
        self.assertIn("Would", output)
        self.assertIn("created", output)
        self.assertIn("Dry run - nothing written.", output)
        self.assertFalse(CustomerRegistration.objects.exists())
        self.assertFalse(RegistrationAttachment.objects.exists())

    def test_a_real_run_needs_yes_an_actor_and_a_clean_file(self):
        path = self.export(customer_rows()[:2])
        with self.assertRaisesMessage(CommandError, "--yes"):
            self.run_import("import_portal_customers", path, "--actor", "ops@example.com")
        with self.assertRaisesMessage(CommandError, "--actor"):
            self.run_import("import_portal_customers", path, "--yes")
        with self.assertRaisesMessage(CommandError, "cannot be read"):
            self.real("import_portal_customers", self.export(customer_rows(), "all.json"))
        self.assertFalse(CustomerRegistration.objects.exists())

    def test_rows_land_with_their_addresses_documents_and_history(self):
        output = self.real("import_portal_customers", self.export(customer_rows()), "--skip-invalid")
        self.assertIn("Import committed.", output)
        self.assertEqual(CustomerRegistration.objects.count(), 2)

        pending = CustomerRegistration.objects.get(legacy_portal_id=11)
        self.assertEqual((pending.company, pending.status), (self.oil, RegistrationStatus.PENDING))
        self.assertEqual(pending.card_name, "TEST TRADERS PVT LTD")
        self.assertEqual(pending.email, "buyer@example.com")
        self.assertEqual(pending.currency, "INR")
        self.assertEqual(
            pending.submitted_at, datetime(2026, 3, 1, 9, 30, 15, 123000, tzinfo=dt_timezone.utc)
        )
        bill = pending.addresses.get(address_type=AddressType.BILL_TO)
        self.assertEqual(
            (bill.address_name, bill.street, bill.state, bill.country, bill.gstin),
            ("TEST TRADERS PVT LTD - HR", "PLOT 1", "HR", "IN", GSTIN),
        )
        self.assertEqual(pending.addresses.filter(address_type=AddressType.SHIP_TO).count(), 1)
        documents = {d.kind: d for d in pending.attachments.all()}
        self.assertEqual(set(documents), {AttachmentKind.PAN, AttachmentKind.AADHAAR, AttachmentKind.OTHER})
        with documents[AttachmentKind.PAN].file.open("rb") as handle:
            self.assertEqual(handle.read(), PDF)
        self.assertEqual(documents[AttachmentKind.OTHER].original_name, "photo.png")
        self.assertEqual(documents[AttachmentKind.OTHER].content_type, "image/png")
        self.assertEqual(pending.legacy_fields["MGR_ZONE"], "NORTH")
        self.assertEqual(pending.legacy_fields["MGR_LANGUAGE"], "English (UK)")
        self.assertNotIn("ATTACHMENTS", pending.legacy_fields)
        events = list(pending.events.order_by("at", "id"))
        self.assertEqual([e.kind for e in events], [EventKind.SUBMITTED, EventKind.IMPORTED])
        self.assertTrue(all(e.data.get("imported") for e in events))
        self.assertEqual(events[-1].actor, self.operator)

        approved = CustomerRegistration.objects.get(legacy_portal_id=12)
        self.assertEqual(approved.status, RegistrationStatus.APPROVED)
        self.assertEqual((approved.card_code, approved.sap_card_code), ("CUSTA000900", "CUSTA000900"))
        self.assertEqual(approved.customer_type, "DISTRIBUTOR")  # the portal's NVARCHAR(10) cut it
        self.assertEqual((approved.bp_group_code, approved.sales_employee_code), (105, 12))
        self.assertEqual(approved.credit_limit, Decimal("1000.50"))
        self.assertEqual(approved.sap_currency, "USD")
        self.assertEqual(approved.sap_attachment_entry, 77)
        self.assertIsNotNone(approved.verified_at)
        self.assertEqual(approved.approved_at, datetime(2026, 2, 3, 11, 0, tzinfo=dt_timezone.utc))
        bill = approved.addresses.get(address_type=AddressType.BILL_TO)
        ship = approved.addresses.get(address_type=AddressType.SHIP_TO)
        self.assertEqual((bill.address_name, bill.state), ("TEST FOODS", "PB"))
        self.assertEqual((ship.street, ship.state), ("SHOP 4", "PB"))
        created = approved.events.get(kind=EventKind.SAP_CREATED)
        self.assertEqual(created.actor_name, "sapadder")
        self.assertIn("CUSTA000900", created.note)

    def test_running_it_again_changes_nothing(self):
        path = self.export(customer_rows()[:2])
        self.real("import_portal_customers", path)
        files = RegistrationAttachment.objects.count()
        output = self.real("import_portal_customers", path)
        self.assertIn("already imported", output)
        self.assertEqual(CustomerRegistration.objects.count(), 2)
        self.assertEqual(RegistrationAttachment.objects.count(), files)

    def test_update_refreshes_only_rows_nobody_has_touched_here(self):
        rows = customer_rows()[:2]
        self.real("import_portal_customers", self.export(rows))
        pending = CustomerRegistration.objects.get(legacy_portal_id=11)
        workflow.verify(CUSTOMER_FAMILY, pending.pk, self.oil, self.operator)
        rows[0]["INDUSTRY"] = "Retail"
        rows[1]["MOBILE"] = "9000000099"
        output = self.real("import_portal_customers", self.export(rows, "again.json"), "--update")
        self.assertIn("changed in JI", output)
        self.assertIn("updated", output)
        pending.refresh_from_db()
        self.assertEqual((pending.industry, pending.status), ("Trading", RegistrationStatus.VERIFIED))
        approved = CustomerRegistration.objects.get(legacy_portal_id=12)
        self.assertEqual(approved.mobile, "9000000099")
        self.assertEqual(approved.events.filter(kind=EventKind.IMPORTED).count(), 1)
        self.assertEqual(CustomerRegistration.objects.count(), 2)

    def test_a_default_company_takes_the_rows_with_an_unknown_database(self):
        output = self.real(
            "import_portal_customers", self.export(customer_rows()[2:3]), "--default-company", "JIVO_OIL"
        )
        self.assertIn("not a known SAP database; used JIVO_OIL", output)
        self.assertEqual(CustomerRegistration.objects.get(legacy_portal_id=13).company, self.oil)

    def test_a_file_that_is_not_an_export_is_refused(self):
        path = os.path.join(self.directory, "bad.json")
        with open(path, "w") as handle:
            handle.write('{"not": "rows"}')
        with self.assertRaisesMessage(CommandError, "JSON array"):
            self.run_import("import_portal_customers", path, "--dry-run")


class VendorImportTests(ImportTestCase):
    def test_vendor_rows_with_bank_accounts_and_the_portals_address_name(self):
        output = self.real("import_portal_vendors", self.export(vendor_rows()))
        self.assertIn("ZVENDOR_PORTAL: 2 row(s)", output)
        self.assertIn("could not be decoded", output.replace("is empty", "could not be decoded"))

        rejected = VendorRegistration.objects.get(legacy_portal_id=21)
        self.assertEqual((rejected.company, rejected.status), (self.mart, RegistrationStatus.REJECTED))
        self.assertEqual(rejected.vendor_type, "SERVICE")
        self.assertTrue(rejected.has_msme)
        self.assertEqual(rejected.msme_business_type, "Service")
        self.assertEqual(rejected.tds_rate, Decimal("2.00"))
        self.assertEqual(rejected.sales_employee_code, 7)
        self.assertEqual(rejected.control_account, "2110005")
        self.assertEqual(rejected.rejected_at, datetime(2026, 1, 6, 7, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(rejected.legacy_fields["MGR_BRANCH"], "HQ")
        bill = rejected.addresses.get(address_type=AddressType.BILL_TO)
        ship = rejected.addresses.get(address_type=AddressType.SHIP_TO)
        # createVendor's own name for its single address.
        self.assertEqual(bill.address_name, "TEST SUPPLIES LLP-HR")
        self.assertEqual((ship.address_name, ship.gstin), ("TEST SUPPLIES LLP-HR", GSTIN))
        bank = rejected.bank_accounts.get()
        self.assertEqual(
            (bank.account_number, bank.account_type, bank.sap_bank_code), ("0001112", "Savings", "TST")
        )
        self.assertEqual(rejected.attachments.get().kind, AttachmentKind.GST)
        self.assertEqual(rejected.events.get(kind=EventKind.REJECTED).actor_name, "manager1")

        verified = VendorRegistration.objects.get(legacy_portal_id=22)
        self.assertEqual(verified.status, RegistrationStatus.VERIFIED)
        self.assertEqual(verified.events.get(kind=EventKind.VERIFIED).actor_name, "manager1")
        self.assertFalse(verified.attachments.exists())
        self.assertEqual(verified.addresses.get(address_type=AddressType.BILL_TO).state, "PB")
