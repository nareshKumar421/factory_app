"""Marker read-back: schema-qualified SQL, two-row refusals, never fail open.

A fake HANA connection records every statement; ``HanaConnection`` and
``CompanyContext`` are patched where ``sap_purchasing.readback`` looks them up.

Run: ``manage.py test sap_purchasing.tests_readback --settings=config.sqlite_test_settings``
"""

import re
from datetime import datetime
from unittest import mock

from django.test import SimpleTestCase
from hdbcli import dbapi

from sap_client.exceptions import SAPDataError, SAPUnavailable
from sap_purchasing import readback
from sap_purchasing.readback import PurchasingReadback

SCHEMA = "TEST_JIVO_OIL_HANADB"


class FakeCursor:
    def __init__(self, results=(), error=None):
        self.results = list(results)
        self.error = error
        self.executed = []

    def execute(self, sql, params=None):
        if self.error is not None:
            raise self.error
        self.executed.append((sql, params))

    def fetchall(self):
        return self.results.pop(0) if self.results else []

    def close(self):
        pass


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


class FakeHana:
    schema = SCHEMA

    def __init__(self, cursor, connect_error=None):
        self._cursor = cursor
        self._connect_error = connect_error

    def connect(self):
        if self._connect_error is not None:
            raise self._connect_error
        return FakeConn(self._cursor)


def _reader(results=(), error=None, connect_error=None):
    cursor = FakeCursor(results, error)
    with mock.patch("sap_purchasing.readback.CompanyContext"), \
            mock.patch("sap_purchasing.readback.HanaConnection",
                       return_value=FakeHana(cursor, connect_error)):
        reader = PurchasingReadback("JIVO_OIL")
    return reader, cursor


class ReadbackTests(SimpleTestCase):
    def assertQualified(self, cursor):
        """Every FROM names the schema -- ODRF and OITM included."""
        self.assertTrue(cursor.executed)
        for sql, _ in cursor.executed:
            froms = re.findall(r'FROM\s+("[^"]+"(?:\."[^"]+")?)', sql)
            self.assertTrue(froms, sql)
            for table in froms:
                self.assertTrue(table.startswith(f'"{SCHEMA}".'), f"unqualified: {table}")

    # -- find_by_marker ------------------------------------------------------

    def test_find_by_marker_one_row(self):
        reader, cursor = _reader([[(27585, 2026096948, 600.0, datetime(2026, 9, 24), None)]])
        found = reader.find_by_marker("20", "ORGV000465", "[JI-CB G5]")
        self.assertEqual(found, {"doc_entry": 27585, "doc_num": 2026096948, "doc_total": 600.0,
                                 "doc_date": "2026-09-24", "draft_key": None})
        sql, params = cursor.executed[0]
        self.assertIn(f'"{SCHEMA}"."OPDN"', sql)
        self.assertIn("\"CANCELED\" = 'N'", sql)
        self.assertEqual(params, ["ORGV000465", "%[JI-CB G5]%"])
        self.assertQualified(cursor)

    def test_find_by_marker_none(self):
        reader, _ = _reader([[]])
        self.assertIsNone(reader.find_by_marker("22", "V", "[JI-CB PO 42]"))

    def test_find_by_marker_two_rows_refuses(self):
        reader, _ = _reader([[(1, 220926153, 1, None, None), (2, 220926157, 1, None, None)]])
        with self.assertRaises(SAPDataError) as caught:
            reader.find_by_marker("22", "V", "[JI-CB PO 42]")
        self.assertIn("220926153", str(caught.exception))
        self.assertIn("220926157", str(caught.exception))

    def test_find_by_draft_key(self):
        reader, cursor = _reader([[(14221, 220926157, 500.0, None, 77)]])
        found = reader.find_by_draft_key("22", 77)
        self.assertEqual(found["doc_entry"], 14221)
        self.assertEqual(found["draft_key"], 77)
        self.assertIn(f'"{SCHEMA}"."OPOR"', cursor.executed[0][0])

    # -- drafts --------------------------------------------------------------

    def test_find_draft_by_marker_is_schema_qualified(self):
        reader, cursor = _reader([[(77, "O", "W", 600.0)]])
        found = reader.find_draft_by_marker("20", "ORGV000465", "[JI-CB G5]")
        self.assertEqual(found, {"draft_entry": 77, "wdd_status": "W", "doc_total": 600.0,
                                 "outcome": readback.DRAFT_IN_APPROVAL})
        sql, params = cursor.executed[0]
        self.assertIn(f'"{SCHEMA}"."ODRF"', sql)
        self.assertIn("\"DocStatus\" = 'O'", sql)
        self.assertEqual(params, ["20", "ORGV000465", "%[JI-CB G5]%"])
        self.assertQualified(cursor)

    def test_find_draft_by_marker_two_rows_refuses(self):
        reader, _ = _reader([[(77, "O", "W", 1.0), (78, "O", "W", 1.0)]])
        with self.assertRaises(SAPDataError):
            reader.find_draft_by_marker("18", "V", "[JI-CB API 9]")

    def test_find_draft_without_approval_reads_other(self):
        reader, _ = _reader([[(77, "O", None, 1.0)]])
        found = reader.find_draft_by_marker("18", "V", "[JI-CB API 9]")
        self.assertEqual(found["wdd_status"], "-")
        self.assertEqual(found["outcome"], readback.DRAFT_OTHER)

    # -- failures never read as "not there" ------------------------------------

    def test_query_error_is_unavailable(self):
        reader, _ = _reader(error=dbapi.OperationalError(-10709, "connection lost"))
        with self.assertRaises(SAPUnavailable):
            reader.find_by_marker("20", "V", "[JI-CB G5]")

    def test_connect_error_is_unavailable(self):
        reader, _ = _reader(connect_error=dbapi.OperationalError(-10709, "refused"))
        with self.assertRaises(SAPUnavailable):
            reader.find_draft_by_marker("20", "V", "[JI-CB G5]")

    def test_unknown_object_is_a_value_error_before_sql(self):
        reader, cursor = _reader()
        for call in (lambda: reader.find_by_marker("99", "V", "[M]"),
                     lambda: reader.find_draft_by_marker("99", "V", "[M]"),
                     lambda: reader.document_lines("99", 1),
                     lambda: reader.document_state("99", 1),
                     lambda: reader.num_at_card_taken("99", "V", "X")):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(cursor.executed, [])

    def test_blank_marker_is_refused(self):
        reader, cursor = _reader()
        with self.assertRaises(ValueError):
            reader.find_by_marker("20", "V", "  ")
        self.assertEqual(cursor.executed, [])

    # -- NumAtCard -------------------------------------------------------------

    def test_num_at_card_checks_header_and_drafts(self):
        reader, cursor = _reader([[], [(91,)]])
        self.assertTrue(reader.num_at_card_taken("20", "V", "500/24-09-26"))
        (h_sql, h_params), (d_sql, d_params) = cursor.executed
        self.assertIn(f'"{SCHEMA}"."OPDN"', h_sql)
        self.assertEqual(h_params, ["V", "500/24-09-26"])
        self.assertIn(f'"{SCHEMA}"."ODRF"', d_sql)
        self.assertEqual(d_params, ["20", "V", "500/24-09-26"])
        self.assertQualified(cursor)

    def test_num_at_card_free(self):
        reader, _ = _reader([[], []])
        self.assertFalse(reader.num_at_card_taken("18", "V", "SEP 26/4050/800"))

    def test_num_at_card_taken_by_posted_document(self):
        reader, _ = _reader([[(5,)], []])
        self.assertTrue(reader.num_at_card_taken("18", "V", "SEP 26/4050/800"))

    # -- one document ----------------------------------------------------------

    def test_document_state(self):
        reader, cursor = _reader([[(220926157, "N", "C")]])
        self.assertEqual(reader.document_state("22", 14221),
                         {"doc_num": 220926157, "canceled": "N", "doc_status": "C"})
        self.assertIn(f'"{SCHEMA}"."OPOR"', cursor.executed[0][0])
        self.assertEqual(cursor.executed[0][1], [14221])

    def test_document_state_missing(self):
        reader, _ = _reader([[]])
        self.assertIsNone(reader.document_state("22", 1))

    def test_document_lines(self):
        reader, cursor = _reader([[
            (0, "CG0000015", 1.0, 1.0, 350.0, 350.0, "JI V12345", None, None, "O"),
            (1, "CG0000005", 1.0, 0.0, 150.0, 150.0, None, None, None, "C"),
        ]])
        lines = reader.document_lines("22", 14221)
        self.assertEqual(lines[0]["free_text"], "JI V12345")
        self.assertEqual(lines[1]["free_text"], "")
        self.assertEqual(lines[1]["line_status"], "C")
        self.assertIn(f'"{SCHEMA}"."POR1"', cursor.executed[0][0])
        self.assertIn('ORDER BY "LineNum"', cursor.executed[0][0])

    # -- item master -----------------------------------------------------------

    def test_item_flags(self):
        reader, cursor = _reader([[("CG0000015", "Pipe", 105, "Y", "Y", "Y", "N")]])
        flags = reader.item_flags(["CG0000015", "CG0000015", "", "NOPE"])
        self.assertEqual(flags, {"CG0000015": {"item_name": "Pipe", "group": 105, "inventory": True,
                                               "purchase": True, "valid": True, "frozen": False}})
        sql, params = cursor.executed[0]
        self.assertIn(f'"{SCHEMA}"."OITM"', sql)
        self.assertEqual(params, ["CG0000015", "NOPE"])

    def test_item_flags_empty_runs_no_sql(self):
        reader, cursor = _reader()
        self.assertEqual(reader.item_flags([]), {})
        self.assertEqual(cursor.executed, [])


class LineMappingTests(SimpleTestCase):
    def test_po_line_free_text(self):
        self.assertEqual(readback.po_line_free_text(12345), "JI V12345")

    def test_map_by_free_text_skips_missing_and_ambiguous(self):
        lines = [
            {"line_num": 0, "free_text": "JI V1"},
            {"line_num": 1, "free_text": "JI V2"},
            {"line_num": 2, "free_text": "JI V3"},
            {"line_num": 3, "free_text": "JI V3"},
        ]
        mapping = readback.map_lines_by_free_text(
            lines, {e: readback.po_line_free_text(e) for e in (1, 2, 3, 4)})
        self.assertEqual(mapping, {1: 0, 2: 1})

    def test_map_by_base_line(self):
        lines = [
            {"line_num": 0, "base_entry": 14221, "base_line": 3},
            {"line_num": 1, "base_entry": 14221, "base_line": 4},
            {"line_num": 2, "base_entry": 99999, "base_line": 5},
        ]
        self.assertEqual(
            readback.map_lines_by_base_line(lines, {"v1": 3, "v2": 4, "v3": 5, "v4": None},
                                            base_entry=14221),
            {"v1": 0, "v2": 1},
        )
        self.assertEqual(readback.map_lines_by_base_line(lines, {"v3": 5}), {"v3": 2})

    def test_classify_draft(self):
        self.assertEqual(readback.classify_draft("W"), readback.DRAFT_IN_APPROVAL)
        self.assertEqual(readback.classify_draft("Y"), readback.DRAFT_APPROVED)
        self.assertEqual(readback.classify_draft("N"), readback.DRAFT_REJECTED)
        for value in ("-", "P", "A", "C", "", None):
            self.assertEqual(readback.classify_draft(value), readback.DRAFT_OTHER)
