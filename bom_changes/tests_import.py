"""
``import_portal_bom_requests``: SAP Portal's ZBOM_REQUESTS rows into BOM Changes.

    DEBUG=False python manage.py test bom_changes.tests_import --settings=config.sqlite_test_settings

The rows below are shaped as the portal stored them (``services/bomRequestStore.js``):
JSON columns as text, timestamps in UTC without a zone, components as the
page posted them, approval-log entries as ``PATCH /:id/action`` wrote them.
"""

import json
import os
import tempfile
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from company.models import Company

from .models import BOMChangeApproval, BOMChangeLine, BOMChangeRequest
from .portal_import import RowProblem, company_codes_by_db, parse_row, parse_timestamp

COMPANY_DB = {
    "JIVO_OIL": "JIVO_OIL_HANADB",
    "JIVO_MART": "JIVO_MART_HANADB",
    "JIVO_BEVERAGES": "JIVO_BEVERAGES_HANADB",
}

PUSHED_DIRECT = {
    "ID": 11,
    "TYPE": "CREATE",
    "ITEM_CODE": "FG0000121",
    "ITEM_NAME": "CANOLA OIL 1 LTR 20 PCS",
    "QTY": "20.0000",
    "BOM_TYPE": "Production",
    "WAREHOUSE": "BH-PF",
    "DISTR_RULE": "",
    "PROJECT": "",
    "COMPONENTS": json.dumps([
        {"itemCode": "RM0001", "itemName": "Canola oil", "qty": 20, "uom": "LTR", "warehouse": "BH-PC",
         "issueMethod": "Backflush", "unitCost": 150.5, "itemType": "pit_Item"},
        {"itemCode": "JWPL09240001", "itemName": "Filling cost", "qty": 20, "issueMethod": "Stock",
         "unitCost": 0, "itemType": "pit_Resource"},
        {"itemCode": "", "qty": 1},
    ]),
    "ORIGINAL_DATA": None,
    "STATUS": "SAP_PUSHED",
    "SUBMITTED_BY": "admin1",
    "SUBMITTED_NAME": "Portal Admin",
    "SUBMITTED_AT": "2026-05-01 10:22:33.123000000",
    "APPROVAL_LOG": json.dumps([
        {"username": "admin1", "name": "Portal Admin", "role": "admin", "action": "approve",
         "comment": "Admin direct push", "status": "PENDING", "timestamp": "2026-05-01T10:22:35.000Z"},
    ]),
    "REJECTED_BY": None,
    "REJECTED_AT": None,
    "SAP_PUSHED_AT": "2026-05-01 10:22:36.000000000",
    "SAP_PUSHED_BY": "admin1",
    "SAP_RESULT": json.dumps({"treeCode": "FG0000121", "operation": "CREATED"}),
    "COMPANY": "JIVO_OIL_HANADB",
}

OPEN_UPDATE = {
    "ID": 12,
    "TYPE": "UPDATE",
    "ITEM_CODE": "fg0000200",
    "ITEM_NAME": "MUSTARD OIL",
    "QTY": 1,
    "BOM_TYPE": "production",
    "WAREHOUSE": "",
    "COMPONENTS": json.dumps([
        {"itemCode": "rm0100", "qty": 2, "issueMethod": "Manual", "warehouse": "", "itemType": "pit_Item",
         "visualOrder": 0},
    ]),
    "ORIGINAL_DATA": json.dumps({"treeCode": "FG0000200", "description": "MUSTARD OIL"}),
    "STATUS": "L1_APPROVED",
    "SUBMITTED_BY": "req1",
    "SUBMITTED_NAME": "Requester One",
    "SUBMITTED_AT": "2026-06-02 04:00:00",
    "APPROVAL_LOG": json.dumps([
        {"username": "mgr1", "name": "Manager One", "role": "manager", "action": "approve", "comment": "",
         "status": "PENDING", "timestamp": "2026-06-02T05:00:00.000Z"},
    ]),
    "SAP_RESULT": "[]",
    "COMPANY": "JIVO_MART_HANADB",
}

REJECTED = {
    "ID": 13,
    "TYPE": "CREATE",
    "ITEM_CODE": "FG0000300",
    "ITEM_NAME": "X",
    "QTY": 1,
    "STATUS": "REJECTED",
    "COMPONENTS": json.dumps([{"itemCode": "RM1", "qty": 1}]),
    "SUBMITTED_BY": "req1",
    "SUBMITTED_AT": "2026-06-03 04:00:00",
    "APPROVAL_LOG": "[]",
    "REJECTED_BY": "mgr2",
    "REJECTED_AT": "2026-06-03 06:00:00",
    "COMPANY": "JIVO_OIL_HANADB",
}

NO_COMPANY = dict(REJECTED, ID=14, STATUS="CANCELLED", COMPANY=None, REJECTED_BY=None)
UNKNOWN_COMPANY = dict(REJECTED, ID=15, COMPANY="TEST_JIVO_OIL_HANADB")
DRAFT = dict(REJECTED, ID=16, STATUS="DRAFT")


@override_settings(COMPANY_DB=COMPANY_DB)
class ParseTests(SimpleTestCase):
    def setUp(self):
        self.db_to_code = company_codes_by_db()

    def test_company_database_maps_back_to_the_code(self):
        self.assertEqual(self.db_to_code["JIVO_MART_HANADB"], "JIVO_MART")

    def test_a_pushed_direct_create(self):
        item = parse_row(PUSHED_DIRECT, self.db_to_code)
        self.assertEqual((item.legacy_id, item.company_code), (11, "JIVO_OIL"))
        self.assertEqual(item.header["quantity"], Decimal("20.0000"))
        self.assertEqual(item.header["legacy_submitted_by"], "Portal Admin (admin1)")
        self.assertEqual(item.header["submitted_at"], datetime(2026, 5, 1, 10, 22, 33, 123000, tzinfo=dt_timezone.utc))
        self.assertEqual(
            [(l["item_code"], l["item_type"], l["issue_method"]) for l in item.lines],
            [("RM0001", "item", "Backflush"), ("JWPL09240001", "resource", "Backflush")],  # Stock → backflush
        )
        self.assertIn("1 component(s) without an item code left out", item.notes)
        self.assertEqual(item.approvals[0]["level"], 0)  # the admin's push skipped the levels
        self.assertEqual(item.header["sap_result"], {"treeCode": "FG0000121", "operation": "CREATED"})

    def test_an_open_update(self):
        item = parse_row(OPEN_UPDATE, self.db_to_code)
        self.assertEqual(item.header["item_code"], "FG0000200")
        self.assertEqual(item.header["bom_type"], "Production")
        self.assertIsNone(item.header["sap_result"])  # the portal stored [] for "none"
        self.assertEqual(item.header["original_data"]["treeCode"], "FG0000200")
        self.assertEqual((item.approvals[0]["level"], item.approvals[0]["from_status"]), (1, "PENDING"))
        self.assertEqual(item.lines[0]["item_code"], "RM0100")

    def test_a_rejection_missing_from_the_log_comes_from_rejected_by(self):
        item = parse_row(REJECTED, self.db_to_code)
        self.assertEqual([(a["action"], a["legacy_decided_by"]) for a in item.approvals], [("REJECT", "mgr2")])

    def test_rows_that_cannot_be_imported(self):
        for row, reason in (
            (NO_COMPANY, "COMPANY is empty"),
            (UNKNOWN_COMPANY, "not one of settings.COMPANY_DB"),
            (DRAFT, "STATUS DRAFT"),
            (dict(REJECTED, TYPE="DELETE"), "TYPE DELETE"),
            (dict(REJECTED, ITEM_CODE=""), "ITEM_CODE is empty"),
            (dict(REJECTED, COMPONENTS="{not json"), "COMPONENTS is not valid JSON"),
            (dict(REJECTED, ID="x"), "not a number"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesMessage(RowProblem, reason):
                    parse_row(row, self.db_to_code)

    def test_an_empty_company_goes_to_the_default_when_named(self):
        item = parse_row(NO_COMPANY, self.db_to_code, default_company="JIVO_OIL")
        self.assertEqual(item.company_code, "JIVO_OIL")

    def test_timestamps(self):
        self.assertEqual(parse_timestamp("2026-06-02T05:00:00.000Z").utcoffset().total_seconds(), 0)
        self.assertEqual(parse_timestamp("2026-06-02 05:00:00").tzinfo, dt_timezone.utc)
        self.assertIsNone(parse_timestamp(""))
        with self.assertRaises(RowProblem):
            parse_timestamp("yesterday")


@override_settings(COMPANY_DB=COMPANY_DB)
class ImportCommandTests(TestCase):
    def setUp(self):
        Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        handle, self.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            # A tool export: one query name holding the rows.
            json.dump({"SELECT * FROM ZBOM_REQUESTS": [PUSHED_DIRECT, OPEN_UPDATE, REJECTED, NO_COMPANY,
                                                       UNKNOWN_COMPANY, DRAFT]}, f)
        self.addCleanup(os.remove, self.path)

    def run_import(self, *extra):
        out = StringIO()
        call_command("import_portal_bom_requests", "--from-file", self.path, *extra, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing_and_says_what_it_would_skip(self):
        output = self.run_import("--dry-run")
        self.assertFalse(BOMChangeRequest.objects.exists())
        self.assertIn("Importable: 3", output)
        self.assertIn("skip 14: COMPANY is empty", output)
        self.assertIn("skip 16: STATUS DRAFT", output)
        self.assertIn("DRY RUN", output)

    def test_a_real_run_needs_yes(self):
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(BOMChangeRequest.objects.exists())

    def test_import_and_import_again(self):
        output = self.run_import("--yes")
        self.assertIn("imported 3", output)
        self.assertEqual(BOMChangeRequest.objects.count(), 3)

        pushed = BOMChangeRequest.objects.get(legacy_portal_id=11)
        self.assertEqual((pushed.company.code, pushed.status, pushed.kind), ("JIVO_OIL", "SAP_PUSHED", "CREATE"))
        self.assertIsNone(pushed.created_by)
        self.assertEqual(pushed.legacy_sap_pushed_by, "admin1")
        self.assertEqual(pushed.sap_pushed_at, datetime(2026, 5, 1, 10, 22, 36, tzinfo=dt_timezone.utc))
        self.assertEqual(pushed.lines.count(), 2)
        self.assertEqual(pushed.lines.get(item_code="RM0001").unit_cost, Decimal("150.5"))

        open_update = BOMChangeRequest.objects.get(legacy_portal_id=12)
        self.assertEqual((open_update.company.code, open_update.status), ("JIVO_MART", "L1_APPROVED"))
        decision = open_update.approvals.get()
        self.assertEqual((decision.level, decision.legacy_decided_by, decision.decided_by),
                         (1, "Manager One (mgr1)", None))
        self.assertEqual(BOMChangeApproval.objects.count(), 3)
        self.assertEqual(BOMChangeLine.objects.count(), 4)

        again = self.run_import("--yes")
        self.assertIn("already imported 3", again)
        self.assertEqual(BOMChangeRequest.objects.count(), 3)

    def test_a_row_that_moved_on_in_the_portal_is_reported_not_overwritten(self):
        self.run_import("--yes")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([dict(OPEN_UPDATE, STATUS="SAP_PUSHED")], f)
        output = self.run_import("--yes")
        self.assertIn("12: L1_APPROVED here but SAP_PUSHED in the portal file - not touched", output)
        self.assertEqual(BOMChangeRequest.objects.get(legacy_portal_id=12).status, "L1_APPROVED")

    def test_default_company_brings_in_the_rows_without_one(self):
        self.run_import("--yes", "--default-company", "JIVO_OIL")
        self.assertEqual(BOMChangeRequest.objects.get(legacy_portal_id=14).company.code, "JIVO_OIL")

    def test_an_unknown_default_company_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_import("--dry-run", "--default-company", "NOPE")

    def test_a_company_missing_from_this_database_is_skipped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([dict(REJECTED, ID=20, COMPANY="JIVO_BEVERAGES_HANADB")], f)
        output = self.run_import("--yes")
        self.assertIn("skipped: company not in this database 1", output)
        self.assertFalse(BOMChangeRequest.objects.exists())
