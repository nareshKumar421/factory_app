"""
Export licences: the register, its arithmetic, who may touch what, and the copy
from EXIM.

The promises worth pinning down:
  - the obligation is the first leg less 3.1% and the balance is that less the
    second leg, on both kinds, recalculated on EVERY line write including a
    delete (EXIM's line models missed three of those);
  - each call needs EXIM's own right for that kind and that part of a licence;
  - a licence is only ever seen from its own company;
  - the copy keeps EXIM's figures as they are, re-runs in place, and never
    overwrites a licence somebody has changed here.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import CommandError, call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from . import services_licence as services
from .licence_import import import_licences, read_exim
from .models_licence import Licence, LicenceKind, LicenceLine, LicenceStatus, LineDirection
from .permissions import licence_right

BASE = "/api/v1/exim/"

ADVANCE_ALL = [
    f"{action}_{model}"
    for action in ("view", "add", "change", "delete")
    for model in ("advancelicenseheaders", "advancelicenseimportlines", "advancelicenseexportlines")
]
DFIA_ALL = [
    f"{action}_{model}"
    for action in ("view", "add", "change", "delete")
    for model in ("dfialicenseheader", "dfialicenseimportlines", "dfialicenseexportlines")
]


def D(value):
    return Decimal(value)


class LicenceTestCase(APITestCase):
    permissions = ADVANCE_ALL + DFIA_ALL

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.role = UserRole.objects.create(name="Import / Export")
        self.user = self.make_user("exim@example.com", "EX001", self.permissions)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}

    def make_user(self, email, code, permissions=(), company=None):
        user = get_user_model().objects.create_user(
            email=email, password="testpass", full_name=email, employee_code=code
        )
        UserCompany.objects.create(
            user=user, company=company or self.company, role=self.role, is_default=True
        )
        user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="exim", codename__in=list(permissions))
        )
        return user

    def as_user(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def licence_payload(self, **overrides):
        payload = {
            "kind": "ADVANCE",
            "number": "511035345",
            "status": "OPEN",
            "issue_date": "2025-09-24",
            "import_validity": "2026-09-24",
            "export_validity": "2027-03-24",
            "cif_value_inr": "602734.430",
            "cif_exchange_rate": "89.000",
            "fob_value_inr": "66620812.500",
            "fob_exchange_rate": "87.300",
            "authorised_qty_mts": "525.030",
        }
        payload.update(overrides)
        return payload

    def make_licence(self, kind=LicenceKind.ADVANCE, number="L-1", company=None):
        return services.create_licence(
            company=company or self.company,
            user=self.user,
            kind=kind,
            number=number,
            status=LicenceStatus.OPEN,
            issue_date=date(2025, 9, 24),
            import_validity=date(2026, 9, 24),
            export_validity=date(2027, 3, 24),
            cif_value_inr=D("1000"),
            cif_exchange_rate=D("80"),
            fob_value_inr=D("2000"),
            fob_exchange_rate=D("80"),
            authorised_qty_mts=D("500"),
        )

    def post(self, path, payload, client=None):
        return (client or self.client).post(f"{BASE}{path}", payload, format="json", **self.headers)

    def patch(self, path, payload, client=None):
        return (client or self.client).patch(f"{BASE}{path}", payload, format="json", **self.headers)

    def get(self, path, client=None, **params):
        return (client or self.client).get(f"{BASE}{path}", params, **self.headers)

    def delete(self, path, client=None):
        return (client or self.client).delete(f"{BASE}{path}", **self.headers)

    def add_line(self, licence, direction, qty, doc="D-1", client=None, **extra):
        payload = {
            "direction": direction,
            "document_no": doc,
            "document_date": "2025-10-24",
            "value_usd": "1000.000",
            "quantity_mts": qty,
        }
        payload.update(extra)
        return self.post(f"licences/{licence.id}/lines/", payload, client=client)


class LicenceRegisterTests(LicenceTestCase):
    def test_create_works_out_the_usd_values(self):
        response = self.post("licences/", self.licence_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["cif_value_usd"], "6772.297")  # 602734.43 / 89
        self.assertEqual(response.data["fob_value_usd"], "763125.000")  # 66620812.5 / 87.3
        self.assertEqual(response.data["obligation_mts"], "0.000")
        self.assertEqual(response.data["balance_mts"], "0.000")
        self.assertEqual(response.data["first_leg"], "IMPORT")
        self.assertEqual(response.data["lines"], [])
        self.assertEqual(response.data["created_by_name"], self.user.full_name)

    def test_a_number_is_on_the_register_once_per_kind(self):
        self.post("licences/", self.licence_payload())
        again = self.post("licences/", self.licence_payload())
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(again.data["code"], "licence_exists")

        dfia = self.post("licences/", self.licence_payload(kind="DFIA"))
        self.assertEqual(dfia.status_code, status.HTTP_201_CREATED)

    def test_an_exchange_rate_of_zero_is_refused(self):
        response = self.post("licences/", self.licence_payload(cif_exchange_rate="0"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cif_exchange_rate", response.data)

    def test_the_list_needs_a_kind_and_holds_only_that_kind(self):
        self.make_licence(LicenceKind.ADVANCE, "A-1")
        self.make_licence(LicenceKind.DFIA, "F-1")

        self.assertEqual(self.get("licences/").status_code, status.HTTP_400_BAD_REQUEST)
        advance = self.get("licences/", kind="ADVANCE").data
        self.assertEqual([row["number"] for row in advance], ["A-1"])
        dfia = self.get("licences/", kind="DFIA").data
        self.assertEqual([row["number"] for row in dfia], ["F-1"])

    def test_the_list_filters_on_status(self):
        self.make_licence(number="A-1")
        closed = self.make_licence(number="A-2")
        services.update_licence(closed, user=self.user, status=LicenceStatus.CLOSED)
        rows = self.get("licences/", kind="ADVANCE", status="CLOSED").data
        self.assertEqual([row["number"] for row in rows], ["A-2"])

    def test_another_companys_licence_does_not_exist_from_here(self):
        theirs = self.make_licence(number="M-1", company=self.other_company)
        self.assertEqual(self.get("licences/", kind="ADVANCE").data, [])
        self.assertEqual(self.get(f"licences/{theirs.id}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.add_line(theirs, "IMPORT", "10").status_code, status.HTTP_404_NOT_FOUND
        )

    def test_an_edit_keeps_the_number_and_reworks_the_usd_value(self):
        licence = self.make_licence(number="A-1")
        response = self.patch(
            f"licences/{licence.id}/",
            {"number": "CHANGED", "kind": "DFIA", "cif_value_inr": "1600", "status": "CLOSED"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["number"], "A-1")
        self.assertEqual(response.data["kind"], "ADVANCE")
        self.assertEqual(response.data["cif_value_usd"], "20.000")
        self.assertEqual(response.data["status"], "CLOSED")

    def test_deleting_a_licence_takes_its_lines(self):
        licence = self.make_licence()
        self.add_line(licence, "IMPORT", "10")
        self.assertEqual(self.delete(f"licences/{licence.id}/").status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Licence.objects.exists())
        self.assertFalse(LicenceLine.objects.exists())


class LicenceArithmeticTests(LicenceTestCase):
    def figures(self, licence):
        licence.refresh_from_db()
        return (
            licence.total_import_mts,
            licence.total_export_mts,
            licence.obligation_mts,
            licence.balance_mts,
        )

    def test_advance_imports_create_the_export_obligation(self):
        licence = self.make_licence()
        response = self.add_line(licence, "IMPORT", "499.640")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        # 499.640 less 3.1% = 484.15116 -> 484.151; and EXIM left the balance
        # untouched here, where it should be the whole obligation.
        self.assertEqual(self.figures(licence), (D("499.640"), D("0"), D("484.151"), D("484.151")))

        self.add_line(licence, "EXPORT", "112.097", doc="SB-1", document_date=None)
        self.assertEqual(self.figures(licence), (D("499.640"), D("112.097"), D("484.151"), D("372.054")))

    def test_dfia_exports_earn_the_import_entitlement(self):
        licence = self.make_licence(LicenceKind.DFIA)
        self.add_line(licence, "EXPORT", "100", document_date=None)
        self.add_line(licence, "IMPORT", "40", doc="BE-1")
        self.assertEqual(self.figures(licence), (D("40"), D("100"), D("96.900"), D("56.900")))

    def test_an_edit_uses_the_new_quantity(self):
        """EXIM summed the database before saving, so an edit counted the old figure."""
        licence = self.make_licence()
        self.add_line(licence, "IMPORT", "100")
        export = self.add_line(licence, "EXPORT", "50", doc="SB-1").data["lines"][1]
        response = self.patch(f"licence-lines/{export['id']}/", {"quantity_mts": "60"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["total_export_mts"], "60.000")
        self.assertEqual(response.data["balance_mts"], "36.900")

    def test_a_delete_takes_the_line_out_of_the_totals(self):
        """EXIM changed nothing on a delete."""
        licence = self.make_licence()
        imported = self.add_line(licence, "IMPORT", "100").data["lines"][0]
        response = self.delete(f"licence-lines/{imported['id']}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.figures(licence), (D("0"), D("0"), D("0"), D("0")))

    def test_exporting_more_than_owed_goes_negative(self):
        """EXIM allowed it and one of its licences stands at -14.84 today."""
        licence = self.make_licence()
        self.add_line(licence, "IMPORT", "100")
        self.add_line(licence, "EXPORT", "110", doc="SB-1")
        self.assertEqual(self.figures(licence)[3], D("-13.100"))

    def test_a_bill_of_entry_needs_its_date_and_a_shipping_bill_does_not(self):
        licence = self.make_licence()
        missing = self.add_line(licence, "IMPORT", "10", document_date=None)
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(missing.data["code"], "date_required")
        self.assertEqual(
            self.add_line(licence, "EXPORT", "10", document_date=None).status_code,
            status.HTTP_201_CREATED,
        )

    def test_a_quantity_must_be_more_than_nothing(self):
        licence = self.make_licence()
        self.assertEqual(self.add_line(licence, "IMPORT", "0").status_code, status.HTTP_400_BAD_REQUEST)


class LicenceLinkTests(LicenceTestCase):
    def test_an_advance_export_names_the_bill_of_entry_it_discharges(self):
        licence = self.make_licence()
        boe = self.add_line(licence, "IMPORT", "100", doc="BE-7").data["lines"][0]
        response = self.add_line(licence, "EXPORT", "50", doc="SB-1", linked_line=boe["id"])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        export = [l for l in response.data["lines"] if l["direction"] == "EXPORT"][0]
        self.assertEqual(export["linked_line"], boe["id"])
        self.assertEqual(export["linked_document_no"], "BE-7")

    def test_a_first_leg_line_is_not_linked(self):
        licence = self.make_licence()
        boe = self.add_line(licence, "IMPORT", "100").data["lines"][0]
        response = self.add_line(licence, "IMPORT", "10", doc="BE-2", linked_line=boe["id"])
        self.assertEqual(response.data["code"], "link_not_allowed")

    def test_a_link_stays_on_its_own_licence(self):
        licence = self.make_licence(number="A-1")
        other = self.make_licence(number="A-2")
        theirs = self.add_line(other, "IMPORT", "100").data["lines"][0]
        response = self.add_line(licence, "EXPORT", "10", linked_line=theirs["id"])
        self.assertEqual(response.data["code"], "link_invalid")

    def test_removing_the_bill_of_entry_keeps_the_export(self):
        """EXIM cascaded the delete and lost the shipping bill with it."""
        licence = self.make_licence()
        boe = self.add_line(licence, "IMPORT", "100").data["lines"][0]
        self.add_line(licence, "EXPORT", "50", doc="SB-1", linked_line=boe["id"])
        self.delete(f"licence-lines/{boe['id']}/")
        export = LicenceLine.objects.get()
        self.assertEqual(export.document_no, "SB-1")
        self.assertIsNone(export.linked_line)

    def test_an_edit_can_clear_the_link(self):
        licence = self.make_licence()
        boe = self.add_line(licence, "IMPORT", "100").data["lines"][0]
        export = self.add_line(licence, "EXPORT", "50", doc="SB-1", linked_line=boe["id"]).data["lines"][1]
        response = self.patch(f"licence-lines/{export['id']}/", {"linked_line": None})
        self.assertIsNone(response.data["lines"][1]["linked_line"])


class LicenceRightsTests(LicenceTestCase):
    def test_each_part_of_a_licence_needs_exims_own_right(self):
        self.assertEqual(licence_right("view", "ADVANCE"), "exim.view_advancelicenseheaders")
        self.assertEqual(
            licence_right("add", "ADVANCE", "EXPORT"), "exim.add_advancelicenseexportlines"
        )
        self.assertEqual(licence_right("delete", "DFIA", "IMPORT"), "exim.delete_dfialicenseimportlines")
        self.assertEqual(licence_right("change", "DFIA"), "exim.change_dfialicenseheader")
        codenames = set(
            Permission.objects.filter(content_type__app_label="exim").values_list("codename", flat=True)
        )
        for codename in ADVANCE_ALL + DFIA_ALL:
            self.assertIn(codename, codenames)

    def test_advance_rights_do_not_open_dfia(self):
        user = self.make_user("adv@example.com", "EX002", ADVANCE_ALL)
        client = self.as_user(user)
        self.assertEqual(self.get("licences/", client=client, kind="ADVANCE").status_code, 200)
        self.assertEqual(self.get("licences/", client=client, kind="DFIA").status_code, 403)
        dfia = self.make_licence(LicenceKind.DFIA)
        self.assertEqual(self.get(f"licences/{dfia.id}/", client=client).status_code, 403)
        self.assertEqual(
            self.post("licences/", self.licence_payload(kind="DFIA"), client=client).status_code, 403
        )

    def test_viewing_a_licence_shows_its_lines_but_does_not_let_you_add_them(self):
        user = self.make_user("view@example.com", "EX003", ["view_advancelicenseheaders"])
        client = self.as_user(user)
        licence = self.make_licence()
        self.add_line(licence, "IMPORT", "100")
        detail = self.get(f"licences/{licence.id}/", client=client)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.data["lines"]), 1)
        self.assertEqual(self.add_line(licence, "IMPORT", "5", client=client).status_code, 403)
        self.assertEqual(self.patch(f"licences/{licence.id}/", {"status": "CLOSED"}, client=client).status_code, 403)
        self.assertEqual(self.delete(f"licences/{licence.id}/", client=client).status_code, 403)

    def test_line_rights_go_by_direction(self):
        user = self.make_user(
            "sb@example.com",
            "EX004",
            ["view_advancelicenseheaders", "add_advancelicenseexportlines"],
        )
        client = self.as_user(user)
        licence = self.make_licence()
        self.assertEqual(self.add_line(licence, "IMPORT", "5", client=client).status_code, 403)
        created = self.add_line(licence, "EXPORT", "5", client=client)
        self.assertEqual(created.status_code, 201)
        line_id = created.data["lines"][0]["id"]
        self.assertEqual(self.patch(f"licence-lines/{line_id}/", {"quantity_mts": "6"}, client=client).status_code, 403)
        self.assertEqual(self.delete(f"licence-lines/{line_id}/", client=client).status_code, 403)

    def test_a_login_outside_the_company_is_refused(self):
        outsider = self.make_user("mart@example.com", "EX005", ADVANCE_ALL, company=self.other_company)
        response = self.get("licences/", client=self.as_user(outsider), kind="ADVANCE")
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# The copy from EXIM
# ---------------------------------------------------------------------------

EXIM_SCHEMA = """
CREATE TABLE advance_license_headers (
    license_no TEXT PRIMARY KEY, issue_date DATE, import_validity DATE, export_validity DATE,
    cif_value_inr NUMERIC, cif_value_usd NUMERIC, cif_exchange_rate NUMERIC,
    fob_value_inr NUMERIC, fob_value_usd NUMERIC, fob_exhange_rate NUMERIC,
    status TEXT, balance NUMERIC, to_be_exported NUMERIC, total_export NUMERIC,
    total_import NUMERIC, total_import_quantity NUMERIC);
CREATE TABLE advance_license_import_lines (
    id INTEGER PRIMARY KEY, "boe_No" TEXT, boe_value_usd NUMERIC, boe_date DATE,
    import_in_mts NUMERIC, license_no_id TEXT);
CREATE TABLE advance_license_export_lines (
    id INTEGER PRIMARY KEY, shipping_bill_no TEXT, sb_value_usd NUMERIC, export_in_mts NUMERIC,
    license_no_id TEXT, sb_date DATE, linked_import_line_id INT);
CREATE TABLE dfia_license_header (
    file_no TEXT PRIMARY KEY, issue_date DATE, export_validity DATE, fob_value_inr NUMERIC,
    fob_value_usd NUMERIC, fob_exchange_rate NUMERIC, import_validity DATE, cif_value_inr NUMERIC,
    cif_value_usd NUMERIC, cif_exchange_rate NUMERIC, status TEXT, total_export_quantity NUMERIC,
    total_import NUMERIC, total_export NUMERIC, to_be_imported NUMERIC, balance NUMERIC);
CREATE TABLE dfia_license_export_lines (
    id INTEGER PRIMARY KEY, shipping_bill_no TEXT, sb_value_usd NUMERIC, sb_date DATE,
    export_in_mts NUMERIC, license_no_id TEXT);
CREATE TABLE dfia_license_import_lines (
    id INTEGER PRIMARY KEY, boe_no TEXT, boe_value_usd NUMERIC, boe_date DATE,
    import_in_mts NUMERIC, license_no_id TEXT, linked_export_line_id INT);
"""

# Two of EXIM's own licences as they stand, and one invented DFIA: a closed
# licence whose stored obligation (500.004) is not EXIM's rule (484.500), an
# open one EXIM never worked a balance out for, and a line EXIM lost the
# licence of.
EXIM_ROWS = """
INSERT INTO advance_license_headers VALUES
  ('511007116', '2021-12-22', '2022-12-22', '2023-06-22', 54500000, 718050.066, 75.9,
   65500000, 882749.326, 74.2, 'CLOSE', 99.830, 500.004, 400.174, 500.000, 516.000),
  ('511035345', '2025-09-24', '2026-09-24', '2027-03-24', 602734.43, 6772.297, 89,
   66620812.5, 763125, 87.3, 'OPEN', NULL, 484.151, 0, 499.640, 525.030);
INSERT INTO advance_license_import_lines VALUES
  (1, '6907664', 1392500, '2021-12-31', 500.000, '511007116'),
  (4, '5276495', 573586.72, '2025-10-24', 499.640, '511035345'),
  (9, '0000000', 1, '2025-01-01', 1, NULL);
INSERT INTO advance_license_export_lines VALUES
  (1, '7440953', 182385, 104.220, '511007116', '2022-01-12', 1),
  (2, '8727803', 182122.5, 107.399, '511007116', '2022-03-05', NULL),
  (3, '8500238', 142538, 66.860, '511007116', '2023-03-15', NULL),
  (4, '6829939', 187093.4, 121.695, '511007116', NULL, NULL);
INSERT INTO dfia_license_header VALUES
  ('F-1', '2025-01-01', '2026-01-01', 100, 1.25, 80, '2026-06-01', 50, 0.625, 80,
   'OPEN', 200, 0, 100, 96.900, NULL);
INSERT INTO dfia_license_export_lines VALUES (7, 'SB-9', 100, '2025-02-01', 100, 'F-1');
"""


def exim_db(extra_sql=""):
    db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    db.executescript(EXIM_SCHEMA + EXIM_ROWS + extra_sql)
    return db


def read(db):
    return read_exim(db.cursor())


class ReadEximLicencesTests(TestCase):
    def test_reads_both_kinds_with_their_lines(self):
        db = exim_db()
        self.addCleanup(db.close)
        snapshot = read(db)
        by_ref = {l.ref: l for l in snapshot.licences}
        self.assertEqual(sorted(by_ref), ["ADVANCE:511007116", "ADVANCE:511035345", "DFIA:F-1"])
        closed = by_ref["ADVANCE:511007116"]
        self.assertEqual(closed.status, "CLOSE")
        self.assertEqual(len(closed.lines), 5)
        linked = [l for l in closed.lines if l.linked_ref]
        self.assertEqual(linked[0].linked_ref, "advance_license_import_lines:1")
        self.assertEqual(snapshot.orphans, {"advance_license_import_lines": 1})


class ImportLicencesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.db = exim_db()
        self.addCleanup(self.db.close)

    def run_import(self, db=None):
        return import_licences(read(db or self.db), company=self.company)

    def test_copies_exims_figures_as_they_are(self):
        report = self.run_import()
        self.assertEqual(report.count("create"), 3)

        closed = Licence.objects.get(exim_ref="ADVANCE:511007116")
        self.assertEqual(closed.company, self.company)
        self.assertEqual(closed.status, LicenceStatus.CLOSED)
        self.assertEqual(closed.obligation_mts, D("500.004"))  # not the rule's 484.500
        self.assertEqual(closed.balance_mts, D("99.830"))
        self.assertEqual(closed.authorised_qty_mts, D("516.000"))
        self.assertEqual(closed.lines.count(), 5)
        self.assertIn(("ADVANCE:511007116", D("500.004"), D("484.500")), report.discrepancies)

    def test_fills_a_balance_exim_never_worked_out(self):
        self.run_import()
        open_licence = Licence.objects.get(exim_ref="ADVANCE:511035345")
        self.assertEqual(open_licence.balance_mts, D("484.151"))
        dfia = Licence.objects.get(exim_ref="DFIA:F-1")
        self.assertEqual(dfia.balance_mts, D("96.900"))

    def test_maps_the_links_between_lines(self):
        self.run_import()
        export = LicenceLine.objects.get(exim_ref="advance_license_export_lines:1")
        self.assertEqual(export.linked_line.exim_ref, "advance_license_import_lines:1")
        self.assertEqual(export.direction, LineDirection.EXPORT)
        self.assertEqual(export.document_date, date(2022, 1, 12))
        undated = LicenceLine.objects.get(exim_ref="advance_license_export_lines:4")
        self.assertIsNone(undated.document_date)

    def test_a_rerun_changes_nothing(self):
        self.run_import()
        again = self.run_import()
        self.assertEqual(again.count("unchanged"), 3)
        self.assertEqual(LicenceLine.objects.count(), 7)
        # Nothing is claimed of a run that wrote nothing, the filled balances included.
        self.assertEqual([r.notes for r in again.results], [[], [], []])

    def test_a_rerun_follows_exim(self):
        self.run_import()
        self.db.executescript(
            """
            UPDATE advance_license_headers SET status = 'CLOSE' WHERE license_no = '511035345';
            DELETE FROM advance_license_export_lines WHERE id = 4;
            INSERT INTO dfia_license_import_lines VALUES (3, 'BE-3', 10, '2025-03-01', 10, 'F-1', 7);
            """
        )
        report = self.run_import()
        self.assertEqual(report.count("update"), 3)
        self.assertEqual(Licence.objects.get(exim_ref="ADVANCE:511035345").status, LicenceStatus.CLOSED)
        self.assertFalse(LicenceLine.objects.filter(exim_ref="advance_license_export_lines:4").exists())
        dfia_import = LicenceLine.objects.get(exim_ref="dfia_license_import_lines:3")
        self.assertEqual(dfia_import.linked_line.exim_ref, "dfia_license_export_lines:7")

    def test_a_licence_changed_here_is_left_alone(self):
        self.run_import()
        licence = Licence.objects.get(exim_ref="ADVANCE:511035345")
        user = get_user_model().objects.create_user(
            email="x@example.com", password="x", full_name="X", employee_code="X1"
        )
        services.add_line(
            licence,
            user=user,
            direction=LineDirection.EXPORT,
            document_no="SB-NEW",
            value_usd=D("1"),
            quantity_mts=D("10"),
        )
        self.db.execute("UPDATE advance_license_headers SET status = 'CLOSE' WHERE license_no = '511035345'")

        report = self.run_import()
        result = [r for r in report.results if r.ref == "ADVANCE:511035345"][0]
        self.assertEqual(result.action, "skip")
        licence.refresh_from_db()
        self.assertEqual(licence.status, LicenceStatus.OPEN)
        self.assertTrue(licence.lines.filter(document_no="SB-NEW").exists())

    def test_a_licence_raised_here_with_an_exim_number_is_a_conflict(self):
        user = get_user_model().objects.create_user(
            email="x@example.com", password="x", full_name="X", employee_code="X1"
        )
        services.create_licence(
            company=self.company, user=user, kind=LicenceKind.DFIA, number="F-1",
            status=LicenceStatus.OPEN, issue_date=date(2025, 1, 1),
            import_validity=date(2026, 1, 1), export_validity=date(2026, 1, 1),
            cif_value_inr=D("1"), cif_exchange_rate=D("1"),
            fob_value_inr=D("1"), fob_exchange_rate=D("1"), authorised_qty_mts=D("1"),
        )
        report = self.run_import()
        result = [r for r in report.results if r.ref == "DFIA:F-1"][0]
        self.assertEqual(result.action, "conflict")
        self.assertIsNone(Licence.objects.get(number="F-1").exim_ref)


class ImportLicencesCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def call(self, *args):
        db = exim_db()
        self.addCleanup(db.close)
        out = StringIO()
        with mock.patch(
            "exim.management.commands.import_exim_licences.read_exim", return_value=read(db)
        ):
            call_command("import_exim_licences", "--database", "default", *args, stdout=out)
        return out.getvalue()

    def test_without_commit_nothing_is_written(self):
        out = self.call()
        self.assertIn("DRY RUN - nothing was written", out)
        self.assertIn("3 created", out)
        self.assertFalse(Licence.objects.exists())

    def test_commit_writes_and_names_the_drift(self):
        out = self.call("--commit")
        self.assertNotIn("DRY RUN", out)
        self.assertEqual(Licence.objects.count(), 3)
        self.assertIn("ADVANCE:511007116", out)
        self.assertIn("stored 500.004", out)
        self.assertIn("belong to no licence", out)

    def test_an_unknown_company_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "no company 'JIVO_NOPE'"):
            self.call("--company", "JIVO_NOPE")

    def test_no_exim_database_is_an_error_that_says_what_to_set(self):
        with self.assertRaisesMessage(CommandError, "EXIM_DB_NAME"):
            call_command("import_exim_licences", "--database", "nowhere", stdout=StringIO())
