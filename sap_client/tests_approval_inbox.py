"""Tests for the general SAP approval inbox's SAP side (ported from SAP Portal).

The reader (``hana/approval_inbox_reader.py``), the draft-line writer and the
signer check. Ports the portal's own unit cases — ``tests/sap-status-filter``,
``stale-approval-requests`` and ``credit-note-duplicates`` — onto the Python
rule, and pins what the port added: the superseded-request rule, identity by
SAP user id only, bound values, and fail-soft decoration versus the fail-closed
duplicate check that gates an approval. HANA and the Service Layer are faked.

    python manage.py test sap_client.tests_approval_inbox --settings=config.sqlite_test_settings
"""

from datetime import date
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from hdbcli import dbapi

from . import approval_status
from .exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .hana import approval_inbox_reader as inbox
from .hana.approval_inbox_reader import (
    EFFECTIVE_STATUS_SQL,
    OBJECT_TYPE_LABELS,
    HanaApprovalInboxReader,
    inbox_status,
    posted_duplicates,
    stale_message,
)
from .service_layer.approval_signer import ApprovalSignerCheck
from .service_layer.draft_line_writer import DraftLineWriter

SL_CONFIG = {
    "base_url": "https://sl.test:50000",
    "company_db": "TEST_DB",
    "username": "svc",
    "password": "svc-pass",
    "approvers": {"USER37": "stored-pass"},
}


def _context():
    context = MagicMock()
    context.service_layer = dict(SL_CONFIG)
    context.hana = {"host": "h", "port": 1, "user": "u", "password": "p", "schema": "SCHEMA"}
    context.company_code = "JIVO_OIL"
    return context


class _FakeHana:
    """A connection that answers each statement from a queue of dict rows."""

    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.connections = 0

    def connect(self):
        self.connections += 1
        return self

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass


class _FakeCursor:
    def __init__(self, hana):
        self.hana = hana
        self.description = None
        self._rows = []

    def execute(self, sql, params):
        self.hana.statements.append((sql, params))
        result = self.hana.results.pop(0)
        if isinstance(result, Exception):
            raise result
        keys = list(result[0]) if result else []
        self.description = [(key,) for key in keys]
        self._rows = [tuple(row.get(key) for key in keys) for row in result]

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def _reader(*results):
    reader = HanaApprovalInboxReader(_context())
    fake = _FakeHana(*results)
    reader.connection = MagicMock()
    reader.connection.connect.side_effect = fake.connect
    reader.connection.schema = "JIVO_OIL_HANADB"
    return reader, fake


def _header(**overrides):
    """One OWDD row as the header SELECT returns it."""
    row = {
        "WddCode": 75424, "ObjType": "14", "DraftEntry": 57198, "OwnerID": 12,
        "CurrStep": 20, "IsDraft": "Y", "WtmCode": 106, "OwddStatus": "W",
        "CreateDate": date(2026, 9, 16), "CreateTime": 1005, "Remarks": None,
        "DraftStatus": "W", "EffStatus": "W", "Superseded": 0,
        "OdrfEntry": 57198, "DocType": "I", "DocNum": 626092648,
        "CardCode": "CUSTA000844", "CardName": "ILAHI CO.", "NumAtCard": "RN-1",
        "DocTotal": 17455.0, "DocCur": "INR", "DocDate": date(2026, 9, 16),
        "Comments": "RN-1626096511", "OwnerCode": "USER12", "OwnerName": "ATUL SHARMA",
        "TemplateName": "USER37 RETURNS", "ApproverCode": "USER37", "ApproverName": "HONEY SINGH",
        "RejectRemarks": None, "DecidedBy": None, "DecidedByName": None,
        "DecidedDate": None, "DecidedTime": None, "WaitingOnMe": 1,
    }
    row.update(overrides)
    return row


USER = [{"UserID": 37}]


# ---------------------------------------------------------------------------
# The pending rule — the portal's stale-approval-requests.test.js, plus the
# superseded request the port added
# ---------------------------------------------------------------------------


class InboxStatusRuleTests(SimpleTestCase):
    def test_a_pending_request_on_a_still_pending_draft_stays_pending(self):
        self.assertEqual(inbox_status("W", "Y", "W", superseded=False), "W")

    def test_a_pending_request_takes_the_outcome_its_draft_reached(self):
        for draft, expected in (("C", "C"), ("N", "N"), ("Y", "Y"), ("P", "P"), ("A", "A")):
            with self.subTest(draft=draft):
                self.assertEqual(inbox_status("W", "Y", draft, superseded=False), expected)

    def test_a_draft_that_is_gone_or_no_longer_under_approval_is_cancelled(self):
        for draft in (None, "-"):
            self.assertEqual(inbox_status("W", "Y", draft, superseded=False), "C")

    def test_a_request_on_an_existing_document_keeps_its_own_status(self):
        self.assertEqual(inbox_status("W", "N", None, superseded=False), "W")
        # A request with no draft is never superseded: the template rule is for drafts.
        self.assertEqual(inbox_status("W", "N", None, superseded=True), "W")

    def test_an_edited_drafts_old_request_is_cancelled_though_the_draft_is_pending(self):
        """The gap in the portal's rule: the draft is back at W for the NEW request."""
        self.assertEqual(inbox_status("W", "Y", "W", superseded=True), "C")
        self.assertEqual(inbox_status("W", "tYES", "W", superseded=True), "C")

    def test_decided_requests_are_never_rewritten(self):
        self.assertEqual(inbox_status("Y", "Y", "C", superseded=True), "Y")
        self.assertEqual(inbox_status("N", "Y", "W", superseded=False), "N")
        self.assertEqual(inbox_status("C", "Y", "W", superseded=False), "C")

    def test_the_sql_rule_carries_both_halves(self):
        self.assertIn('W2."WtmCode" = W."WtmCode"', EFFECTIVE_STATUS_SQL)
        self.assertIn('MAX(W2."WddCode")', EFFECTIVE_STATUS_SQL)
        self.assertIn(approval_status.effective_status_sql("W"), EFFECTIVE_STATUS_SQL)
        self.assertIn("""W."IsDraft" = 'Y'""", EFFECTIVE_STATUS_SQL)

    def test_every_status_filter_scans_the_rows_still_at_w(self):
        """The portal's sap-status-filter cases, as the app's words."""
        self.assertEqual(approval_status.APP_FILTER_TO_OWDD["APPROVED"], ("Y", "P", "A"))
        self.assertEqual(approval_status.APP_FILTER_TO_OWDD["GENERATED"], ("P", "A"))
        for status, codes in approval_status.APP_FILTER_TO_OWDD.items():
            with self.subTest(status=status):
                self.assertIn("W", approval_status.owdd_codes_to_scan(codes))

    def test_the_refusal_says_what_happened(self):
        self.assertRegex(
            stale_message({"wdd_code": 390, "status": "CANCELLED", "stale_pending": True}),
            r"#390.*was cancelled",
        )
        self.assertIn(
            "was rejected",
            stale_message({"wdd_code": 7, "status": "REJECTED", "stale_pending": True}),
        )
        self.assertIn(
            "newer request",
            stale_message({"wdd_code": 7, "status": "CANCELLED", "stale_pending": True,
                           "superseded": True}),
        )
        self.assertIn(
            "already approved",
            stale_message({"wdd_code": 7, "status": "APPROVED", "stale_pending": False}),
        )


# ---------------------------------------------------------------------------
# Duplicates — the portal's credit-note-duplicates.test.js
# ---------------------------------------------------------------------------


def _posted(doc_entry, doc_num):
    return {"doc_entry": doc_entry, "doc_num": doc_num, "doc_date": "2026-09-16", "table": "ORIN"}


class EntryNowTests(SimpleTestCase):
    """Where a rejected entry stands now — its own draft first, else the
    correction keyed as a new document."""

    ROW = {"draft_entry": 100, "raised_on": "2026-09-10", "reference": "INV-7",
           "total_amount": "5000.00"}

    @staticmethod
    def _draft(entry, status, *, ref="INV-7", total=5000, created=date(2026, 9, 12), num=None):
        return {"DocEntry": entry, "WddStatus": status, "NumAtCard": ref, "DocTotal": total,
                "CreateDate": created, "DocNum": num or entry}

    @staticmethod
    def _posted(entry, *, ref="INV-7", total=5000, created=date(2026, 9, 14), draft_key=None):
        return {"DocEntry": entry, "NumAtCard": ref, "DocTotal": total, "CreateDate": created,
                "DocNum": 9000 + entry, "draftKey": draft_key}

    def now(self, own="N", drafts=(), posted=(), **row):
        return inbox.entry_now({**self.ROW, **row}, own_status=own,
                               drafts=list(drafts), posted=list(posted))

    def test_untouched_is_still_rejected_and_a_closed_draft_is_closed(self):
        self.assertEqual(self.now("N")["stage"], "STILL_REJECTED")
        self.assertEqual(self.now("C")["stage"], "CLOSED")
        self.assertEqual(self.now(None)["stage"], "CLOSED")

    def test_its_own_draft_taken_forward_is_the_answer(self):
        for own, stage in (("W", "PENDING"), ("Y", "APPROVED"), ("P", "POSTED"), ("A", "POSTED")):
            with self.subTest(own=own):
                self.assertEqual(self.now(own), {"stage": stage, "via": "same_request",
                                                 "doc_num": None, "posted": False})

    def test_a_re_keyed_draft_with_the_same_reference_gives_its_stage(self):
        for status, stage in (("W", "PENDING"), ("Y", "APPROVED"), ("N", "REJECTED_AGAIN")):
            with self.subTest(status=status):
                found = self.now("C", drafts=[self._draft(101, status)])
                self.assertEqual((found["stage"], found["via"], found["doc_num"]),
                                 (stage, "reference", 101))

    def test_a_posted_copy_beats_a_draft_and_the_own_draft_is_never_its_correction(self):
        found = self.now(
            "C",
            drafts=[self._draft(100, "W"), self._draft(101, "W")],
            posted=[self._posted(7, draft_key=100), self._posted(8, draft_key=101)],
        )
        self.assertEqual((found["stage"], found["posted"], found["doc_num"]), ("POSTED", True, 9008))

    def test_without_the_reference_only_the_same_amount_soon_after_counts(self):
        soon = self._draft(101, "W", ref="", created=date(2026, 9, 20))
        late = self._draft(102, "W", ref="", created=date(2026, 10, 10))
        other = self._draft(103, "W", ref="", total=4999)
        before = self._draft(104, "W", created=date(2026, 9, 1))
        self.assertEqual(self.now("N", drafts=[soon])["via"], "amount")
        for draft in (late, other, before):
            with self.subTest(draft=draft["DocEntry"]):
                self.assertEqual(self.now("N", drafts=[draft])["stage"], "STILL_REJECTED")

    def test_a_reference_match_beats_an_amount_match(self):
        found = self.now("C", drafts=[self._draft(105, "W", ref="", num=55),
                                      self._draft(101, "Y", total=4800, num=11)])
        self.assertEqual((found["stage"], found["doc_num"]), ("APPROVED", 11))


class PostedDuplicatesTests(SimpleTestCase):
    def test_a_pending_request_whose_twin_draft_already_posted_is_a_duplicate(self):
        row = {"status": "PENDING", "duplicate_of_posted": [_posted(1963, 626096824)]}
        self.assertEqual([p["doc_num"] for p in posted_duplicates(row)], [626096824])

    def test_an_approved_request_is_not_a_duplicate_of_its_own_document(self):
        row = {"status": "APPROVED", "already_posted_as": _posted(1963, 626096824)}
        self.assertEqual(posted_duplicates(row), [])

    def test_a_still_pending_request_whose_own_draft_posted_is_a_leftover(self):
        row = {"status": "PENDING", "already_posted_as": _posted(1963, 626096824)}
        self.assertEqual([p["doc_num"] for p in posted_duplicates(row)], [626096824])

    def test_a_probe_with_no_status_is_treated_as_open(self):
        self.assertEqual(len(posted_duplicates({"already_posted_as": _posted(1963, 1)})), 1)

    def test_an_ordinary_request_carries_nothing(self):
        self.assertEqual(posted_duplicates({"status": "PENDING"}), [])
        self.assertEqual(posted_duplicates({}), [])

    def test_the_same_posted_document_reached_two_ways_is_reported_once(self):
        row = {
            "status": "PENDING",
            "already_posted_as": _posted(1963, 626096824),
            "duplicate_of_posted": [_posted(1963, 626096824), _posted(14089, 626082614)],
        }
        self.assertEqual([p["doc_entry"] for p in posted_duplicates(row)], [1963, 14089])


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


class ListRequestsTests(SimpleTestCase):
    def test_an_unknown_sap_user_sees_nothing_and_no_list_is_read(self):
        reader, fake = _reader([])
        self.assertEqual(reader.list_requests("USER99"), [])
        self.assertEqual(len(fake.statements), 1)
        self.assertEqual(fake.statements[0][1], ("USER99",))

    def test_identity_is_the_sap_user_id_only(self):
        """No name or login matching (the portal's sapApprovals.js:83-89)."""
        reader, fake = _reader(USER, [], )
        reader.list_requests("user37")
        user_sql, user_params = fake.statements[0]
        self.assertIn('UPPER("USER_CODE") = ?', user_sql)
        self.assertEqual(user_params, ("USER37",))
        list_sql, list_params = fake.statements[1]
        self.assertNotIn("U_NAME\") = ?", list_sql)
        # Every visibility clause binds the resolved USERID.
        self.assertEqual(list_params.count(37), 5)

    def test_all_uses_the_portals_visibility_rule(self):
        reader, fake = _reader(USER, [])
        reader.list_requests("USER37", scope="all")
        sql = fake.statements[1][0]
        self.assertIn('x."OwnerID" = ?', sql)
        self.assertIn(
            """(x."EffStatus" <> 'W' OR (L."Status" = 'W' AND L."StepCode" = x."CurrStep"))""", sql
        )

    def test_waiting_on_me_ignores_the_status_filter(self):
        reader, fake = _reader(USER, [])
        reader.list_requests("USER37", scope="waiting_on_me", status="APPROVED")
        sql, params = fake.statements[1]
        self.assertIn("""x."EffStatus" = 'W' AND""", sql)
        self.assertNotIn("Y", params)

    def test_raised_by_me_is_ownership(self):
        reader, fake = _reader(USER, [])
        reader.list_requests("USER37", scope="raised_by_me")
        sql, params = fake.statements[1]
        self.assertIn('W."OwnerID" = ?', sql)
        self.assertNotIn('x."OwnerID" = ?', sql)

    def test_a_status_filter_scans_w_and_judges_by_the_effective_status(self):
        reader, fake = _reader(USER, [])
        reader.list_requests("USER37", status="REJECTED")
        sql, params = fake.statements[1]
        self.assertIn('W."Status" IN (?, ?)', sql)
        self.assertIn('x."EffStatus" IN (?)', sql)
        self.assertEqual(params.count("N"), 2)
        self.assertIn("W", params)

    def test_search_and_filters_are_bound_never_formatted(self):
        reader, fake = _reader(USER, [])
        reader.list_requests(
            "USER37", object_type="18", date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30), search="ab'c",
        )
        sql, params = fake.statements[1]
        self.assertNotIn("ab'c", sql)
        self.assertNotIn("AB'C", sql)
        self.assertIn("%AB'C%", params)
        self.assertIn("18", params)
        self.assertIn(date(2026, 9, 1), params)
        self.assertEqual(sql.count("?"), len(params))

    def test_a_number_also_matches_the_request_draft_and_document(self):
        reader, fake = _reader(USER, [])
        reader.list_requests("USER37", search="75424")
        sql, params = fake.statements[1]
        self.assertIn('W."WddCode" = ?', sql)
        self.assertEqual(params.count(75424), 3)

    def test_the_limit_is_clamped(self):
        reader, fake = _reader(USER, [])
        reader.list_requests("USER37", limit=10_000)
        self.assertIn("LIMIT 500", fake.statements[1][0])

    def test_unknown_scope_or_status_is_refused(self):
        reader, _ = _reader()
        with self.assertRaises(SAPValidationError):
            reader.list_requests("USER37", scope="everyone")
        with self.assertRaises(SAPValidationError):
            reader.list_requests("USER37", status="DONE")

    def test_rows_carry_the_document_the_people_and_the_labels(self):
        reader, fake = _reader(USER, [_header()], [], [])
        row = reader.list_requests("USER37")[0]
        self.assertEqual(row["wdd_code"], 75424)
        self.assertEqual(row["object_type_label"], "A/R Credit Note")
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["approver_code"], "USER37")
        self.assertEqual(row["originator_code"], "USER12")
        self.assertEqual(row["document"]["total_amount"], "17455.00")
        self.assertEqual(row["document"]["party_name"], "ILAHI CO.")
        self.assertEqual(row["created_at"], "2026-09-16T10:05:00")
        self.assertTrue(row["waiting_on_me"])
        # One connection for the whole page.
        self.assertEqual(fake.connections, 1)

    def test_a_leftover_is_listed_under_its_drafts_outcome(self):
        reader, _ = _reader(USER, [_header(EffStatus="C", DraftStatus="C")], [])
        row = reader.list_requests("USER37", status="CANCELLED")[0]
        self.assertEqual(row["status"], "CANCELLED")
        self.assertTrue(row["stale_pending"])
        # Nobody is waiting on a leftover.
        self.assertIsNone(row["approver_code"])

    def test_goods_receipt_and_issue_are_the_right_way_round(self):
        """The portal had 59 and 60 swapped."""
        self.assertEqual(OBJECT_TYPE_LABELS["59"], "Goods Receipt")
        self.assertEqual(OBJECT_TYPE_LABELS["60"], "Goods Issue")
        self.assertNotIn("1470000113", OBJECT_TYPE_LABELS)

    def test_connection_failure_is_an_outage_and_a_bad_query_a_data_error(self):
        reader = HanaApprovalInboxReader(_context())
        reader.connection = MagicMock()
        reader.connection.connect.side_effect = dbapi.Error("down")
        with self.assertRaises(SAPConnectionError):
            reader.list_requests("USER37")
        reader, _ = _reader(dbapi.Error("bad sql"))
        with self.assertRaises(SAPDataError):
            reader.list_requests("USER37")


class SiblingTests(SimpleTestCase):
    def test_siblings_are_grouped_by_draft_and_object_type_and_counted_live(self):
        reader, fake = _reader(
            USER,
            [_header()],
            [
                {"DraftEntry": 57198, "ObjType": "14", "WddCode": 75424, "WtmCode": 106,
                 "TemplateName": "USER37 RETURNS", "CreateDate": date(2026, 9, 16),
                 "CreateTime": 1005, "EffStatus": "W"},
                {"DraftEntry": 57198, "ObjType": "14", "WddCode": 75426, "WtmCode": 73,
                 "TemplateName": "USER26 FG", "CreateDate": date(2026, 9, 16),
                 "CreateTime": 1005, "EffStatus": "W"},
                # A payment draft with the same number is another document.
                {"DraftEntry": 57198, "ObjType": "46", "WddCode": 70001, "WtmCode": 5,
                 "TemplateName": None, "CreateDate": date(2026, 9, 1),
                 "CreateTime": 900, "EffStatus": "W"},
                # A superseded request holds nothing.
                {"DraftEntry": 57198, "ObjType": "14", "WddCode": 75000, "WtmCode": 106,
                 "TemplateName": None, "CreateDate": date(2026, 9, 10),
                 "CreateTime": 900, "EffStatus": "C"},
            ],
            [],
        )
        row = reader.list_requests("USER37")[0]
        self.assertEqual(row["request_count"], 2)
        self.assertEqual(row["pending_request_count"], 2)
        self.assertEqual([s["wdd_code"] for s in row["sibling_requests"]], [75426])
        sibling_sql = fake.statements[2][0]
        self.assertIn("""W."Status" <> 'C'""", sibling_sql)

    def test_a_failed_sibling_lookup_costs_the_flags_not_the_list(self):
        reader, _ = _reader(USER, [_header()], dbapi.Error("no"), [])
        with self.assertLogs("sap_client.hana.approval_inbox_reader", level="WARNING"):
            rows = reader.list_requests("USER37")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_count"], 1)


class DuplicateTests(SimpleTestCase):
    def _hit(self, kind, **overrides):
        hit = {
            "Kind": kind, "ObjType": "14", "DraftEntry": 57198, "FromDraft": 57100,
            "PostedEntry": 1963, "PostedDocNum": 626096824, "PostedTotal": 17455.0,
            "PostedCurrency": "INR", "PostedDate": date(2026, 9, 16),
        }
        hit.update(overrides)
        return hit

    def test_a_twin_that_already_posted_flags_the_pending_request(self):
        reader, fake = _reader(USER, [_header()], [], [self._hit("TWIN")])
        row = reader.list_requests("USER37")[0]
        self.assertTrue(row["is_duplicate"])
        self.assertEqual(row["posted_duplicates"][0]["doc_num"], 626096824)
        self.assertEqual(row["posted_duplicates"][0]["table"], "ORIN")
        sql, params = fake.statements[3]
        self.assertIn('"ORIN" p ON p."draftKey" = t."DocEntry"', sql)
        self.assertIn(57198, params)

    def test_a_decided_request_is_not_checked(self):
        reader, fake = _reader(USER, [_header(EffStatus="Y", OwddStatus="Y")], [])
        row = reader.list_requests("USER37")[0]
        self.assertFalse(row["is_duplicate"])
        self.assertEqual(len(fake.statements), 3)  # user, list, siblings — no duplicate read

    def test_types_without_a_posted_table_are_never_queried(self):
        """Transfers and payments have no posted-document check (portal's last case)."""
        reader, fake = _reader(USER, [_header(ObjType="67"), _header(WddCode=2, ObjType="46")], [])
        rows = reader.list_requests("USER37")
        self.assertFalse(any(r["is_duplicate"] for r in rows))
        self.assertEqual(len(fake.statements), 3)

    def test_only_credit_notes_look_for_a_twin_draft(self):
        reader, fake = _reader(USER, [_header(ObjType="18")], [], [])
        reader.list_requests("USER37")
        sql = fake.statements[3][0]
        self.assertNotIn("'TWIN'", sql)
        self.assertIn('"OPCH" p', sql)
        # The posted-directly shape needs the same reference or remarks.
        self.assertIn('p."NumAtCard" = d."NumAtCard"', sql)

    def test_a_leftover_of_its_own_posted_draft_is_flagged(self):
        reader, _ = _reader(USER, [_header()], [], [self._hit("SELF", FromDraft=57198)])
        row = reader.list_requests("USER37")[0]
        self.assertEqual(row["already_posted_as"]["doc_entry"], 1963)
        self.assertTrue(row["is_duplicate"])


# ---------------------------------------------------------------------------
# One request
# ---------------------------------------------------------------------------


class CurrentStageTests(SimpleTestCase):
    def _stage_header(self, **overrides):
        row = _header(LatestCode=75424)
        row.pop("WaitingOnMe")
        row.update(overrides)
        return row

    def test_the_status_is_computed_from_the_raw_columns(self):
        reader, _ = _reader(
            [self._stage_header(EffStatus="W", LatestCode=75500)],  # superseded
            [{"Code": "USER37", "Name": "HONEY SINGH"}],
        )
        stage = reader.current_stage(75424)
        self.assertEqual(stage["status"], "CANCELLED")
        self.assertTrue(stage["stale_pending"])
        self.assertTrue(stage["superseded"])

    def test_every_undecided_authorizer_of_the_stage_is_listed(self):
        reader, fake = _reader(
            [self._stage_header()],
            [{"Code": "USER26", "Name": "A"}, {"Code": "USER37", "Name": "B"}],
        )
        stage = reader.current_stage(75424)
        self.assertEqual(stage["status"], "PENDING")
        self.assertEqual(stage["authorizer_codes"], ["USER26", "USER37"])
        self.assertEqual(fake.statements[1][1], (75424, 20))

    def test_a_missing_request_is_none(self):
        reader, _ = _reader([])
        self.assertIsNone(reader.current_stage(1))

    def test_item_lines_carry_without_qty_posting(self):
        reader, fake = _reader(
            [self._stage_header()],
            [{"Code": "USER37", "Name": "B"}],
            [{"LineNum": 0, "ItemCode": "FG1", "NoInvtryMv": "N"},
             {"LineNum": 1, "ItemCode": "FG2", "NoInvtryMv": "Y"}],
        )
        stage = reader.current_stage(75424, with_item_lines=True)
        self.assertEqual(
            [(line["line_num"], line["without_qty_posting"]) for line in stage["item_lines"]],
            [(0, False), (1, True)],
        )
        self.assertIn('"ItemCode" IS NOT NULL', fake.statements[2][0])

    def test_the_duplicate_check_that_gates_an_approval_fails_closed(self):
        reader, _ = _reader(
            [self._stage_header()], [{"Code": "USER37", "Name": "B"}], dbapi.Error("boom")
        )
        with self.assertRaises(SAPDataError):
            reader.current_stage(75424, with_duplicates=True)

    def test_a_rejected_request_gets_the_duplicate_check_an_approval_needs(self):
        """Changing a rejection to an approval posts the document: check it first."""
        reader, fake = _reader(
            [self._stage_header(OwddStatus="N", EffStatus="N", DraftStatus="N",
                                DecidedBy="USER37")],
            [],
            [{"Kind": "TWIN", "ObjType": "14", "DraftEntry": 57198, "FromDraft": 57100,
              "PostedEntry": 1963, "PostedDocNum": 626096824, "PostedTotal": 17455.0,
              "PostedCurrency": "INR", "PostedDate": date(2026, 9, 16)}],
        )
        stage = reader.current_stage(75424, with_duplicates=True)
        self.assertEqual(stage["status"], "REJECTED")
        self.assertEqual(stage["decided_by"], "USER37")
        self.assertEqual([p["doc_entry"] for p in stage["posted_duplicates"]], [1963])
        self.assertEqual(len(fake.statements), 3)

    def test_an_approved_request_needs_no_duplicate_read(self):
        reader, fake = _reader(
            [self._stage_header(OwddStatus="Y", EffStatus="Y", DraftStatus="Y")], [],
        )
        stage = reader.current_stage(75424, with_duplicates=True)
        self.assertEqual(stage["status"], "APPROVED")
        self.assertEqual(stage["posted_duplicates"], [])
        self.assertEqual(len(fake.statements), 2)

    def test_duplicates_are_reported_on_the_stage(self):
        reader, _ = _reader(
            [self._stage_header()],
            [{"Code": "USER37", "Name": "B"}],
            [{"Kind": "POSTED", "ObjType": "14", "DraftEntry": 57198, "FromDraft": None,
              "PostedEntry": 14089, "PostedDocNum": 626082614, "PostedTotal": 17455.0,
              "PostedCurrency": "INR", "PostedDate": date(2026, 9, 16)}],
        )
        stage = reader.current_stage(75424, with_duplicates=True)
        self.assertEqual([p["doc_entry"] for p in stage["posted_duplicates"]], [14089])


class DetailTests(SimpleTestCase):
    def test_detail_reads_stages_lines_and_survives_missing_stage_names(self):
        reader, fake = _reader(
            USER,
            [_header()],
            [{"StepCode": 20, "Status": "W", "Remarks": None, "UpdateDate": None,
              "UpdateTime": None, "UserCode": "USER37", "UserName": "HONEY SINGH"},
             {"StepCode": 6, "Status": "Y", "Remarks": "ok", "UpdateDate": date(2026, 9, 16),
              "UpdateTime": 1130, "UserCode": "USER26", "UserName": "HARPREET"}],
            dbapi.Error("no OWST"),
            [{"LineNum": 0, "ItemCode": "FG1", "Dscription": "Oil", "Quantity": 2.0,
              "UnitMsr": "PCS", "Price": 10.0, "LineTotal": 20.0, "VatGroup": "IGST18",
              "WhsCode": "BH-GR", "AcctCode": None, "BaseType": 16, "BaseRef": "1626",
              "NoInvtryMv": "Y"}],
            [],
            [],
        )
        with self.assertLogs("sap_client.hana.approval_inbox_reader", level="WARNING"):
            data = reader.detail(75424, "USER37")
        self.assertEqual([s["step_code"] for s in data["stages"]], [20, 6])
        self.assertTrue(data["stages"][0]["is_current"])
        self.assertEqual(data["stages"][1]["decided_at"], "2026-09-16T11:30:00")
        self.assertIsNone(data["stages"][0]["stage_name"])
        self.assertTrue(data["lines"][0]["without_qty_posting"])
        self.assertEqual(data["lines"][0]["base_type_label"], "A/R Return")
        self.assertTrue(data["lines_available"])

    def test_a_payment_draft_has_no_lines_here(self):
        reader, _ = _reader(
            USER, [_header(ObjType="46", OdrfEntry=None)], [], [], [],
        )
        data = reader.detail(75424, "USER37")
        self.assertEqual(data["lines"], [])
        self.assertFalse(data["lines_available"])


class WaitingCountTests(SimpleTestCase):
    def test_the_badge_counts_what_waits_on_the_user(self):
        reader, fake = _reader(USER, [{"N": 4}])
        self.assertEqual(reader.waiting_count("USER37"), 4)
        sql, params = fake.statements[1]
        self.assertIn("""x."EffStatus" = 'W'""", sql)
        self.assertEqual(params, (37,))

    def test_an_unknown_user_counts_zero(self):
        reader, _ = _reader([])
        self.assertEqual(reader.waiting_count("USER99"), 0)


# ---------------------------------------------------------------------------
# Service Layer: Without Qty Posting and the signer check
# ---------------------------------------------------------------------------


def _response(status_code, json_body=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    response.content = b"x" if json_body is not None else b""
    response.json.return_value = json_body if json_body is not None else {}
    response.text = ""
    return response


class DraftLineWriterTests(SimpleTestCase):
    def setUp(self):
        from .service_layer import entity_client

        entity_client.clear_session_cache()
        patcher = patch("sap_client.service_layer.entity_client.ServiceLayerSession")
        self.session_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.session_class.return_value.login.return_value = {"B1SESSION": "s"}

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_only_the_named_lines_are_patched_as_the_portal_did(self, request):
        request.return_value = _response(204)
        DraftLineWriter(_context()).set_without_qty_posting(57198, [0, 2], True)
        method, url = request.call_args[0]
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.endswith("/b1s/v2/Drafts(57198)"))
        self.assertEqual(
            request.call_args[1]["json"],
            {"DocumentLines": [
                {"LineNum": 0, "WithoutInventoryMovement": "tYES"},
                {"LineNum": 2, "WithoutInventoryMovement": "tYES"},
            ]},
        )
        # Merge by LineNum — never replace the draft's whole line collection.
        self.assertNotIn("B1S-ReplaceCollectionsOnPatch", request.call_args[1]["headers"])

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_sap_refusing_the_change_is_a_validation_error(self, request):
        request.return_value = _response(400, {"error": {"code": -5002, "message": "no"}})
        with self.assertRaises(SAPValidationError):
            DraftLineWriter(_context()).set_without_qty_posting(57198, [0], False)

    @patch("sap_client.service_layer.entity_client.requests.request")
    def test_no_lines_means_no_request(self, request):
        DraftLineWriter(_context()).set_without_qty_posting(57198, [], True)
        request.assert_not_called()


class ApprovalSignerCheckTests(SimpleTestCase):
    def setUp(self):
        patcher = patch("sap_client.service_layer.approval_writer.ServiceLayerSession")
        self.session_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.session_class.return_value.login.return_value = {"B1SESSION": "s"}

    @patch("sap_client.service_layer.approval_writer.requests.patch")
    @patch("sap_client.service_layer.approval_writer.requests.get")
    def test_verify_logs_in_as_the_signer_and_changes_nothing(self, get, patch_):
        get.return_value = _response(200, {"Status": "arsPending"})
        self.assertEqual(ApprovalSignerCheck(_context()).verify(10, "USER37"), "USER37")
        self.assertEqual(self.session_class.call_args[0][0]["password"], "stored-pass")
        patch_.assert_not_called()

    @patch("sap_client.service_layer.approval_writer.requests.get")
    def test_a_typed_password_is_used_for_the_login(self, get):
        get.return_value = _response(200, {"Status": "arsPending"})
        ApprovalSignerCheck(_context()).verify(10, "USER37", password="typed")
        self.assertEqual(self.session_class.call_args[0][0]["password"], "typed")

    @patch("sap_client.service_layer.approval_writer.requests.get")
    def test_a_decided_request_is_refused(self, get):
        get.return_value = _response(200, {"Status": "arsApproved"})
        with self.assertRaises(SAPValidationError):
            ApprovalSignerCheck(_context()).verify(10, "USER37")

    def test_no_stored_password_is_refused_before_sap(self):
        with self.assertRaises(SAPValidationError):
            ApprovalSignerCheck(_context()).verify(10, "USER99")
        self.session_class.assert_not_called()


class ModuleSurfaceTests(SimpleTestCase):
    def test_the_duplicate_tables_are_a_fixed_whitelist(self):
        self.assertEqual(set(inbox.POSTED_TABLE), {str(t) for t in range(13, 23)})
        self.assertEqual(inbox.TWIN_DRAFT_TYPES, ("14", "19"))
