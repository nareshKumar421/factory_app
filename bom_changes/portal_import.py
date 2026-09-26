"""Read SAP Portal's ``ZBOM_REQUESTS`` rows into BOM change requests.

Pure parsing, no database: ``import_portal_bom_requests`` uses it for the dry
run as well as the real one. Each row is keyed exactly as the portal's table
(``backend_v1/services/bomRequestStore.js`` ``bootstrap``): ``ID``, ``TYPE``,
``ITEM_CODE``, ``ITEM_NAME``, ``QTY``, ``BOM_TYPE``, ``WAREHOUSE``, ``DISTR_RULE``,
``PROJECT``, ``COMPONENTS``, ``ORIGINAL_DATA``, ``STATUS``, ``SUBMITTED_BY``,
``SUBMITTED_NAME``, ``SUBMITTED_AT``, ``APPROVAL_LOG``, ``REJECTED_BY``,
``REJECTED_AT``, ``SAP_PUSHED_AT``, ``SAP_PUSHED_BY``, ``SAP_RESULT``, ``COMPANY``.

What becomes what:

* ``COMPANY`` holds the SAP company database (``JIVO_OIL_HANADB``); it maps to a
  JI company code through the inverse of ``settings.COMPANY_DB``. Rows written
  before the portal added the column have none; they are reported and skipped
  unless the operator names ``--default-company``.
* ``COMPONENTS`` (JSON, the page's ``itemCode``/``qty``/``issueMethod``/...) →
  lines; an issue method the portal mapped by alias (``Stock``, ``Phantom``...)
  lands on the Manual/Backflush it was pushed as.
* ``APPROVAL_LOG`` (JSON) → decision rows. The level is the step the request
  was at (``status`` in the entry); an ``admin`` entry is a direct push (level
  0), which is what the portal's admin approval was.
* ``ORIGINAL_DATA`` and ``SAP_RESULT`` are kept as they were.
* Timestamps: the portal wrote UTC with the zone cut off
  (``bomRequestStore.js`` ``toTs``), so a naive value is read as UTC.
* People: portal usernames are not JI logins and are never matched to one by
  name. They are kept as text (``legacy_submitted_by`` and friends).
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings

from . import workflow
from .constants import (
    DIRECT_LEVEL,
    ISSUE_METHOD_MAP,
    OPEN_STATUSES,
    ApprovalAction,
    BOMChangeKind,
    BOMChangeStatus,
    BOMType,
    IssueMethod,
    LineType,
)

COLUMNS = (
    "ID", "TYPE", "ITEM_CODE", "ITEM_NAME", "QTY", "BOM_TYPE", "WAREHOUSE", "DISTR_RULE",
    "PROJECT", "COMPONENTS", "ORIGINAL_DATA", "STATUS", "SUBMITTED_BY", "SUBMITTED_NAME",
    "SUBMITTED_AT", "APPROVAL_LOG", "REJECTED_BY", "REJECTED_AT", "SAP_PUSHED_AT",
    "SAP_PUSHED_BY", "SAP_RESULT", "COMPANY",
)

_FRACTION = re.compile(r"(\.\d{6})\d+")


class RowProblem(Exception):
    """This row cannot be imported; the message says why."""


@dataclass
class ParsedRequest:
    legacy_id: int
    company_code: str
    header: dict
    lines: list = field(default_factory=list)
    approvals: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def load_rows(path) -> list[dict]:
    """A JSON array of rows, or an object holding one (``{"rows": [...]}`` or a
    tool's ``{"<query>": [...]}`` export)."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        lists = [value for value in data.values() if isinstance(value, list)]
        if len(lists) != 1:
            raise ValueError("Expected a JSON array of rows, or an object holding exactly one.")
        data = lists[0]
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of rows.")
    return data


def company_codes_by_db() -> dict[str, str]:
    """``JIVO_OIL_HANADB`` → ``JIVO_OIL``: the inverse of ``settings.COMPANY_DB``."""
    return {
        str(db).strip().upper(): code
        for code, db in (getattr(settings, "COMPANY_DB", {}) or {}).items()
        if db
    }


def parse_timestamp(value):
    """A portal timestamp as an aware datetime; naive values are UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = _FRACTION.sub(r"\1", str(value).strip())
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            raise RowProblem(f"unreadable timestamp {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt_timezone.utc)
    return moment


def parse_json(value, *, column: str):
    """An NCLOB of JSON as the export wrote it (text), or already parsed."""
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise RowProblem(f"{column} is not valid JSON")


def _text(row, column) -> str:
    value = row.get(column)
    return "" if value is None else str(value).strip()


def _decimal(value, *, default: Decimal) -> Decimal:
    """The portal's ``Number(x) || default``: missing, unreadable or zero → default."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    if not number.is_finite() or number <= 0:
        return default
    return number


def _person(username: str, name: str) -> str:
    username, name = (username or "").strip(), (name or "").strip()
    if name and username and name != username:
        return f"{name} ({username})"
    return name or username


def _limit(value: str, size: int, column: str) -> str:
    if len(value) > size:
        raise RowProblem(f"{column} is longer than {size} characters")
    return value


def _issue_method(value) -> str:
    sap = ISSUE_METHOD_MAP.get(str(value or "").strip())
    return IssueMethod.BACKFLUSH if sap == "im_Backflush" else IssueMethod.MANUAL


def _lines(components, notes) -> list[dict]:
    lines = []
    skipped = 0
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            skipped += 1
            continue
        code = str(component.get("itemCode") or "").strip().upper()
        if not code:
            skipped += 1
            continue
        comment = str(component.get("note") or component.get("comment") or "").strip()
        visual_order = component.get("visualOrder")
        try:
            visual_order = int(visual_order) if visual_order is not None else index
        except (TypeError, ValueError):
            visual_order = index
        lines.append(
            {
                "visual_order": max(visual_order, 0),
                "item_type": LineType.RESOURCE if component.get("itemType") == "pit_Resource" else LineType.ITEM,
                "item_code": _limit(code, 50, "a component's itemCode"),
                "item_name": _limit(str(component.get("itemName") or "").strip(), 200, "a component's itemName"),
                "quantity": _decimal(component.get("qty"), default=Decimal("1")),
                "issue_method": _issue_method(component.get("issueMethod")),
                "warehouse": _limit(str(component.get("warehouse") or "").strip(), 20, "a component's warehouse"),
                "unit_cost": _decimal(component.get("unitCost"), default=Decimal("0")),
                "comment": _limit(comment, 254, "a component's note"),
            }
        )
    if skipped:
        notes.append(f"{skipped} component(s) without an item code left out")
    return lines


def _approvals(log, row, status, notes) -> list[dict]:
    approvals = []
    approved_so_far = 0
    for entry in log:
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip().lower()
        if action not in ("approve", "reject"):
            notes.append(f"approval log entry with action {entry.get('action')!r} left out")
            continue
        from_status = str(entry.get("status") or "").strip().upper()
        step = workflow.step_of(from_status)
        if (entry.get("role") or "") == "admin":
            level = DIRECT_LEVEL
        elif step is not None:
            level = step + 1
        else:
            level = approved_so_far + 1
        if from_status not in BOMChangeStatus.values:
            # Older entries carry no status: the request was wherever the
            # approvals before this one had taken it.
            from_status = OPEN_STATUSES[min(approved_so_far, len(OPEN_STATUSES) - 1)]
        if action == "approve":
            approved_so_far += 1
        approvals.append(
            {
                "level": level,
                "from_status": from_status,
                "action": ApprovalAction.APPROVE if action == "approve" else ApprovalAction.REJECT,
                "legacy_decided_by": _person(entry.get("username"), entry.get("name"))[:160],
                "remarks": str(entry.get("comment") or ""),
                "decided_at": parse_timestamp(entry.get("timestamp")),
            }
        )
    rejected_by = _text(row, "REJECTED_BY")
    if (
        status == BOMChangeStatus.REJECTED
        and rejected_by
        and not any(a["action"] == ApprovalAction.REJECT for a in approvals)
    ):
        # The portal set REJECTED_BY together with the log entry; keep the
        # rejection even where the log lost it.
        approvals.append(
            {
                "level": approved_so_far + 1,
                "from_status": OPEN_STATUSES[min(approved_so_far, len(OPEN_STATUSES) - 1)],
                "action": ApprovalAction.REJECT,
                "legacy_decided_by": rejected_by[:160],
                "remarks": "",
                "decided_at": parse_timestamp(row.get("REJECTED_AT")),
            }
        )
        notes.append("rejection taken from REJECTED_BY (not in the approval log)")
    return approvals


def parse_row(row: dict, db_to_code: dict[str, str], default_company: str | None = None) -> ParsedRequest:
    """One portal row, checked and mapped. Raises :class:`RowProblem`."""
    if not isinstance(row, dict):
        raise RowProblem("not a row object")
    try:
        legacy_id = int(row.get("ID"))
    except (TypeError, ValueError):
        raise RowProblem(f"ID {row.get('ID')!r} is not a number")
    if legacy_id <= 0:
        raise RowProblem(f"ID {legacy_id} is not positive")

    notes: list[str] = []
    database = _text(row, "COMPANY").upper()
    if not database:
        if not default_company:
            raise RowProblem("COMPANY is empty (pass --default-company to import these)")
        company_code = default_company
        notes.append(f"no COMPANY; imported under {default_company}")
    else:
        company_code = db_to_code.get(database)
        if not company_code:
            raise RowProblem(f"COMPANY {database} is not one of settings.COMPANY_DB")

    kind = _text(row, "TYPE").upper()
    if kind not in BOMChangeKind.values:
        raise RowProblem(f"TYPE {kind or '(empty)'} is neither CREATE nor UPDATE")
    status = _text(row, "STATUS").upper() or BOMChangeStatus.PENDING
    if status not in BOMChangeStatus.values:
        raise RowProblem(f"STATUS {status} is not a status the portal's flow produces")
    item_code = _text(row, "ITEM_CODE").upper()
    if not item_code:
        raise RowProblem("ITEM_CODE is empty")

    bom_type_text = _text(row, "BOM_TYPE")
    bom_type = next(
        (value for value in BOMType.values if value.lower() == bom_type_text.lower()), None
    )
    if bom_type is None:
        bom_type = BOMType.PRODUCTION
        if bom_type_text:
            notes.append(f"BOM_TYPE {bom_type_text!r} read as Production (the portal pushed it so)")

    components = parse_json(row.get("COMPONENTS"), column="COMPONENTS")
    if components is None:
        components = []
    if not isinstance(components, list):
        raise RowProblem("COMPONENTS is not a JSON array")
    lines = _lines(components, notes)
    if not lines:
        notes.append("no components")

    try:
        log = parse_json(row.get("APPROVAL_LOG"), column="APPROVAL_LOG") or []
    except RowProblem:
        log = []
        notes.append("APPROVAL_LOG unreadable; decisions not imported")
    if not isinstance(log, list):
        log = []
        notes.append("APPROVAL_LOG is not a list; decisions not imported")

    sap_result = parse_json(row.get("SAP_RESULT"), column="SAP_RESULT")
    if sap_result in ([], {}):
        sap_result = None  # the portal stored ``[]`` for "no result"
    original = parse_json(row.get("ORIGINAL_DATA"), column="ORIGINAL_DATA")

    submitted_at = parse_timestamp(row.get("SUBMITTED_AT"))
    if submitted_at is None:
        notes.append("no SUBMITTED_AT; the import time is used")

    header = {
        "kind": kind,
        "item_code": _limit(item_code, 50, "ITEM_CODE"),
        "item_name": _limit(_text(row, "ITEM_NAME"), 200, "ITEM_NAME"),
        "quantity": _decimal(row.get("QTY"), default=Decimal("1")),
        "bom_type": bom_type,
        "warehouse": _limit(_text(row, "WAREHOUSE"), 20, "WAREHOUSE"),
        "distribution_rule": _limit(_text(row, "DISTR_RULE"), 50, "DISTR_RULE"),
        "project": _limit(_text(row, "PROJECT"), 50, "PROJECT"),
        "status": status,
        "submitted_at": submitted_at,
        "original_data": original,
        "sap_result": sap_result,
        "sap_pushed_at": parse_timestamp(row.get("SAP_PUSHED_AT")),
        "legacy_portal_id": legacy_id,
        "legacy_submitted_by": _person(_text(row, "SUBMITTED_BY"), _text(row, "SUBMITTED_NAME"))[:160],
        "legacy_sap_pushed_by": _text(row, "SAP_PUSHED_BY")[:160],
    }
    return ParsedRequest(
        legacy_id=legacy_id,
        company_code=company_code,
        header=header,
        lines=lines,
        approvals=_approvals(log, row, status, notes),
        notes=notes,
    )
