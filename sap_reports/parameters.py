"""
sap_reports/parameters.py

Turning SAP's numbered prompts into a form a screen can render.

A saved query only tells us *that* it wants a value (``'[%0]'``) -- never what
the value means. SAP itself relies on the user remembering, which is exactly the
part that does not survive being moved into our app. So the surrounding SQL is
read to guess each prompt's type and label ("From date", "Warehouse", ...), and
that guess is stored once at sync time; a human can correct any parameter
afterwards and the correction is never overwritten by a later sync.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from .exceptions import SapReportParameterError
from .sql import BLOCK_COMMENT_RE, Prompt, find_prompts, normalise_sql


class ParameterKind:
    """How the frontend should ask for a value, and how we validate it."""

    DATE = "DATE"
    TEXT = "TEXT"
    NUMBER = "NUMBER"
    ITEM = "ITEM"
    WAREHOUSE = "WAREHOUSE"
    BUSINESS_PARTNER = "BUSINESS_PARTNER"
    ITEM_GROUP = "ITEM_GROUP"
    PERIOD = "PERIOD"

    CHOICES = [
        (DATE, "Date"),
        (TEXT, "Free text"),
        (NUMBER, "Number"),
        (ITEM, "Item / SKU"),
        (WAREHOUSE, "Warehouse"),
        (BUSINESS_PARTNER, "Customer / vendor"),
        (ITEM_GROUP, "Item group"),
        (PERIOD, "Fiscal period"),
    ]

    # Kinds the API can offer a picklist for; the rest are typed in.
    LOOKUP_KINDS = (ITEM, WAREHOUSE, BUSINESS_PARTNER, ITEM_GROUP, PERIOD)


# SAP B1 column -> what the prompt is really asking for. Checked before the
# generic "anything with Date in the name is a date" rule below.
COLUMN_KINDS = {
    "ITEMCODE": ParameterKind.ITEM,
    "ITEMNAME": ParameterKind.ITEM,
    "WHSCODE": ParameterKind.WAREHOUSE,
    "WAREHOUSE": ParameterKind.WAREHOUSE,
    "FROMWHSCOD": ParameterKind.WAREHOUSE,
    "TOWHSCODE": ParameterKind.WAREHOUSE,
    "WHSNAME": ParameterKind.WAREHOUSE,
    "CARDCODE": ParameterKind.BUSINESS_PARTNER,
    "CARDNAME": ParameterKind.BUSINESS_PARTNER,
    "ITMSGRPCOD": ParameterKind.ITEM_GROUP,
    "ITMSGRPNAM": ParameterKind.ITEM_GROUP,
    "DOCNUM": ParameterKind.NUMBER,
    "DOCENTRY": ParameterKind.NUMBER,
    "LOCCODE": ParameterKind.NUMBER,
}

COLUMN_LABELS = {
    "ITEMCODE": "Item",
    "ITEMNAME": "Item name",
    "WHSCODE": "Warehouse",
    "WAREHOUSE": "Warehouse",
    "FROMWHSCOD": "From warehouse",
    "TOWHSCODE": "To warehouse",
    "CARDCODE": "Customer / vendor",
    "CARDNAME": "Customer / vendor name",
    "ITMSGRPCOD": "Item group",
    "ITMSGRPNAM": "Item group",
    "DOCNUM": "Document number",
    "DOCENTRY": "Document entry",
    "LOCCODE": "Branch",
    "DOCDATE": "Posting date",
    "REFDATE": "Posting date",
    "TAXDATE": "Document date",
    "PRDDATE": "Manufacturing date",
    "EXPDATE": "Expiry date",
}

# Tables that pin down an otherwise meaningless column name: OFCT."Name" is a
# fiscal period, not a person's name.
TABLE_KINDS = {
    "OFCT": ParameterKind.PERIOD,
    "OITM": ParameterKind.ITEM,
    "OWHS": ParameterKind.WAREHOUSE,
    "OCRD": ParameterKind.BUSINESS_PARTNER,
    "OITB": ParameterKind.ITEM_GROUP,
}

DATE_COLUMN_RE = re.compile(r"DATE", re.IGNORECASE)

# Which end of a range an operator puts the prompt at, most trustworthy first.
# A prompt can be compared several ways in one query -- "Purchase working Query"
# uses [%0] both as `DocDate < '[%0]'` (an opening-balance cut-off) and as the
# start of `BETWEEN '[%0]' AND '[%1]'`. The BETWEEN is what the user is being
# asked for, so it outranks the bare comparison.
RANGE_PRIORITY = (
    ("BETWEEN", "From"),
    ("AND", "To"),
    (">=", "From"),
    ("<=", "To"),
    (">", "From"),
    ("<", "To"),
)

# A query author's way of making a prompt optional: `(T."X" = '[%0]' OR '[%0]' = '')`.
# Leaving such a prompt blank is a deliberate "no filter", not a missing value.
OPTIONAL_IDIOM_RE = re.compile(r"'\[%(\d+)\]'\s*(?:=|<>|!=)\s*''|''\s*(?:=|<>|!=)\s*'\[%(\d+)\]'")


@dataclass
class InferredParameter:
    """The guess made for one prompt, ready to be stored on a report."""

    position: int
    label: str
    kind: str
    is_required: bool
    help_text: str
    is_quoted: bool = True
    default_value: str = ""
    blank_value: str = ""
    occurrences: int = 1
    _hint_column: str = field(default="", repr=False)


def infer_parameters(sql: str) -> List[InferredParameter]:
    """Reads ``sql`` and describes each of its prompts, ordered by prompt number."""
    normalised = normalise_sql(sql)
    by_position: Dict[int, List[Prompt]] = defaultdict(list)
    for prompt in find_prompts(normalised):
        by_position[prompt.position].append(prompt)

    optional = optional_positions(normalised)

    inferred: List[InferredParameter] = []
    for position in sorted(by_position):
        prompts = by_position[position]
        kind, hint_column = _infer_kind(prompts)
        inferred.append(
            InferredParameter(
                position=position,
                label=_infer_label(prompts, kind, hint_column, position),
                kind=kind,
                is_required=position not in optional,
                help_text=f'SAP field "{hint_column}"' if hint_column else "",
                is_quoted=prompts[0].quoted,
                occurrences=len(prompts),
                _hint_column=hint_column,
            )
        )

    return _disambiguate_labels(inferred)


def optional_positions(sql: str) -> set:
    """Prompt numbers the query itself allows to be left blank."""
    analysed = BLOCK_COMMENT_RE.sub(" ", normalise_sql(sql))
    return {
        int(match.group(1) or match.group(2))
        for match in OPTIONAL_IDIOM_RE.finditer(analysed)
    }


def _infer_kind(prompts: List[Prompt]):
    """
    The kind for one prompt number, plus the column the guess came from.

    Occurrences are tried in order and the first that resolves to something more
    specific than free text wins; an ``IN ('[%0]', '[%1]')`` list, where only the
    first placeholder sits next to the column name, still types every member
    because each one's context reaches back past the list to the column.
    """
    fallback_column = ""
    for prompt in prompts:
        column = (prompt.hint_column or "").upper()
        table = _hint_table(prompt)
        fallback_column = fallback_column or column

        if column in COLUMN_KINDS:
            return COLUMN_KINDS[column], prompt.hint_column
        if column and DATE_COLUMN_RE.search(column):
            return ParameterKind.DATE, prompt.hint_column
        if table in TABLE_KINDS:
            return TABLE_KINDS[table], prompt.hint_column

    return ParameterKind.TEXT, fallback_column


def _hint_table(prompt: Prompt) -> str:
    """The last SAP table named in the SQL just before the prompt, if any."""
    tables = re.findall(r"\b(O[A-Z]{3}|[A-Z]{3}\d)\b", prompt.context_before.upper())
    return tables[-1] if tables else ""


def _infer_label(prompts: List[Prompt], kind: str, hint_column: str, position: int) -> str:
    column = (hint_column or "").upper()
    base = COLUMN_LABELS.get(column) or _humanise(column) or f"Parameter {position + 1}"

    if kind == ParameterKind.DATE:
        end = _range_end(prompts)
        if end:
            return f"{end} date"
        return base if "date" in base.lower() else f"{base} date"

    if kind == ParameterKind.PERIOD:
        return "Fiscal period"

    return base


def _range_end(prompts: List[Prompt]) -> str:
    """``"From"``/``"To"`` if the prompt bounds a range, else ``""``."""
    comparisons = {prompt.comparison for prompt in prompts}
    for operator, end in RANGE_PRIORITY:
        if operator in comparisons:
            return end
    return ""


def _humanise(column: str) -> str:
    """``U_Dipatch_Date`` -> ``Dipatch date``; good enough for a starting label."""
    if not column:
        return ""
    cleaned = re.sub(r"^U_", "", column, flags=re.IGNORECASE).replace("_", " ")
    return cleaned.capitalize()


def _disambiguate_labels(parameters: List[InferredParameter]) -> List[InferredParameter]:
    """
    Numbers labels that would otherwise repeat.

    "Honey special report FG" filters on ``Warehouse IN ('[%0]', '[%1]',
    '[%2]', '[%3]')``: four prompts, one column, and four boxes all captioned
    "Warehouse" would be unusable.
    """
    counts = defaultdict(int)
    for parameter in parameters:
        counts[parameter.label] += 1

    seen = defaultdict(int)
    for parameter in parameters:
        if counts[parameter.label] > 1:
            seen[parameter.label] += 1
            parameter.label = f"{parameter.label} {seen[parameter.label]}"
    return parameters


# ---------------------------------------------------------------------------
# Coercing what the caller sent into what SAP expects
# ---------------------------------------------------------------------------

SAP_DATE_FORMAT = "%Y%m%d"
ACCEPTED_DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y", "%d-%m-%Y")


def coerce_value(kind: str, raw, *, label: str, quoted: bool = True):
    """
    Returns the bind value for one parameter, or raises ``SapReportParameterError``.

    Prompts that SAP wrote quoted (``'[%0]'``) keep string semantics so the
    statement behaves exactly as it does inside Query Manager; HANA casts the
    string against the column it is compared to, as it already did for SAP.
    """
    if isinstance(raw, str):
        raw = raw.strip()

    if raw is None or raw == "":
        raise SapReportParameterError(f"'{label}' is required.")

    if kind == ParameterKind.DATE:
        return _coerce_date(raw, label=label)

    if kind == ParameterKind.NUMBER:
        number = _coerce_number(raw, label=label)
        return str(number) if quoted else number

    return str(raw)


def _coerce_date(raw, *, label: str) -> str:
    if isinstance(raw, datetime):
        return raw.date().strftime(SAP_DATE_FORMAT)
    if isinstance(raw, date):
        return raw.strftime(SAP_DATE_FORMAT)

    text = str(raw).strip()
    for date_format in ACCEPTED_DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).strftime(SAP_DATE_FORMAT)
        except ValueError:
            continue
    raise SapReportParameterError(f"'{label}' must be a date (YYYY-MM-DD).")


def _coerce_number(raw, *, label: str):
    text = str(raw).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    try:
        return float(text)
    except (TypeError, ValueError):
        raise SapReportParameterError(f"'{label}' must be a number.")


def build_bind_values(parameters, supplied: Optional[Dict] = None) -> Dict[int, object]:
    """
    Maps ``{position: bind value}`` from what the caller supplied.

    ``parameters`` is any iterable of objects exposing ``position``, ``label``,
    ``kind``, ``is_required``, ``default_value``, ``blank_value`` and
    ``is_quoted`` -- i.e. either stored ``SapReportParameter`` rows or the
    inferred ones.
    """
    supplied = supplied or {}
    values: Dict[int, object] = {}

    for parameter in parameters:
        raw = supplied.get(str(parameter.position), supplied.get(parameter.position))
        if raw in (None, ""):
            raw = getattr(parameter, "default_value", "") or None

        if raw in (None, ""):
            if getattr(parameter, "is_required", True):
                raise SapReportParameterError(f"'{parameter.label}' is required.")
            # The query has an "or blank means no filter" escape for this prompt,
            # so bind the sentinel that switches the filter off.
            values[parameter.position] = getattr(parameter, "blank_value", "") or ""
            continue

        values[parameter.position] = coerce_value(
            parameter.kind,
            raw,
            label=parameter.label,
            quoted=getattr(parameter, "is_quoted", True),
        )

    return values
