"""
sap_reports/tests.py

Tests for the SAP Reports module.

The SQL fixtures below are the real saved queries from SAP's Factory category,
shortened but with their quirks intact: bare-CR line breaks, prompts reused in
several comparisons, an ``IN`` list of prompts, the ``OR '[%2]' = ''`` optional
idiom, and procedure calls. Those quirks are the whole difficulty of the module,
so they are what the tests are built from.

Nothing here touches SAP: the HANA reader is mocked everywhere.

Covered:
  1. sql          — normalising, read-only guard, prompt binding
  2. parameters   — type/label inference, coercion, bind values
  3. catalog      — sync: create, refresh, preserve edits, flag deletions
  4. runner       — row ceiling, audit trail, refusals
  5. exports      — csv / xlsx shaping
  6. API views    — auth, permissions, company scoping, error mapping
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPDataError

from sap_reports import exports
from sap_reports.exceptions import SapReportError, SapReportParameterError, SapReportSqlError
from sap_reports.models import SapReport, SapReportParameter, SapReportRun
from sap_reports.parameters import (
    ParameterKind,
    build_bind_values,
    coerce_value,
    infer_parameters,
    optional_positions,
)
from sap_reports.services.catalog import SapReportCatalogService
from sap_reports.services.runner import SapReportRunner
from sap_reports.sql import (
    assert_read_only,
    bind_prompts,
    detect_statement_kind,
    find_prompts,
    is_runnable,
    normalise_sql,
    prompt_positions,
    sql_hash,
)

# ---------------------------------------------------------------------------
# Fixtures — real SAP saved queries (line breaks as SAP stores them: bare CR)
# ---------------------------------------------------------------------------

SQL_NO_PROMPTS = 'select "ItemCode","BatchNum","ExpDate" from oibt'

SQL_DATE_RANGE = (
    'SELECT T0."DocNum", T0."DocDate"\r'
    'FROM OWTR T0\r'
    'WHERE T0."DocDate" BETWEEN \'[%0]\' AND \'[%1]\''
)

SQL_ITEM_AND_WAREHOUSE = (
    'SELECT T0."DocDate", T0."ItemCode"\r'
    'FROM OINM T0\r'
    'WHERE T0."ItemCode" = \'[%0]\'\r'
    '  AND T0."Warehouse" = \'[%1]\'\r'
    '  AND T0."DocDate" >= \'[%2]\'\r'
    '  AND T0."DocDate" <= \'[%3]\''
)

# [%0] is reused: once as an opening cut-off, once as the start of a range.
SQL_REUSED_PROMPT = (
    'SELECT\r'
    '  SUM(CASE WHEN T0."DocDate" < \'[%0]\' THEN T0."InQty" ELSE 0 END) AS "Opening",\r'
    '  SUM(CASE WHEN T0."DocDate" BETWEEN \'[%0]\' AND \'[%1]\' THEN T0."InQty" ELSE 0 END) AS "In"\r'
    'FROM OINM T0\r'
    'WHERE (T4."ItmsGrpNam" = \'[%2]\' OR \'[%2]\' = \'\')'
)

SQL_PROMPT_LIST = (
    'SELECT T0."ItemCode" FROM OINM T0\r'
    'WHERE T0."Warehouse" IN (\'[%0]\', \'[%1]\', \'[%2]\',\'[%3]\')'
)

SQL_ANNOTATED_PROMPT = (
    '/* SELECT FROM OWHS T0 */\r'
    '/* WHERE */\r'
    'SELECT O."DocNum" FROM OINV O\r'
    'INNER JOIN INV1 I ON O."DocEntry" = I."DocEntry"\r'
    'WHERE I."WhsCode" = /* T0."WhsCode" */ \'[%0]\''
)

SQL_PROCEDURE_CALL = (
    'CALL "REPORT_DOLLY" ((Select MIN(T1."RefDate") From OJDT T1 '
    'Where T1."RefDate" >=\'[%0]\'), (Select MAX(T1."RefDate") From OJDT T1 '
    'Where T1."RefDate" <=\'[%1]\'))'
)

SQL_PERIOD_CALL = (
    'Call "SALES PLANNING VS REQUIREMENT_OLD"'
    '((select T1."Name" from OFCT T1 where T1."Name"=\'[%0]\'));'
)


def saved_query(
    *,
    key=1,
    name="TEST REPORT",
    sql=SQL_DATE_RANGE,
    category="Factory",
    category_id=22,
    changed_at=None,
):
    """One row as ``HanaSapReportReader.list_saved_queries`` returns it."""
    return {
        "sap_internal_key": key,
        "sap_name": name,
        "sap_category_id": category_id,
        "sap_category_name": category,
        "sql_text": sql,
        "sap_changed_at": changed_at or timezone.now(),
    }


# ---------------------------------------------------------------------------
# 1. sql
# ---------------------------------------------------------------------------


class TestNormalising(TestCase):

    def test_bare_carriage_returns_become_newlines(self):
        """SAP stores line breaks as CR; unnormalised SQL is unreadable."""
        normalised = normalise_sql(SQL_DATE_RANGE)
        self.assertNotIn("\r", normalised)
        self.assertEqual(len(normalised.splitlines()), 3)

    def test_hash_ignores_line_ending_style(self):
        self.assertEqual(
            sql_hash("SELECT 1\rFROM DUMMY"),
            sql_hash("SELECT 1\nFROM DUMMY"),
        )

    def test_hash_changes_when_the_query_changes(self):
        self.assertNotEqual(sql_hash(SQL_DATE_RANGE), sql_hash(SQL_ITEM_AND_WAREHOUSE))

    def test_empty_sql_normalises_to_empty(self):
        self.assertEqual(normalise_sql(None), "")
        self.assertEqual(normalise_sql(""), "")


class TestStatementKind(TestCase):

    def test_select_is_recognised_whatever_the_casing(self):
        self.assertEqual(detect_statement_kind(SQL_NO_PROMPTS), "SELECT")
        self.assertEqual(detect_statement_kind(SQL_DATE_RANGE), "SELECT")

    def test_procedure_call_is_recognised(self):
        self.assertEqual(detect_statement_kind(SQL_PROCEDURE_CALL), "CALL")
        self.assertEqual(detect_statement_kind(SQL_PERIOD_CALL), "CALL")

    def test_leading_comment_does_not_hide_the_statement(self):
        self.assertEqual(detect_statement_kind(SQL_ANNOTATED_PROMPT), "SELECT")


class TestReadOnlyGuard(TestCase):

    def test_real_reports_pass(self):
        for sql in (
            SQL_NO_PROMPTS,
            SQL_DATE_RANGE,
            SQL_REUSED_PROMPT,
            SQL_ANNOTATED_PROMPT,
            SQL_PROCEDURE_CALL,
            SQL_PERIOD_CALL,
        ):
            with self.subTest(sql=sql[:40]):
                assert_read_only(sql)

    def test_write_statement_is_refused(self):
        with self.assertRaises(SapReportSqlError):
            assert_read_only('DELETE FROM OITM WHERE "ItemCode" = \'X\'')

    def test_write_hidden_after_a_select_is_refused(self):
        with self.assertRaises(SapReportSqlError):
            assert_read_only('SELECT 1 FROM DUMMY; DROP TABLE OITM')

    def test_second_statement_is_refused(self):
        with self.assertRaises(SapReportSqlError):
            assert_read_only('SELECT 1 FROM DUMMY; SELECT 2 FROM DUMMY')

    def test_trailing_semicolon_is_fine(self):
        assert_read_only('SELECT 1 FROM DUMMY;')

    def test_a_column_named_like_a_keyword_is_not_a_write(self):
        """A guard that trips on aliases would refuse real reports."""
        assert_read_only(
            'SELECT T0."UpdateDate" AS "Last Update", \'no delete here\' AS "Note" FROM OITM T0'
        )

    def test_empty_sql_is_refused(self):
        with self.assertRaises(SapReportSqlError):
            assert_read_only("")

    def test_is_runnable_reports_the_reason(self):
        runnable, reason = is_runnable("DROP TABLE OITM")
        self.assertFalse(runnable)
        self.assertIn("DROP", reason)
        self.assertEqual(is_runnable(SQL_DATE_RANGE), (True, ""))


class TestPromptBinding(TestCase):

    def test_positions_are_found(self):
        self.assertEqual(prompt_positions(SQL_ITEM_AND_WAREHOUSE), [0, 1, 2, 3])
        self.assertEqual(prompt_positions(SQL_NO_PROMPTS), [])

    def test_quotes_are_replaced_along_with_the_placeholder(self):
        """The bind must take the literal's place, quotes and all."""
        statement, params = bind_prompts(SQL_DATE_RANGE, {0: "20260801", 1: "20260822"})
        self.assertNotIn("[%", statement)
        self.assertNotIn("'?'", statement)
        self.assertIn('BETWEEN ? AND ?', statement)
        self.assertEqual(params, ["20260801", "20260822"])

    def test_a_reused_prompt_binds_once_per_occurrence(self):
        statement, params = bind_prompts(
            SQL_REUSED_PROMPT, {0: "20260801", 1: "20260822", 2: ""}
        )
        self.assertEqual(statement.count("?"), 5)
        # Two [%0], one [%1], two [%2] — in the order they appear in the text.
        self.assertEqual(params, ["20260801", "20260801", "20260822", "", ""])

    def test_binds_follow_text_order_not_prompt_number(self):
        statement, params = bind_prompts(
            'SELECT 1 FROM DUMMY WHERE A = \'[%1]\' AND B = \'[%0]\'',
            {0: "zero", 1: "one"},
        )
        self.assertEqual(params, ["one", "zero"])

    def test_missing_value_is_reported_with_its_prompt_number(self):
        with self.assertRaises(SapReportSqlError) as caught:
            bind_prompts(SQL_DATE_RANGE, {0: "20260801"})
        self.assertIn("[%1]", str(caught.exception))

    def test_an_unquoted_prompt_is_bound_too(self):
        statement, params = bind_prompts(
            'SELECT 1 FROM OINV WHERE "DocNum" = [%0]', {0: 626080206}
        )
        self.assertIn("= ?", statement)
        self.assertEqual(params, [626080206])

    def test_annotation_comment_survives_binding(self):
        statement, _ = bind_prompts(SQL_ANNOTATED_PROMPT, {0: "BH-FG"})
        self.assertIn('/* T0."WhsCode" */ ?', statement)

    def test_prompt_context_reaches_back_to_its_column(self):
        prompt = find_prompts(SQL_ANNOTATED_PROMPT)[0]
        self.assertEqual(prompt.hint_column, "WhsCode")


# ---------------------------------------------------------------------------
# 2. parameters
# ---------------------------------------------------------------------------


class TestParameterInference(TestCase):

    def test_a_between_range_becomes_from_and_to_dates(self):
        parameters = infer_parameters(SQL_DATE_RANGE)
        self.assertEqual([p.kind for p in parameters], [ParameterKind.DATE] * 2)
        self.assertEqual([p.label for p in parameters], ["From date", "To date"])

    def test_greater_and_less_than_also_become_from_and_to(self):
        parameters = infer_parameters(SQL_ITEM_AND_WAREHOUSE)
        self.assertEqual(
            [(p.kind, p.label) for p in parameters],
            [
                (ParameterKind.ITEM, "Item"),
                (ParameterKind.WAREHOUSE, "Warehouse"),
                (ParameterKind.DATE, "From date"),
                (ParameterKind.DATE, "To date"),
            ],
        )

    def test_a_range_use_outranks_a_bare_comparison(self):
        """
        [%0] is compared with ``<`` first, then used as the start of a BETWEEN.

        Reading only the first occurrence would caption the period's start "To
        date" — the exact opposite of what the user must type.
        """
        parameters = {p.position: p for p in infer_parameters(SQL_REUSED_PROMPT)}
        self.assertEqual(parameters[0].label, "From date")
        self.assertEqual(parameters[1].label, "To date")
        self.assertEqual(parameters[0].occurrences, 2)

    def test_the_or_blank_idiom_makes_a_prompt_optional(self):
        self.assertEqual(optional_positions(SQL_REUSED_PROMPT), {2})
        parameters = {p.position: p for p in infer_parameters(SQL_REUSED_PROMPT)}
        self.assertFalse(parameters[2].is_required)
        self.assertEqual(parameters[2].kind, ParameterKind.ITEM_GROUP)
        self.assertTrue(parameters[0].is_required)

    def test_repeated_labels_are_numbered(self):
        """Four warehouse boxes all captioned "Warehouse" would be unusable."""
        parameters = infer_parameters(SQL_PROMPT_LIST)
        self.assertEqual(
            [p.label for p in parameters],
            ["Warehouse 1", "Warehouse 2", "Warehouse 3", "Warehouse 4"],
        )
        self.assertTrue(all(p.kind == ParameterKind.WAREHOUSE for p in parameters))

    def test_sap_prompt_annotation_is_read(self):
        parameter = infer_parameters(SQL_ANNOTATED_PROMPT)[0]
        self.assertEqual(parameter.kind, ParameterKind.WAREHOUSE)
        self.assertEqual(parameter.label, "Warehouse")

    def test_procedure_call_dates_are_inferred(self):
        parameters = infer_parameters(SQL_PROCEDURE_CALL)
        self.assertEqual([p.label for p in parameters], ["From date", "To date"])

    def test_a_period_is_recognised_from_its_table(self):
        """OFCT."Name" is a fiscal period; the column name alone says nothing."""
        parameter = infer_parameters(SQL_PERIOD_CALL)[0]
        self.assertEqual(parameter.kind, ParameterKind.PERIOD)
        self.assertEqual(parameter.label, "Fiscal period")

    def test_a_query_without_prompts_has_no_parameters(self):
        self.assertEqual(infer_parameters(SQL_NO_PROMPTS), [])

    def test_an_unrecognised_column_falls_back_to_text(self):
        parameter = infer_parameters(
            'SELECT 1 FROM OINV T0 WHERE T0."U_Bilty_No" = \'[%0]\''
        )[0]
        self.assertEqual(parameter.kind, ParameterKind.TEXT)
        self.assertEqual(parameter.label, "Bilty no")


class TestValueCoercion(TestCase):

    def test_dates_are_sent_in_sap_format(self):
        for supplied in ("2026-08-22", "20260822", "22/08/2026", "22-08-2026"):
            with self.subTest(supplied=supplied):
                self.assertEqual(
                    coerce_value(ParameterKind.DATE, supplied, label="From date"),
                    "20260822",
                )

    def test_a_date_object_is_accepted(self):
        from datetime import date, datetime

        self.assertEqual(
            coerce_value(ParameterKind.DATE, date(2026, 8, 22), label="d"), "20260822"
        )
        self.assertEqual(
            coerce_value(ParameterKind.DATE, datetime(2026, 8, 22, 14, 30), label="d"),
            "20260822",
        )

    def test_a_bad_date_is_rejected_by_label(self):
        with self.assertRaises(SapReportParameterError) as caught:
            coerce_value(ParameterKind.DATE, "not a date", label="From date")
        self.assertIn("From date", str(caught.exception))

    def test_a_quoted_number_prompt_keeps_string_semantics(self):
        """
        SAP wrote the placeholder quoted, so the bind stays a string.

        HANA casts it against the column exactly as it did for SAP; sending a
        native int would change how the comparison behaves.
        """
        self.assertEqual(
            coerce_value(ParameterKind.NUMBER, "626080206", label="Doc", quoted=True),
            "626080206",
        )
        self.assertEqual(
            coerce_value(ParameterKind.NUMBER, "626080206", label="Doc", quoted=False),
            626080206,
        )

    def test_a_non_number_is_rejected(self):
        with self.assertRaises(SapReportParameterError):
            coerce_value(ParameterKind.NUMBER, "twelve", label="Doc")

    def test_text_is_trimmed(self):
        self.assertEqual(coerce_value(ParameterKind.ITEM, "  FG0000081 ", label="Item"), "FG0000081")

    def test_blank_required_value_is_rejected(self):
        with self.assertRaises(SapReportParameterError):
            coerce_value(ParameterKind.TEXT, "   ", label="Anything")


class TestBindValues(TestCase):
    """``build_bind_values`` over stored parameter rows."""

    def _parameter(self, **overrides):
        defaults = {
            "position": 0,
            "label": "From date",
            "kind": ParameterKind.DATE,
            "is_required": True,
            "default_value": "",
            "blank_value": "",
            "is_quoted": True,
        }
        defaults.update(overrides)
        return SapReportParameter(**defaults)

    def test_supplied_values_are_coerced(self):
        values = build_bind_values([self._parameter()], {"0": "2026-08-22"})
        self.assertEqual(values, {0: "20260822"})

    def test_an_integer_key_is_accepted_too(self):
        values = build_bind_values([self._parameter()], {0: "2026-08-22"})
        self.assertEqual(values, {0: "20260822"})

    def test_a_missing_required_value_is_rejected(self):
        with self.assertRaises(SapReportParameterError) as caught:
            build_bind_values([self._parameter()], {})
        self.assertIn("From date", str(caught.exception))

    def test_a_default_fills_in_for_a_missing_value(self):
        parameter = self._parameter(default_value="2026-01-01")
        self.assertEqual(build_bind_values([parameter], {}), {0: "20260101"})

    def test_an_optional_prompt_left_blank_binds_its_blank_value(self):
        """The query's own "or blank means no filter" escape."""
        parameter = self._parameter(
            label="Item group", kind=ParameterKind.ITEM_GROUP, is_required=False
        )
        self.assertEqual(build_bind_values([parameter], {}), {0: ""})

    def test_no_parameters_binds_nothing(self):
        self.assertEqual(build_bind_values([], {}), {})


# ---------------------------------------------------------------------------
# 3. catalog sync
# ---------------------------------------------------------------------------


class SapReportsSyncTestCase(TestCase):
    """Base class giving each sync test a company and a mocked HANA reader."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        patcher = patch("sap_reports.services.catalog.HanaSapReportReader")
        self.reader_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.reader = self.reader_class.return_value

        context_patcher = patch("sap_reports.services.catalog.CompanyContext")
        context_patcher.start()
        self.addCleanup(context_patcher.stop)

    def sync(self, saved_queries, **kwargs):
        self.reader.list_saved_queries.return_value = saved_queries
        return SapReportCatalogService(self.company).sync(**kwargs)


class TestCatalogSync(SapReportsSyncTestCase):

    def test_a_new_query_becomes_a_report_with_its_filters(self):
        summary = self.sync([saved_query(name="STOCK TRANSFER  REPORT", sql=SQL_DATE_RANGE)])

        self.assertEqual(summary["created"], ["STOCK TRANSFER  REPORT"])
        report = SapReport.objects.get(company=self.company)
        self.assertEqual(report.slug, "stock-transfer-report")
        self.assertEqual(report.statement_kind, "SELECT")
        self.assertTrue(report.is_runnable)
        self.assertEqual(
            [(p.position, p.label) for p in report.parameters.all()],
            [(0, "From date"), (1, "To date")],
        )

    def test_syncing_twice_changes_nothing(self):
        query = saved_query()
        self.sync([query])
        summary = self.sync([query])

        self.assertEqual(summary["created"], [])
        self.assertEqual(summary["updated"], [])
        self.assertEqual(summary["unchanged"], ["TEST REPORT"])
        self.assertEqual(SapReport.objects.count(), 1)

    def test_edited_sql_refreshes_the_report_and_its_filters(self):
        self.sync([saved_query(sql=SQL_DATE_RANGE)])
        summary = self.sync([saved_query(sql=SQL_ITEM_AND_WAREHOUSE)])

        self.assertEqual(summary["updated"], ["TEST REPORT"])
        report = SapReport.objects.get()
        self.assertEqual(report.parameters.count(), 4)
        self.assertEqual(report.parameters.first().label, "Item")

    def test_a_prompt_removed_in_sap_is_removed_here(self):
        self.sync([saved_query(sql=SQL_ITEM_AND_WAREHOUSE)])
        self.sync([saved_query(sql=SQL_DATE_RANGE)])

        self.assertEqual(
            list(SapReport.objects.get().parameters.values_list("position", flat=True)),
            [0, 1],
        )

    def test_a_renamed_query_keeps_its_slug_and_history(self):
        """The slug is a URL; SAP renames must not break saved links."""
        self.sync([saved_query(name="OLD NAME")])
        self.sync([saved_query(name="NEW NAME")])

        report = SapReport.objects.get()
        self.assertEqual(report.slug, "old-name")
        self.assertEqual(report.sap_name, "NEW NAME")

    def test_our_own_fields_survive_a_sync(self):
        self.sync([saved_query()])
        SapReport.objects.update(
            display_name="Friendly Name",
            description="What this answers.",
            is_enabled=False,
            sort_order=5,
            row_limit=100,
        )

        self.sync([saved_query(sql=SQL_ITEM_AND_WAREHOUSE)])

        report = SapReport.objects.get()
        self.assertEqual(report.display_name, "Friendly Name")
        self.assertEqual(report.description, "What this answers.")
        self.assertFalse(report.is_enabled)
        self.assertEqual(report.sort_order, 5)
        self.assertEqual(report.row_limit, 100)

    def test_a_corrected_parameter_survives_a_sync(self):
        """
        The point of ``is_customised``: a human's label beats the guess.

        Re-inference happens on every SQL change, so without the flag every
        correction would be silently undone the next time SAP was edited.
        """
        self.sync([saved_query(sql=SQL_ITEM_AND_WAREHOUSE)])
        report = SapReport.objects.get()
        report.parameters.filter(position=0).update(
            label="SKU", help_text="Finished-goods code", is_customised=True
        )

        self.sync([saved_query(sql=SQL_ITEM_AND_WAREHOUSE + '\rAND 1 = 1')])

        corrected = report.parameters.get(position=0)
        self.assertEqual(corrected.label, "SKU")
        self.assertEqual(corrected.help_text, "Finished-goods code")
        # An untouched one still tracks the SQL.
        self.assertEqual(report.parameters.get(position=1).label, "Warehouse")

    def test_a_query_deleted_in_sap_is_flagged_not_deleted(self):
        self.sync([saved_query(key=1, name="KEEP"), saved_query(key=2, name="GONE")])
        summary = self.sync([saved_query(key=1, name="KEEP")])

        self.assertEqual(summary["missing_in_sap"], ["GONE"])
        self.assertEqual(SapReport.objects.count(), 2)
        self.assertTrue(SapReport.objects.get(sap_name="GONE").is_missing_in_sap)
        self.assertNotIn(
            "GONE",
            SapReport.objects.runnable().values_list("sap_name", flat=True),
        )

    def test_a_report_that_comes_back_in_sap_is_unflagged(self):
        self.sync([saved_query()])
        SapReport.objects.update(is_missing_in_sap=True)

        self.sync([saved_query()])

        self.assertFalse(SapReport.objects.get().is_missing_in_sap)

    def test_syncing_one_category_leaves_another_alone(self):
        """A Factory sync must not flag the GST reports as deleted."""
        self.sync([saved_query(key=9, name="GST THING", category="GST R1")], category_name="GST R1")
        self.sync([saved_query(key=1, name="FACTORY THING")], category_name="Factory")

        self.assertFalse(SapReport.objects.get(sap_name="GST THING").is_missing_in_sap)

    def test_a_write_query_is_catalogued_but_not_runnable(self):
        summary = self.sync([saved_query(name="BAD", sql="DELETE FROM OITM")])

        report = SapReport.objects.get()
        self.assertFalse(report.is_runnable)
        self.assertIn("DELETE", report.not_runnable_reason)
        self.assertTrue(any("BAD" in note for note in summary["not_runnable"]))
        self.assertFalse(SapReport.objects.runnable().exists())

    def test_two_queries_with_the_same_name_get_different_slugs(self):
        self.sync([saved_query(key=1, name="SAME"), saved_query(key=2, name="SAME")])

        slugs = sorted(SapReport.objects.values_list("slug", flat=True))
        self.assertEqual(slugs, ["same", "same-2"])

    def test_a_dry_run_writes_nothing(self):
        summary = self.sync([saved_query()], dry_run=True)

        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["created"], ["TEST REPORT"])
        self.assertFalse(SapReport.objects.exists())

    def test_a_procedure_call_is_catalogued_as_a_call(self):
        self.sync([saved_query(name="DOLLY MAM REPORT", sql=SQL_PROCEDURE_CALL)])

        report = SapReport.objects.get()
        self.assertEqual(report.statement_kind, "CALL")
        self.assertTrue(report.is_runnable)


# ---------------------------------------------------------------------------
# 4. runner
# ---------------------------------------------------------------------------


class TestReportRunner(TestCase):

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        User = get_user_model()
        self.user = User.objects.create_user(
            email="runner@test.com",
            password="testpass123",
            full_name="Runner",
            employee_code="EMP100",
        )

        self.report = SapReport.objects.create(
            company=self.company,
            sap_internal_key=1,
            sap_name="STOCK TRANSFER  REPORT",
            sap_category_id=22,
            sap_category_name="Factory",
            sql_text=normalise_sql(SQL_DATE_RANGE),
            sql_hash=sql_hash(SQL_DATE_RANGE),
            slug="stock-transfer-report",
        )
        SapReportParameter.objects.create(
            report=self.report, position=0, label="From date", kind=ParameterKind.DATE
        )
        SapReportParameter.objects.create(
            report=self.report, position=1, label="To date", kind=ParameterKind.DATE
        )

        patcher = patch("sap_reports.services.runner.HanaSapReportReader")
        self.reader_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.reader = self.reader_class.return_value

        context_patcher = patch("sap_reports.services.runner.CompanyContext")
        context_patcher.start()
        self.addCleanup(context_patcher.stop)

        self.columns = [
            {"key": "DocNum", "label": "DocNum", "type": "number"},
            {"key": "DocDate", "label": "DocDate", "type": "date"},
        ]
        self.reader.run_statement.return_value = (
            self.columns,
            [[626080206, "2026-08-22"]],
            False,
        )

    def runner(self):
        return SapReportRunner(company=self.company, user=self.user)

    def test_a_run_returns_columns_rows_and_meta(self):
        result = self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

        self.assertEqual(result["columns"], self.columns)
        self.assertEqual(result["rows"], [[626080206, "2026-08-22"]])
        self.assertEqual(result["meta"]["row_count"], 1)
        self.assertFalse(result["meta"]["was_truncated"])
        self.assertEqual(result["meta"]["company"], "JIVO_OIL")

    def test_values_reach_hana_as_binds_never_as_sql(self):
        """The injection guarantee: user input only ever arrives as a parameter."""
        self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

        statement, params = self.reader.run_statement.call_args[0]
        self.assertNotIn("[%", statement)
        self.assertNotIn("20260801", statement)
        self.assertEqual(params, ["20260801", "20260822"])

    def test_a_quote_in_a_value_cannot_escape_into_the_statement(self):
        self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})
        statement, _ = self.reader.run_statement.call_args[0]
        self.assertEqual(statement.count("?"), 2)

    def test_the_filters_used_are_echoed_back(self):
        result = self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

        self.assertEqual(
            result["meta"]["parameters"],
            [
                {"position": 0, "label": "From date", "kind": "DATE", "value": "20260801"},
                {"position": 1, "label": "To date", "kind": "DATE", "value": "20260822"},
            ],
        )

    def test_truncation_is_reported(self):
        self.reader.run_statement.return_value = (self.columns, [[1, "x"]] * 5, True)

        result = self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

        self.assertTrue(result["meta"]["was_truncated"])

    def test_the_row_ceiling_is_capped_at_the_module_maximum(self):
        self.runner().run(
            self.report, {"0": "2026-08-01", "1": "2026-08-22"}, row_limit=10_000_000
        )

        self.assertEqual(
            self.reader.run_statement.call_args.kwargs["row_limit"],
            50000,
        )

    def test_a_report_row_limit_is_honoured(self):
        self.report.row_limit = 250
        self.report.save()

        self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

        self.assertEqual(self.reader.run_statement.call_args.kwargs["row_limit"], 250)

    def test_a_successful_run_is_audited(self):
        self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

        run = SapReportRun.objects.get()
        self.assertEqual(run.status, SapReportRun.Status.SUCCESS)
        self.assertEqual(run.row_count, 1)
        self.assertEqual(run.run_by, self.user)
        self.assertEqual(run.parameters, {"0": "20260801", "1": "20260822"})
        self.report.refresh_from_db()
        self.assertIsNotNone(self.report.last_run_at)

    def test_a_failed_run_is_audited_and_the_error_reraised(self):
        self.reader.run_statement.side_effect = SAPDataError("invalid column name")

        with self.assertRaises(SAPDataError):
            self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

        run = SapReportRun.objects.get()
        self.assertEqual(run.status, SapReportRun.Status.ERROR)
        self.assertIn("invalid column name", run.error_message)

    def test_a_connection_failure_is_audited_too(self):
        self.reader.run_statement.side_effect = SAPConnectionError("down")

        with self.assertRaises(SAPConnectionError):
            self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

        self.assertEqual(SapReportRun.objects.get().status, SapReportRun.Status.ERROR)

    def test_a_missing_filter_is_refused_before_sap_is_touched(self):
        with self.assertRaises(SapReportParameterError):
            self.runner().run(self.report, {"0": "2026-08-01"})

        self.reader.run_statement.assert_not_called()

    def test_a_disabled_report_will_not_run(self):
        self.report.is_enabled = False
        self.report.save()

        with self.assertRaises(SapReportError):
            self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})
        self.reader.run_statement.assert_not_called()

    def test_a_report_deleted_in_sap_will_not_run(self):
        self.report.is_missing_in_sap = True
        self.report.save()

        with self.assertRaises(SapReportError):
            self.runner().run(self.report, {"0": "2026-08-01", "1": "2026-08-22"})

    def test_stored_sql_is_re_checked_at_run_time(self):
        """
        The guard runs against what is about to execute, not what was synced.

        A row edited straight in the database would otherwise slip past the
        check that only ever ran during a sync.
        """
        SapReport.objects.filter(pk=self.report.pk).update(
            sql_text="DELETE FROM OITM", is_runnable=True
        )
        self.report.refresh_from_db()

        with self.assertRaises(SapReportSqlError):
            self.runner().run(self.report, {})
        self.reader.run_statement.assert_not_called()

    def test_a_report_with_no_filters_runs(self):
        report = SapReport.objects.create(
            company=self.company,
            sap_internal_key=2,
            sap_name="EXP DATE",
            sap_category_id=22,
            sap_category_name="Factory",
            sql_text=normalise_sql(SQL_NO_PROMPTS),
            sql_hash=sql_hash(SQL_NO_PROMPTS),
            slug="exp-date",
        )

        result = self.runner().run(report, {})

        self.assertEqual(result["meta"]["row_count"], 1)
        _, params = self.reader.run_statement.call_args[0]
        self.assertEqual(params, [])


# ---------------------------------------------------------------------------
# 5. exports
# ---------------------------------------------------------------------------


class TestExports(TestCase):

    COLUMNS = [
        {"key": "SKU", "label": "SKU", "type": "text"},
        {"key": "Qty", "label": "Current Stock", "type": "number"},
    ]
    ROWS = [["FG0000081", 20424.0], ["FG0000404", None]]
    META = {
        "title": "Warehouse-wise Stock",
        "company": "JIVO_OIL",
        "executed_at": "2026-08-22T14:31:00+05:30",
        "row_count": 2,
        "was_truncated": False,
        "parameters": [{"label": "Item", "value": "FG0000081"}],
    }

    def test_csv_has_a_header_and_blanks_for_nulls(self):
        response = exports.csv_response("Warehouse-wise Stock", self.COLUMNS, self.ROWS)

        body = response.content.decode("utf-8-sig")
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertEqual(
            body.splitlines(),
            ["SKU,Current Stock", "FG0000081,20424.0", "FG0000404,"],
        )

    def test_a_download_is_named_after_the_report(self):
        response = exports.csv_response("Warehouse-wise Stock", self.COLUMNS, self.ROWS)

        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("warehouse-wise-stock-", response["Content-Disposition"])
        self.assertIn(".csv", response["Content-Disposition"])

    def test_xlsx_is_a_workbook_with_the_rows_and_the_filters(self):
        import io

        import openpyxl

        response = exports.xlsx_response(
            "Warehouse-wise Stock", self.COLUMNS, self.ROWS, self.META
        )

        workbook = openpyxl.load_workbook(io.BytesIO(response.content))
        self.assertIn("Filters", workbook.sheetnames)
        sheet = workbook["Warehouse-wise Stock"]
        self.assertEqual([cell.value for cell in sheet[1]], ["SKU", "Current Stock"])
        self.assertEqual(sheet["A2"].value, "FG0000081")
        filters = workbook["Filters"]
        self.assertEqual(filters["A1"].value, "Report")

    def test_a_value_starting_with_equals_is_not_left_as_a_formula(self):
        self.assertEqual(exports._excel_safe("=1+1"), "'=1+1")
        self.assertEqual(exports._excel_safe("normal"), "normal")
        self.assertEqual(exports._excel_safe(12.5), 12.5)
        self.assertIsNone(exports._excel_safe(None))

    def test_a_long_report_name_still_makes_a_legal_sheet_name(self):
        name = exports._sheet_name("STOCK TRANSFER REPORT / GUPTA GODOWN [FG]")

        self.assertLessEqual(len(name), 31)
        for character in "\\/*?:[]":
            self.assertNotIn(character, name)


# ---------------------------------------------------------------------------
# 6. API
# ---------------------------------------------------------------------------


class SapReportsAPITestCase(APITestCase):
    """A user with a company, a report in the catalogue, and no real SAP."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="analyst@test.com",
            password="testpass123",
            full_name="Analyst",
            employee_code="EMP200",
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Analyst")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_default=True, is_active=True
        )

        self.report = SapReport.objects.create(
            company=self.company,
            sap_internal_key=1,
            sap_name="STOCK TRANSFER  REPORT",
            sap_category_id=22,
            sap_category_name="Factory",
            sql_text=normalise_sql(SQL_DATE_RANGE),
            sql_hash=sql_hash(SQL_DATE_RANGE),
            slug="stock-transfer-report",
        )
        SapReportParameter.objects.create(
            report=self.report, position=0, label="From date", kind=ParameterKind.DATE
        )
        SapReportParameter.objects.create(
            report=self.report, position=1, label="To date", kind=ParameterKind.DATE
        )

        self.client = APIClient()
        self._authenticate()

    def _authenticate(self):
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}",
            HTTP_COMPANY_CODE="JIVO_OIL",
        )

    def grant(self, codename):
        content_type = ContentType.objects.get_for_model(SapReport)
        permission, _ = Permission.objects.get_or_create(
            codename=codename,
            content_type=content_type,
            defaults={"name": codename.replace("_", " ")},
        )
        self.user.user_permissions.add(permission)
        # Permissions are cached per instance for the life of a request.
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self._authenticate()


class TestSapReportsAccess(SapReportsAPITestCase):

    URL = "/api/v1/sap-reports/reports/"

    def test_anonymous_access_is_refused(self):
        client = APIClient()
        self.assertEqual(client.get(self.URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_user_without_the_permission_is_refused(self):
        self.assertEqual(self.client.get(self.URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_missing_company_header_is_refused(self):
        self.grant("can_view_sap_reports")
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        self.assertEqual(self.client.get(self.URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_viewer_sees_the_catalogue(self):
        self.grant("can_view_sap_reports")

        response = self.client.get(self.URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["slug"], "stock-transfer-report")
        self.assertEqual(response.data["meta"]["categories"], ["Factory"])
        self.assertFalse(response.data["meta"]["can_manage"])


class TestSapReportsCatalogueAPI(SapReportsAPITestCase):

    def setUp(self):
        super().setUp()
        self.grant("can_view_sap_reports")

    def test_another_companys_report_is_not_listed(self):
        """A saved query belongs to one company database and means nothing elsewhere."""
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        SapReport.objects.create(
            company=other,
            sap_internal_key=1,
            sap_name="MART ONLY",
            sap_category_id=22,
            sap_category_name="Factory",
            sql_text="select 1 from dummy",
            sql_hash="x",
            slug="mart-only",
        )

        response = self.client.get("/api/v1/sap-reports/reports/")

        self.assertEqual([r["slug"] for r in response.data["data"]], ["stock-transfer-report"])

    def test_a_disabled_report_is_hidden(self):
        self.report.is_enabled = False
        self.report.save()

        response = self.client.get("/api/v1/sap-reports/reports/")

        self.assertEqual(response.data["data"], [])

    def test_an_unrunnable_report_is_hidden(self):
        self.report.is_runnable = False
        self.report.not_runnable_reason = "contains a DELETE"
        self.report.save()

        response = self.client.get("/api/v1/sap-reports/reports/")

        self.assertEqual(response.data["data"], [])

    def test_a_manager_can_see_the_hidden_ones_to_fix_them(self):
        self.grant("can_manage_sap_reports")
        self.report.is_enabled = False
        self.report.save()

        response = self.client.get("/api/v1/sap-reports/reports/?include_hidden=true")

        self.assertEqual(len(response.data["data"]), 1)

    def test_search_matches_name_and_description(self):
        response = self.client.get("/api/v1/sap-reports/reports/?search=transfer")
        self.assertEqual(len(response.data["data"]), 1)

        response = self.client.get("/api/v1/sap-reports/reports/?search=nothing-like-this")
        self.assertEqual(len(response.data["data"]), 0)

    def test_detail_lists_the_filters_to_render(self):
        response = self.client.get("/api/v1/sap-reports/reports/stock-transfer-report/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        parameters = response.data["data"]["parameters"]
        self.assertEqual([p["label"] for p in parameters], ["From date", "To date"])
        self.assertEqual(parameters[0]["kind"], "DATE")
        self.assertFalse(parameters[0]["has_lookup"])

    def test_an_unknown_report_is_a_404(self):
        response = self.client.get("/api/v1/sap-reports/reports/no-such-report/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_sql_is_only_for_a_manager(self):
        url = "/api/v1/sap-reports/reports/stock-transfer-report/sql/"
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        self.grant("can_manage_sap_reports")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("OWTR", response.data["data"]["sql_text"])

    def test_a_viewer_cannot_edit_a_report(self):
        response = self.client.patch(
            "/api/v1/sap-reports/reports/stock-transfer-report/",
            {"display_name": "Nope"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_manager_can_rename_a_report_and_relabel_its_filters(self):
        self.grant("can_manage_sap_reports")

        response = self.client.patch(
            "/api/v1/sap-reports/reports/stock-transfer-report/",
            {
                "display_name": "Stock Transfers",
                "description": "Transfers between warehouses.",
                "parameters": [{"position": 0, "label": "Transfers from", "kind": "DATE"}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.report.refresh_from_db()
        self.assertEqual(self.report.display_name, "Stock Transfers")
        parameter = self.report.parameters.get(position=0)
        self.assertEqual(parameter.label, "Transfers from")
        self.assertTrue(parameter.is_customised)

    def test_a_manager_can_make_a_filter_optional_without_touching_its_label(self):
        """
        The setup dialog only ever sends a filter's requiredness.

        The rest of the parameter — its inferred label and type — must be left as
        the SQL described it, so a partial patch has to be a partial update.
        """
        self.grant("can_manage_sap_reports")

        response = self.client.patch(
            "/api/v1/sap-reports/reports/stock-transfer-report/",
            {"parameters": [{"position": 1, "is_required": False}]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        parameter = self.report.parameters.get(position=1)
        self.assertFalse(parameter.is_required)
        self.assertEqual(parameter.label, "To date")
        self.assertEqual(parameter.kind, ParameterKind.DATE)
        # Marked human-owned, so the next sync cannot re-infer it back to required.
        self.assertTrue(parameter.is_customised)
        self.assertTrue(self.report.parameters.get(position=0).is_required)


class TestRunReportAPI(SapReportsAPITestCase):

    URL = "/api/v1/sap-reports/reports/stock-transfer-report/run/"

    def setUp(self):
        super().setUp()
        self.grant("can_view_sap_reports")
        patcher = patch("sap_reports.views.SapReportRunner")
        self.runner_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = self.runner_class.return_value
        self.runner.run.return_value = {
            "columns": [{"key": "DocNum", "label": "DocNum", "type": "number"}],
            "rows": [[626080206]],
            "meta": {"row_count": 1, "was_truncated": False},
        }

    def test_a_run_returns_the_result_set(self):
        response = self.client.post(
            self.URL, {"parameters": {"0": "2026-08-01", "1": "2026-08-22"}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["rows"], [[626080206]])
        self.assertEqual(
            self.runner.run.call_args[0][1], {"0": "2026-08-01", "1": "2026-08-22"}
        )

    def test_a_bad_filter_is_a_400_not_a_500(self):
        self.runner.run.side_effect = SapReportParameterError("'From date' is required.")

        response = self.client.post(self.URL, {"parameters": {}}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("From date", response.data["detail"])

    def test_a_broken_saved_query_is_a_502_with_saps_complaint(self):
        """These reports are authored in SAP; a broken one is not our crash."""
        self.runner.run.side_effect = SAPDataError("SAP rejected this report: invalid column")

        response = self.client.post(
            self.URL, {"parameters": {"0": "2026-08-01", "1": "2026-08-22"}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("invalid column", response.data["detail"])

    def test_sap_being_down_is_a_503(self):
        self.runner.run.side_effect = SAPConnectionError("unreachable")

        response = self.client.post(
            self.URL, {"parameters": {"0": "2026-08-01", "1": "2026-08-22"}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_a_switched_off_report_is_a_409(self):
        self.runner.run.side_effect = SapReportError("This report is switched off.")

        response = self.client.post(self.URL, {"parameters": {}}, format="json")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_an_over_large_row_limit_is_rejected_by_validation(self):
        response = self.client.post(
            self.URL, {"parameters": {}, "row_limit": 10_000_000}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("row_limit", response.data["errors"])


class TestExportReportAPI(SapReportsAPITestCase):

    URL = "/api/v1/sap-reports/reports/stock-transfer-report/export/"

    def setUp(self):
        super().setUp()
        self.grant("can_view_sap_reports")
        patcher = patch("sap_reports.views.SapReportRunner")
        self.runner_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.runner_class.return_value.run.return_value = {
            "columns": [{"key": "DocNum", "label": "DocNum", "type": "number"}],
            "rows": [[626080206]],
            "meta": {"title": "Stock Transfers", "parameters": []},
        }

    def test_csv_is_downloadable(self):
        response = self.client.post(self.URL, {"export_format": "csv"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment;", response["Content-Disposition"])

    def test_xlsx_is_the_default(self):
        response = self.client.post(self.URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("spreadsheetml", response["Content-Type"])

    def test_an_export_is_run_as_an_export(self):
        self.client.post(self.URL, {"export_format": "csv"}, format="json")

        self.assertEqual(
            self.runner_class.return_value.run.call_args.kwargs["export_format"], "csv"
        )

    def test_an_unknown_format_is_rejected(self):
        response = self.client.post(self.URL, {"export_format": "pdf"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TestParameterOptionsAPI(SapReportsAPITestCase):

    def setUp(self):
        super().setUp()
        self.grant("can_view_sap_reports")
        SapReportParameter.objects.create(
            report=self.report, position=2, label="Warehouse", kind=ParameterKind.WAREHOUSE
        )
        patcher = patch("sap_reports.views.SapReportLookupService")
        self.service_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.service_class.return_value.options_for.return_value = [
            {"value": "BH-FG", "label": "Bhakharpur Finished Goods"}
        ]

    def test_options_come_back_for_a_lookup_parameter(self):
        response = self.client.get(
            "/api/v1/sap-reports/reports/stock-transfer-report/parameters/2/options/?search=BH"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"][0]["value"], "BH-FG")
        self.assertEqual(response.data["meta"]["kind"], "WAREHOUSE")
        self.service_class.return_value.options_for.assert_called_once_with("WAREHOUSE", "BH")

    def test_a_date_parameter_has_no_options(self):
        self.service_class.return_value.options_for.return_value = []

        response = self.client.get(
            "/api/v1/sap-reports/reports/stock-transfer-report/parameters/0/options/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"], [])

    def test_an_unknown_parameter_is_a_404(self):
        response = self.client.get(
            "/api/v1/sap-reports/reports/stock-transfer-report/parameters/9/options/"
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TestSyncAPI(SapReportsAPITestCase):

    URL = "/api/v1/sap-reports/sync/"

    def setUp(self):
        super().setUp()
        self.grant("can_view_sap_reports")
        patcher = patch("sap_reports.views.SapReportCatalogService")
        self.service_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.service_class.return_value.sync.return_value = {
            "company": "JIVO_OIL",
            "category": "Factory",
            "found_in_sap": 21,
            "created": ["EXP DATE"],
            "updated": [],
            "unchanged": [],
            "not_runnable": [],
            "missing_in_sap": [],
            "dry_run": False,
        }

    def test_a_viewer_cannot_sync(self):
        self.assertEqual(
            self.client.post(self.URL, {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_manager_syncs_the_default_category(self):
        self.grant("can_manage_sap_reports")

        response = self.client.post(self.URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["found_in_sap"], 21)
        self.service_class.return_value.sync.assert_called_once_with(
            category_name="Factory", dry_run=False
        )

    def test_a_named_category_can_be_synced(self):
        self.grant("can_manage_sap_reports")

        self.client.post(self.URL, {"category": "GST R1", "dry_run": True}, format="json")

        self.service_class.return_value.sync.assert_called_once_with(
            category_name="GST R1", dry_run=True
        )

    def test_all_categories_syncs_without_a_category_filter(self):
        self.grant("can_manage_sap_reports")

        self.client.post(self.URL, {"all_categories": True}, format="json")

        self.service_class.return_value.sync.assert_called_once_with(
            category_name=None, dry_run=False
        )

    def test_sap_being_down_during_a_sync_is_a_503(self):
        self.grant("can_manage_sap_reports")
        self.service_class.return_value.sync.side_effect = SAPConnectionError("down")

        response = self.client.post(self.URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class TestRunHistoryAPI(SapReportsAPITestCase):

    def setUp(self):
        super().setUp()
        self.grant("can_view_sap_reports")
        SapReportRun.objects.create(
            report=self.report,
            company=self.company,
            run_by=self.user,
            parameters={"0": "20260801"},
            status=SapReportRun.Status.SUCCESS,
            row_count=42,
            duration_ms=310,
        )

    def test_one_reports_history_is_visible_to_a_viewer(self):
        response = self.client.get(
            "/api/v1/sap-reports/reports/stock-transfer-report/runs/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"][0]["row_count"], 42)
        self.assertEqual(response.data["meta"]["total"], 1)

    def test_the_company_wide_audit_feed_needs_the_manage_permission(self):
        self.assertEqual(
            self.client.get("/api/v1/sap-reports/runs/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.grant("can_manage_sap_reports")
        response = self.client.get("/api/v1/sap-reports/runs/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)


class TestCategoriesAPI(SapReportsAPITestCase):

    URL = "/api/v1/sap-reports/categories/"

    def setUp(self):
        super().setUp()
        self.grant("can_view_sap_reports")
        patcher = patch("sap_reports.views.SapReportCatalogService")
        self.service_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.service_class.return_value.list_categories.return_value = [
            {"category_id": 22, "category_name": "Factory", "query_count": 21}
        ]

    def test_a_viewer_cannot_list_sap_categories(self):
        self.assertEqual(self.client.get(self.URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_manager_sees_what_could_be_synced(self):
        self.grant("can_manage_sap_reports")

        response = self.client.get(self.URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"][0]["category_name"], "Factory")
        self.assertEqual(response.data["meta"]["default_category"], "Factory")


class TestReaderRowShaping(TestCase):
    """``HanaSapReportReader`` value and column handling, without a connection."""

    def reader(self):
        from sap_reports.hana_reader import HanaSapReportReader

        context = MagicMock()
        context.hana = {
            "host": "h",
            "port": 30015,
            "user": "u",
            "password": "p",
            "schema": "JIVO_OIL_HANADB",
        }
        return HanaSapReportReader(context)

    def test_duplicate_column_names_are_made_unique(self):
        """A real report ("DOLLY MAM BST") selects the same alias twice."""
        cursor = MagicMock()
        cursor.description = [
            ("INVOICE Taxable Value", 5, 0, 0, 0, 0, True),
            ("INVOICE Taxable Value", 5, 0, 0, 0, 0, True),
        ]

        columns = self.reader()._describe(cursor)

        self.assertEqual([c["key"] for c in columns], [
            "INVOICE Taxable Value",
            "INVOICE Taxable Value (2)",
        ])
        self.assertEqual(columns[1]["label"], "INVOICE Taxable Value")

    def test_column_types_are_classified_for_display(self):
        cursor = MagicMock()
        cursor.description = [
            ("ItemCode", 11, 0, 0, 0, 0, False),
            ("OnHand", 5, 0, 0, 0, 0, True),
            ("CreateDate", 16, 0, 0, 0, 0, True),
        ]

        columns = self.reader()._describe(cursor)

        self.assertEqual([c["type"] for c in columns], ["text", "number", "date"])

    def test_values_are_made_json_safe(self):
        from datetime import date, datetime
        from decimal import Decimal

        safe = self.reader()._json_safe

        self.assertEqual(safe(Decimal("114476.1944")), 114476.1944)
        self.assertEqual(safe(datetime(2026, 8, 22, 0, 0)), "2026-08-22")
        self.assertEqual(safe(datetime(2026, 8, 22, 14, 30)), "2026-08-22 14:30:00")
        self.assertEqual(safe(date(2026, 8, 22)), "2026-08-22")
        self.assertIsNone(safe(None))
        self.assertEqual(safe(b"\x01\x02"), "0102")

    def test_a_midnight_timestamp_is_shown_as_a_plain_date(self):
        """SAP posts dates as midnight timestamps; a time of 00:00 is noise."""
        from datetime import datetime

        self.assertEqual(self.reader()._json_safe(datetime(2026, 8, 22)), "2026-08-22")

    def test_blank_master_data_codes_are_dropped_from_options(self):
        options = self.reader()._as_options([("  ", "blank"), ("FG1", "Item one"), ("FG2", "")])

        self.assertEqual(
            options,
            [{"value": "FG1", "label": "Item one"}, {"value": "FG2", "label": "FG2"}],
        )
