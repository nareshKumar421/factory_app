"""
sap_reports/sql.py

Parsing, safety-checking and parameter binding for SAP Business One *saved
queries* -- the ones users author in SAP's Query Manager and that live in the
``OUQR`` table of each company database.

Three jobs live here:

1. ``normalise_sql``    - SAP stores line breaks as bare CR; make the text readable.
2. ``assert_read_only`` - refuse anything that isn't a single read statement.
3. ``bind_prompts``     - turn SAP's ``[%0]`` prompts into real bind markers so a
                          user-supplied value can never be concatenated into SQL.

Nothing in this module talks to the database.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .exceptions import SapReportSqlError

# SAP writes a prompt as a *quoted* placeholder, e.g. WHERE T0."DocDate" >= '[%0]'.
# Every prompt in every Factory query is of this form, but a query author can
# also leave the quotes off (WHERE T0."DocNum" = [%0]), so both are handled.
QUOTED_PROMPT_RE = re.compile(r"'\[%(\d+)\]'")
BARE_PROMPT_RE = re.compile(r"\[%(\d+)\]")

# Statements we are willing to run. Saved queries are reports, so this is the
# whole vocabulary: a plain select, a CTE, or a call into a reporting procedure.
ALLOWED_LEADING_KEYWORDS = ("SELECT", "WITH", "CALL")

# Tokens that must never appear in a report, checked *after* string literals and
# comments are stripped so a column alias like "Last Update" cannot trip it.
FORBIDDEN_TOKEN_RE = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|MERGE|UPSERT"
    r"|CREATE|GRANT|REVOKE|COMMIT|ROLLBACK|EXEC"
    r")\b",
    re.IGNORECASE,
)

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT_RE = re.compile(r"--[^\n]*")
STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
QUOTED_IDENTIFIER_RE = re.compile(r'"(?:[^"]|"")*"')


class StatementKind:
    SELECT = "SELECT"
    CALL = "CALL"


# ---------------------------------------------------------------------------
# Normalising
# ---------------------------------------------------------------------------


def normalise_sql(sql: str) -> str:
    """
    Returns the query text with SAP's bare-CR line breaks turned into newlines.

    SAP B1 stores ``OUQR.QString`` with CR line separators. HANA treats a CR as
    whitespace so execution works either way, but anything that *displays* the
    SQL (admin, API, logs) is unreadable until this runs.
    """
    if not sql:
        return ""
    return sql.replace("\r\n", "\n").replace("\r", "\n").strip()


def sql_hash(sql: str) -> str:
    """Fingerprint of the normalised text, used to spot edits made inside SAP."""
    return hashlib.sha256(normalise_sql(sql).encode("utf-8")).hexdigest()


def _strip_noise(sql: str) -> str:
    """
    Query text with comments, string literals and quoted names blanked out.

    Column names and aliases have to go as well as literals: SAP reports select
    things like ``T0."UpdateDate" AS "Last Update"``, and a guard that read the
    word inside the quotes as a statement would refuse a perfectly good report.
    A real write keeps its keyword outside any quotes, so it is still caught.
    """
    stripped = BLOCK_COMMENT_RE.sub(" ", sql)
    stripped = LINE_COMMENT_RE.sub(" ", stripped)
    stripped = STRING_LITERAL_RE.sub("''", stripped)
    return QUOTED_IDENTIFIER_RE.sub('""', stripped)


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def _leading_keyword(sql: str) -> str:
    analysed = _strip_noise(normalise_sql(sql)).lstrip().lstrip("(").lstrip()
    match = re.match(r"[A-Za-z_]+", analysed)
    return match.group(0).upper() if match else ""


def detect_statement_kind(sql: str) -> str:
    """
    Returns ``StatementKind.CALL`` for a procedure call, else ``SELECT``.

    Several Factory reports are thin wrappers over a HANA procedure, e.g.
    ``CALL "REPORT_DOLLY" (...)``. They still hand back a result set, but they
    are worth telling apart: their columns are only known once they have run.
    """
    return StatementKind.CALL if _leading_keyword(sql) == "CALL" else StatementKind.SELECT


def assert_read_only(sql: str) -> None:
    """
    Raises ``SapReportSqlError`` unless ``sql`` is one read-only statement.

    This guards what SAP hands us, not what the caller types: prompt values are
    always bound (see :func:`bind_prompts`), so the only way a write could reach
    here is a saved query that was never meant to be a report.
    """
    normalised = normalise_sql(sql)
    if not normalised:
        raise SapReportSqlError("The report has no SQL text.")

    analysed = _strip_noise(normalised)

    leading = _leading_keyword(normalised)
    if leading not in ALLOWED_LEADING_KEYWORDS:
        raise SapReportSqlError(
            f"Only SELECT/WITH/CALL reports can be run; this one starts with "
            f"'{leading or '?'}'."
        )

    forbidden = FORBIDDEN_TOKEN_RE.search(analysed)
    if forbidden:
        raise SapReportSqlError(
            f"The report contains a '{forbidden.group(1).upper()}' statement and "
            f"will not be run."
        )

    # One statement only. A single trailing semicolon is normal in SAP and fine.
    if [part for part in analysed.split(";")[1:] if part.strip()]:
        raise SapReportSqlError("The report contains more than one SQL statement.")


def is_runnable(sql: str) -> Tuple[bool, str]:
    """``(True, "")`` if the query passes :func:`assert_read_only`, else the reason."""
    try:
        assert_read_only(sql)
    except SapReportSqlError as error:
        return False, str(error)
    return True, ""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    """One ``[%N]`` placeholder occurrence in the query text."""

    position: int          # the N in [%N]
    start: int             # index of the placeholder (quotes included) in the SQL
    end: int
    quoted: bool
    context_before: str    # the ~90 characters of SQL that precede it

    @property
    def hint_column(self) -> str:
        """
        The column this prompt is compared against, e.g. ``DocDate``.

        Reads both the plain form (``T0."DocDate" >= '[%0]'``) and SAP's own
        prompt annotation (``I."WhsCode" = /* T0."WhsCode" */ '[%0]'``), which
        Query Manager writes when the prompt is linked to a table field.
        """
        annotated = re.search(r'/\*[^*]*"([A-Za-z_0-9]+)"[^*]*\*/\s*$', self.context_before)
        if annotated:
            return annotated.group(1)

        columns = re.findall(r'"([A-Za-z_0-9]+)"', self.context_before)
        return columns[-1] if columns else ""

    @property
    def comparison(self) -> str:
        """The operator just before the prompt: ``>=``, ``<=``, ``BETWEEN``, ``=`` ..."""
        tail = BLOCK_COMMENT_RE.sub(" ", self.context_before).rstrip()
        for operator in (">=", "<=", "<>", "!="):
            if tail.endswith(operator):
                return operator
        if re.search(r"\bBETWEEN\s*$", tail, re.IGNORECASE):
            return "BETWEEN"
        if re.search(r"\b(LIKE|IN)\s*\(?\s*$", tail, re.IGNORECASE):
            return "LIKE"
        for operator in (">", "<", "="):
            if tail.endswith(operator):
                return operator
        if re.search(r"\bAND\s*$", tail, re.IGNORECASE):
            return "AND"
        return ""


def find_prompts(sql: str) -> List[Prompt]:
    """Every prompt occurrence in the query, in the order it appears in the text."""
    normalised = normalise_sql(sql)
    prompts: List[Prompt] = []
    covered: List[Tuple[int, int]] = []

    for match in QUOTED_PROMPT_RE.finditer(normalised):
        prompts.append(_prompt_from_match(normalised, match, quoted=True))
        covered.append(match.span())

    for match in BARE_PROMPT_RE.finditer(normalised):
        # Skip the ones already picked up together with their surrounding quotes.
        if any(start <= match.start() and match.end() <= end for start, end in covered):
            continue
        prompts.append(_prompt_from_match(normalised, match, quoted=False))

    prompts.sort(key=lambda prompt: prompt.start)
    return prompts


def _prompt_from_match(sql: str, match: "re.Match", *, quoted: bool) -> Prompt:
    return Prompt(
        position=int(match.group(1)),
        start=match.start(),
        end=match.end(),
        quoted=quoted,
        context_before=sql[max(0, match.start() - 90):match.start()],
    )


def first_prompt_per_position(sql: str) -> Dict[int, Prompt]:
    """
    The first occurrence of each prompt number, keyed by number.

    A prompt used twice describes the same input, so its first appearance is the
    one whose surrounding SQL is used to guess the parameter's type and label.
    """
    prompts: Dict[int, Prompt] = {}
    for prompt in find_prompts(sql):
        prompts.setdefault(prompt.position, prompt)
    return prompts


def prompt_positions(sql: str) -> List[int]:
    """The distinct prompt numbers the query asks for, ascending."""
    return sorted({prompt.position for prompt in find_prompts(sql)})


def bind_prompts(sql: str, values: Dict[int, object]) -> Tuple[str, List[object]]:
    """
    Replaces every ``[%N]`` with a ``?`` marker and returns the bind list.

    A prompt used twice in one query (``BETWEEN '[%0]' AND '[%0]'``) yields two
    markers bound to the same value, so the caller only ever supplies one value
    per position. Values are never interpolated into the statement text.
    """
    normalised = normalise_sql(sql)
    prompts = find_prompts(normalised)

    missing = sorted({prompt.position for prompt in prompts} - set(values))
    if missing:
        raise SapReportSqlError(
            "Missing value for parameter "
            + ", ".join(f"[%{position}]" for position in missing)
        )

    statement_parts: List[str] = []
    params: List[object] = []
    cursor = 0
    for prompt in prompts:
        statement_parts.append(normalised[cursor:prompt.start])
        statement_parts.append("?")
        params.append(values[prompt.position])
        cursor = prompt.end
    statement_parts.append(normalised[cursor:])

    return "".join(statement_parts), params
