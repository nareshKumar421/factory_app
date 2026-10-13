"""Series for (object, date, branch): the branch, hint and exhausted rules.

``pick_series`` is pure; ``candidates`` runs against a fake HANA connection
patched where ``HanaSeriesReader`` builds it
(``sap_client.hana.series_reader.HanaConnection``).

Run: ``manage.py test sap_purchasing.tests_series --settings=config.sqlite_test_settings``
"""

from datetime import date
from unittest import mock

from django.test import SimpleTestCase
from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_purchasing import series

DAY = date(2026, 9, 24)


def _row(series_no, name, bpl_id, next_number=1, last_number=999, begin="", locked="N"):
    return {"series": series_no, "series_name": name, "begin_str": begin, "bpl_id": bpl_id,
            "next_number": next_number, "last_number": last_number, "locked": locked,
            "indicator": "2026-09", "period_status": "N"}


class PickSeriesTests(SimpleTestCase):
    def test_single_row_is_the_answer(self):
        self.assertEqual(series.pick_series([_row(2466, "PO0926", 2)], "22", DAY, bpl_id=2)["series"], 2466)

    def test_branch_rows_win(self):
        rows = [_row(1, "BH0926", 2), _row(2, "DL0926", 3), _row(3, "ANY0926", None)]
        self.assertEqual(series.pick_series(rows, "18", DAY, bpl_id=3)["series"], 2)

    def test_falls_back_to_branchless_rows(self):
        rows = [_row(1, "BH0926", 2), _row(3, "ANY0926", None)]
        self.assertEqual(series.pick_series(rows, "18", DAY, bpl_id=7)["series"], 3)

    def test_no_branch_given_keeps_every_row(self):
        rows = [_row(1, "BH0926", 2), _row(3, "ANY0926", None)]
        with self.assertRaises(SAPDataError):
            series.pick_series(rows, "18", DAY)

    def test_hint_matches_name_or_begin_string(self):
        rows = [_row(1, "AP0926", 2), _row(2, "IMP0926", 2)]
        self.assertEqual(series.pick_series(rows, "18", DAY, bpl_id=2, name_hint="imp")["series"], 2)
        rows = [_row(1, "PURCH", 2, begin="626"), _row(2, "PURCH", 2, begin="726")]
        self.assertEqual(series.pick_series(rows, "18", DAY, bpl_id=2, name_hint="726")["series"], 2)

    def test_exhausted_rows_are_dropped(self):
        rows = [_row(1, "A", 2, next_number=1000, last_number=999), _row(2, "B", 2)]
        self.assertEqual(series.pick_series(rows, "20", DAY, bpl_id=2)["series"], 2)

    def test_locked_rows_are_dropped(self):
        rows = [_row(1, "A", 2, locked="Y"), _row(2, "B", 2)]
        self.assertEqual(series.pick_series(rows, "20", DAY, bpl_id=2)["series"], 2)

    def test_locked_branch_series_does_not_fall_back_to_branchless(self):
        # Branch 2's own series exists but is locked: the answer is "no open
        # series", not the branchless ANY0926.
        rows = [_row(1, "BH0926", 2, locked="Y"), _row(3, "ANY0926", None)]
        with self.assertRaises(SAPDataError) as caught:
            series.pick_series(rows, "18", DAY, bpl_id=2)
        message = str(caught.exception)
        self.assertIn("No open SAP series", message)
        self.assertIn("BH0926 (1) is locked", message)

    def test_rows_without_a_locked_key_count_as_unlocked(self):
        row = _row(9, "X", None)
        del row["locked"]
        self.assertEqual(series.pick_series([row], "22", DAY)["series"], 9)

    def test_none_left_names_object_date_and_branch(self):
        with self.assertRaises(SAPDataError) as caught:
            series.pick_series([], "20", DAY, bpl_id=2)
        message = str(caught.exception)
        self.assertIn("object 20", message)
        self.assertIn("24 Sep 2026", message)
        self.assertIn("branch 2", message)

    def test_several_left_asks_for_a_hint(self):
        rows = [_row(1, "A0926", 2), _row(2, "B0926", 2)]
        with self.assertRaises(SAPDataError) as caught:
            series.pick_series(rows, "22", DAY, bpl_id=2)
        self.assertIn("A0926", str(caught.exception))
        self.assertIn("series hint", str(caught.exception))

    def test_iso_string_dates_are_accepted(self):
        self.assertEqual(series.pick_series([_row(9, "X", None)], "22", "2026-09-01")["series"], 9)


class _Cursor:
    def __init__(self, rows, error=None):
        self.rows, self.error, self.executed = rows, error, []

    def execute(self, sql, params=None):
        if self.error:
            raise self.error
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _reader(rows=(), connect_error=None, error=None):
    cursor = _Cursor(list(rows), error)
    hana = mock.Mock()
    hana.schema = "TEST_JIVO_OIL_HANADB"
    if connect_error:
        hana.connect.side_effect = connect_error
    else:
        hana.connect.return_value = _Conn(cursor)
    ctx = mock.Mock()
    ctx.hana = {"schema": "TEST_JIVO_OIL_HANADB"}
    with mock.patch("sap_client.hana.series_reader.HanaConnection", return_value=hana):
        reader = series.BranchSeriesReader(ctx)
    return reader, cursor


class CandidatesTests(SimpleTestCase):
    def test_narrowest_period_only_and_sql_shape(self):
        reader, cursor = _reader([
            (2466, "PO0926", "", 2, 220926162, 220926999, "2026-09", "N", 29, "N"),
            (2470, "DLPO0926", "7", 3, 1, 9, "2026-09", "N", 29, "N"),
            (100, "PO2026", "", None, 1, 9, "2026", "N", 364, "N"),
        ])
        rows = reader.candidates("22", DAY)
        self.assertEqual([r["series"] for r in rows], [2466, 2470])
        self.assertEqual(rows[1]["begin_str"], "7")
        self.assertEqual(rows[1]["bpl_id"], 3)
        sql, params = cursor.executed[0]
        for fragment in ('"TEST_JIVO_OIL_HANADB"."OFPR"', '"TEST_JIVO_OIL_HANADB"."NNM1"',
                         'S."BPLId"', 'S."BeginStr"', "IFNULL(S.\"Locked\", 'N')"):
            self.assertIn(fragment, sql)
        # Locked is read, not filtered: the narrowest period is chosen first.
        self.assertNotIn("IFNULL(S.\"Locked\", 'N') = 'N'", sql)
        self.assertEqual(rows[0]["locked"], "N")
        self.assertEqual(params, ("22", DAY))

    def test_locked_rows_still_decide_the_narrowest_period(self):
        reader, _ = _reader([
            (2466, "PO0926", "", 2, 1, 999, "2026-09", "N", 29, "Y"),
            (100, "PO2026", "", 2, 1, 999, "2026", "N", 364, "N"),
        ])
        rows = reader.candidates("22", DAY)
        self.assertEqual([(r["series"], r["locked"]) for r in rows], [(2466, "Y")])

    def test_locked_month_series_does_not_fall_back_to_the_year_series(self):
        reader, _ = _reader([
            (2466, "PO0926", "", 2, 1, 999, "2026-09", "N", 29, "Y"),
            (100, "PO2026", "", 2, 1, 999, "2026", "N", 364, "N"),
        ])
        with self.assertRaises(SAPDataError) as caught:
            reader.resolve_for_branch("22", DAY, bpl_id=2)
        self.assertIn("No open SAP series", str(caught.exception))
        self.assertIn("PO0926 (2466) is locked", str(caught.exception))

    def test_empty(self):
        reader, _ = _reader([])
        self.assertEqual(reader.candidates("20", DAY), [])

    def test_connect_failure_is_a_connection_error(self):
        reader, _ = _reader(connect_error=dbapi.OperationalError(-10709, "down"))
        with self.assertRaises(SAPConnectionError):
            reader.candidates("20", DAY)

    def test_query_failure_is_a_data_error(self):
        reader, _ = _reader(error=dbapi.ProgrammingError(260, "invalid column"))
        with self.assertRaises(SAPDataError):
            reader.candidates("20", DAY)

    def test_resolve_for_branch_end_to_end(self):
        reader, _ = _reader([
            (2478, "GR0926", "", 2, 1, 999, "2026-09", "N", 29, "N"),
            (2480, "GRDL0926", "", 3, 1, 999, "2026-09", "N", 29, "N"),
        ])
        self.assertEqual(reader.resolve_for_branch("20", DAY, bpl_id=2)["series"], 2478)

    def test_module_function_uses_the_company_context(self):
        with mock.patch("sap_purchasing.series.CompanyContext") as ctx_cls, \
                mock.patch.object(series.BranchSeriesReader, "candidates",
                                  return_value=[_row(3325, "AP0926", 2)]):
            row = series.resolve_series_for_branch("JIVO_OIL", "18", DAY, bpl_id=2)
        ctx_cls.assert_called_once_with("JIVO_OIL")
        self.assertEqual(row["series"], 3325)
