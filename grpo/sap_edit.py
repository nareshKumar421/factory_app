"""Editing a GRPO that is already in SAP, from the app.

What may change is what SAP itself lets change after posting, worked out on
2026-10-12 from SAP's change log (ADOC/ADO1: every saved version of every GRPO
since Oct 2024, all three companies) and checked field by field with PATCHes
on the TEST companies:

* **Any GRPO, header only:** vendor ref (``NumAtCard``), document date
  (``TaxDate``), delivery date (``DocDueDate``), remarks (``Comments``),
  contact person, the header's user-defined fields the company has (vehicle,
  bilty, transporter, bill of entry, receive date, city), and attachments:
  files added, and files removed (so a file is replaced by adding the new one
  and removing the old one in the same edit).
  The posting date, vendor, items, quantities, prices, warehouses, tax codes
  and accounts are fixed: the Service Layer answers "Field cannot be updated
  (ODBC -1029)" for those.
* **Service GRPO lines, while open:** the amount and the expense's effective
  month (dimension 2). A service GRPO posts no journal entry, so SAP lets its
  amount change until an A/P invoice closes the line. The amount is sent as the
  unit price with the discount cleared, so the line total is exactly what was
  typed; sending a line total instead makes SAP back-compute a discount (that
  is where the -4.22% and -15% discounts on live come from).

Some line fields the Service Layer *accepts* and then silently does not save
(free text, description, account and dimensions on an item GRPO). So nothing is
taken on SAP's word: after every PATCH the GRPO is read back, and an edit is
saved only when SAP holds every value asked for.

SAP's own rules (``SBO_SP_TRANSACTIONNOTIFICATION``) re-check the whole GRPO on
every update. The one that matters here: the app posts as ``B1i``, and SAP
refuses B1i's updates to a GRPO dated before the open back-date window
("(2011) Back Date Entry not allowed for Goods Receipt PO on ... by user B1i").
Only ``manager`` and Gurcharan (and user 48 in Mart) are exempt, and there are
no back-date grants (``BKDT``) for B1i. The screen warns up front
(:func:`back_date_floor`); SAP's own answer is what decides.

Another rule, in all three companies: a GRPO that is not cancelled must have an
attachment ("(200019) Please Attach its Receiving"; tried on TEST: taking the
link off a GRPO is refused). So removing files never leaves a GRPO with none:
the last file goes only when a new one comes in the same edit. Removing one file
of several is the Service Layer's ``B1S-ReplaceCollectionsOnPatch`` on the
attachment entry; it could not be tried on TEST, whose Service Layer has no
attachments folder (-5002), so SAP's answer and the read-back decide.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests
from django.db import transaction
from django.utils import timezone

from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPOutcomeUnknown,
    SAPUnavailable,
    SAPValidationError,
)
from sap_postings.services import Outcome

from .models import (
    GRPOPosting,
    GRPOSapEdit,
    GRPOSapEditAttachment,
    GRPOSapEditStatus,
    ServiceGRPOPosting,
)

logger = logging.getLogger(__name__)

KIND = "grpo.edit"

MATERIAL = "I"
SERVICE = "S"

PERMISSION_BY_TYPE = {
    MATERIAL: "grpo.can_edit_grpo_in_sap",
    SERVICE: "grpo.can_edit_service_grpo_in_sap",
}

#: Amounts are compared to the paisa: SAP stores six decimals, people type two.
AMOUNT_TOLERANCE = Decimal("0.005")

#: The stamp the app writes into the remarks of a GRPO it posts. A/P invoice
#: drafts find the GRPO's gate entry by it (``ap_invoice_draft.services``), so
#: an edit may rewrite the remarks around it but not drop it.
GATE_ENTRY_STAMP = re.compile(r"Gate Entry:\s*\S+")


LAST_FILE_MESSAGE = (
    "A GRPO must keep at least one attachment: SAP refuses a GRPO without one "
    "(\"Please Attach its Receiving\"). To replace the last file, add the new one "
    "in the same save."
)


def same_file(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def shared_message(shared_with: list) -> str:
    others = ", ".join(
        f"{'cancelled ' if other['cancelled'] else ''}{other['document']} {other['doc_num']}"
        for other in shared_with[:3]
    )
    return (
        f"This GRPO's attachments are shared with {others}: a file removed here would "
        "disappear there too. Remove it in SAP itself."
    )


class EditError(Exception):
    """The edit cannot be made as asked. The message says why, for the person."""


class EditConflict(EditError):
    """SAP no longer holds what the person was looking at."""


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    sap: str  # the Service Layer property
    type: str  # text | date | integer | number | choice
    max_length: Optional[int] = None


#: The header fields every GRPO offers. User-defined ones are added per company
#: from SAP's own definitions (``HanaGRPOEditReader.header_udfs``).
HEADER_FIELDS = (
    Field("vendor_ref", "Vendor Ref. No.", "NumAtCard", "text", 100),
    Field("document_date", "Document Date", "TaxDate", "date"),
    Field("delivery_date", "Delivery Date", "DocDueDate", "date"),
    Field("contact_person", "Contact Person", "ContactPersonCode", "choice"),
    Field("remarks", "Remarks", "Comments", "text", 254),
)

#: Service lines only, and only while the line is open.
LINE_FIELDS = (
    Field("amount", "Amount", "UnitPrice", "number"),
    Field("month", "Effective Month", "CostingCode2", "choice"),
)

#: Each company's back-date rule opens on the 1st of the previous month until
#: this day of the month, and on the 1st of the current month after it. Read
#: from each company's SBO_SP_TRANSACTIONNOTIFICATION ("min_allowed_date").
BACK_DATE_CUTOFF_DAY = {"JIVO_OIL": 18, "JIVO_BEVERAGES": 16, "JIVO_MART": 16}


def reader_for(company_code):
    from sap_client.context import CompanyContext
    from sap_client.hana.grpo_edit_reader import HanaGRPOEditReader

    return HanaGRPOEditReader(CompanyContext(company_code))


def back_date_floor(company_code: str, today: Optional[date] = None) -> Optional[date]:
    """The earliest posting date SAP lets B1i change a GRPO of, as of ``today``."""
    cutoff = BACK_DATE_CUTOFF_DAY.get((company_code or "").upper())
    if cutoff is None:
        return None
    today = today or timezone.localdate()
    first = today.replace(day=1)
    if today.day > cutoff:
        return first
    return (first.replace(year=first.year - 1, month=12) if first.month == 1
            else first.replace(month=first.month - 1))


def header_fields(udfs: list) -> list:
    """The header fields this company's GRPO offers, standard ones first."""
    return list(HEADER_FIELDS) + [
        Field(udf["column"], udf["label"], udf["column"], udf["type"], udf["max_length"])
        for udf in udfs
    ]


# ---------------------------------------------------------------------------
# values: what SAP holds, what was typed, compared alike
# ---------------------------------------------------------------------------

def current_header_value(document: dict, field: Field):
    if field.key.startswith("U_"):
        return document["udfs"].get(field.key)
    return document.get(field.key)


def current_line_value(line: dict, field: Field):
    return line.get(field.key)


def normalize(field: Field, value):
    """One spelling per value, so SAP's and the person's can be compared.

    Text is stripped, with nothing meaning empty; dates are ISO strings; the
    contact person is an int or None; amounts are two-decimal strings.
    Raises :class:`EditError` for a value SAP could not take.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "" if field.type == "text" else None
    if field.type == "text":
        text = str(value).strip()
        if field.max_length and len(text) > field.max_length:
            raise EditError(f"{field.label} takes at most {field.max_length} characters.")
        return text
    if field.type == "date":
        if isinstance(value, date):
            return value.isoformat()
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError:
            raise EditError(f"{field.label} is not a date.")
    if field.type in ("integer", "choice") and field.key != "month":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise EditError(f"{field.label} is not valid.")
    if field.key == "month":
        return str(value).strip()
    try:
        number = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise EditError(f"{field.label} is not a number.")
    return str(number)


def same(field: Field, a, b) -> bool:
    if field.type == "number" and a is not None and b is not None:
        return abs(Decimal(a) - Decimal(b)) <= AMOUNT_TOLERANCE
    return a == b


def _display(value) -> str:
    return "(empty)" if value in (None, "") else str(value)


# ---------------------------------------------------------------------------
# building an edit from what the person sent
# ---------------------------------------------------------------------------

def editable_reason(document: dict) -> str:
    """Why nothing on this GRPO can be edited, or "" when it can."""
    if document["cancelled"]:
        return "This GRPO is cancelled in SAP, so it cannot be changed."
    return ""


def line_closed_reason(line: dict) -> str:
    if line["line_status"] == "O":
        return ""
    if line["ap_invoice_doc_num"]:
        return f"Closed by A/P invoice {line['ap_invoice_doc_num']}."
    return "Closed in SAP."


def build_changes(
    document: dict,
    udfs: list,
    data: dict,
    *,
    month_codes=None,
    new_file_names=(),
    shared_with=(),
) -> dict:
    """The changes the person asked for, checked against SAP's current values.

    ``data`` is ``{"header": {key: {"before", "after"}}, "lines": [{"line_num",
    "field", "before", "after"}]}``: ``before`` is what their screen showed.
    A field whose ``after`` equals SAP's value is dropped; one whose ``before``
    no longer equals it means someone changed the GRPO since, and the whole edit
    is refused rather than overwriting that.

    ``data["remove_attachments"]`` names files of the GRPO's attachment entry to
    remove (``[{"filename"}]``). ``new_file_names`` are the files added in the
    same edit; ``shared_with`` the other documents on the same attachment entry.
    """
    reason = editable_reason(document)
    if reason:
        raise EditError(reason)

    fields = {field.key: field for field in header_fields(udfs)}
    header = {}
    for key, change in (data.get("header") or {}).items():
        field = fields.get(key)
        if field is None:
            raise EditError(f"'{key}' cannot be changed on a GRPO from here.")
        change = change or {}
        after = normalize(field, change.get("after"))
        before = normalize(field, change.get("before"))
        current = normalize(field, current_header_value(document, field))
        if same(field, after, current):
            continue
        if not same(field, before, current):
            raise EditConflict(
                f"{field.label} was changed in SAP since this page was opened: it is now "
                f"{_display(current)}. Reload the GRPO and make the change again."
            )
        if key == "remarks":
            stamp = GATE_ENTRY_STAMP.search(current or "")
            if stamp and stamp.group(0) not in (after or ""):
                raise EditError(
                    f"Keep \"{stamp.group(0)}\" in the remarks: the app finds this GRPO's "
                    "gate entry by it, for its A/P invoice draft."
                )
        header[key] = {"label": field.label, "before": current, "after": after}

    line_fields = {field.key: field for field in LINE_FIELDS}
    lines_by_num = {line["line_num"]: line for line in document["lines"]}
    lines = []
    for change in data.get("lines") or []:
        if document["doc_type"] != SERVICE:
            raise EditError(
                "A material GRPO's lines are fixed in SAP once it is posted: items, "
                "quantities and prices cannot change."
            )
        field = line_fields.get(change.get("field"))
        if field is None:
            raise EditError(f"'{change.get('field')}' cannot be changed on a GRPO line.")
        try:
            line = lines_by_num[int(change.get("line_num"))]
        except (KeyError, TypeError, ValueError):
            raise EditError(f"The GRPO has no line {change.get('line_num')}.")
        label = f"{field.label}, line {line['line_num'] + 1}"
        after = normalize(field, change.get("after"))
        before = normalize(field, change.get("before"))
        current = normalize(field, current_line_value(line, field))
        if same(field, after, current):
            continue
        closed = line_closed_reason(line)
        if closed:
            raise EditError(f"Line {line['line_num'] + 1} cannot be changed. {closed}")
        if not same(field, before, current):
            raise EditConflict(
                f"{label} was changed in SAP since this page was opened: it is now "
                f"{_display(current)}. Reload the GRPO and make the change again."
            )
        if field.key == "amount" and (after is None or Decimal(after) <= 0):
            raise EditError(f"{label} must be more than 0.")
        if field.key == "month":
            if not after:
                raise EditError(f"{label} cannot be left empty.")
            if month_codes is not None and after not in month_codes:
                raise EditError(f"{label}: SAP has no active month {after}.")
        lines.append(
            {
                "line_num": line["line_num"],
                "field": field.key,
                "label": label,
                "before": current,
                "after": after,
            }
        )

    removals = build_removals(document, data.get("remove_attachments") or [], new_file_names)
    if removals and shared_with:
        raise EditError(shared_message(list(shared_with)))

    return {"header": header, "lines": lines, "attachments_removed": removals}


def build_removals(document: dict, requested: list, new_file_names=()) -> list:
    """The files to take off the GRPO, checked against its attachments in SAP."""
    removals = []
    for item in requested:
        name = str((item or {}).get("filename") or "").strip()
        if not name or any(same_file(r["filename"], name) for r in removals):
            continue
        if not document["attachment_entry"]:
            raise EditError("This GRPO has no attachments in SAP to remove.")
        matches = [a for a in document["attachments"] if same_file(a["filename"], name)]
        if not matches:
            raise EditConflict(
                f"{name} is no longer attached to this GRPO in SAP. Reload the GRPO."
            )
        if len(matches) > 1:
            raise EditError(
                f"SAP holds {name} on this GRPO more than once, so the app cannot tell "
                "which to remove. Remove it in SAP itself."
            )
        if any(same_file(new, name) for new in new_file_names):
            raise EditError(
                f"A new file is also called {name}. Rename it before adding it, so it "
                "cannot be mistaken for the one being removed."
            )
        removals.append({"filename": matches[0]["filename"],
                         "label": f"Removed {matches[0]['filename']}"})
    if removals:
        remaining = len(document["attachments"]) - len(removals) + len(list(new_file_names))
        if remaining < 1:
            raise EditError(LAST_FILE_MESSAGE)
    return removals


def has_changes(changes: dict) -> bool:
    return bool(
        changes.get("header") or changes.get("lines") or changes.get("attachments_removed")
    )


def summary(changes: dict, attachment_count: int = 0) -> str:
    labels = [change["label"] for change in changes.get("header", {}).values()]
    labels += [change["label"] for change in changes.get("lines", [])]
    if attachment_count:
        labels.append(
            f"added {attachment_count} attachment{'s' if attachment_count > 1 else ''}"
        )
    labels += [change["label"] for change in changes.get("attachments_removed", [])]
    return ", ".join(labels)


def title_for(edit: GRPOSapEdit) -> str:
    return f"Edit GRPO {edit.doc_num}"


#: Where each type's edit screen lives: material GRPOs with the warehouse's
#: GRPO pages, service GRPOs with dispatch's Service GRPO pages -- the people who
#: book transporters' bilties do not have the warehouse menu.
EDIT_PAGE_BY_TYPE = {
    MATERIAL: "/warehouse/grpo/sap-edit",
    SERVICE: "/dispatch/bilty-grpo/sap-edit",
}


def edit_page(doc_type: str, doc_entry: int) -> str:
    return f"{EDIT_PAGE_BY_TYPE.get(doc_type, EDIT_PAGE_BY_TYPE[MATERIAL])}/{doc_entry}"


def link_for(edit: GRPOSapEdit) -> str:
    return edit_page(edit.doc_type, edit.doc_entry)


def pending_edit(company, doc_entry) -> Optional[GRPOSapEdit]:
    """An earlier edit of this GRPO that is still waiting for SAP."""
    return (
        GRPOSapEdit.objects.filter(
            company=company, doc_entry=doc_entry, status=GRPOSapEditStatus.WAITING
        )
        .order_by("id")
        .first()
    )


# ---------------------------------------------------------------------------
# sending it
# ---------------------------------------------------------------------------

def payload_for(changes: dict, document: dict, fields: dict) -> dict:
    """The PATCH body for the changes SAP does not hold yet."""
    payload = {}
    for key, change in changes.get("header", {}).items():
        field = fields[key]
        current = normalize(field, current_header_value(document, field))
        if same(field, current, change["after"]):
            continue
        value = change["after"]
        payload[field.sap] = None if value == "" and key.startswith("U_") else value
    by_line = {}
    lines_by_num = {line["line_num"]: line for line in document["lines"]}
    line_fields = {field.key: field for field in LINE_FIELDS}
    for change in changes.get("lines", []):
        field = line_fields[change["field"]]
        line = lines_by_num.get(change["line_num"])
        if line is None:
            raise EditError(f"The GRPO no longer has line {change['line_num'] + 1}.")
        if same(field, normalize(field, current_line_value(line, field)), change["after"]):
            continue
        body = by_line.setdefault(change["line_num"], {"LineNum": change["line_num"]})
        if field.key == "amount":
            body["UnitPrice"] = float(Decimal(change["after"]))
            body["DiscountPercent"] = 0
        else:
            body[field.sap] = change["after"]
    if by_line:
        payload["DocumentLines"] = [by_line[num] for num in sorted(by_line)]
    return payload


def check_still_applies(changes: dict, document: dict, fields: dict) -> None:
    """At send time: every change must find SAP holding either what the person saw
    or what they asked for. Anything else was changed in SAP meanwhile."""
    reason = editable_reason(document)
    if reason:
        raise EditError(reason)
    for key, change in changes.get("header", {}).items():
        field = fields.get(key)
        if field is None:
            raise EditError(f"{change['label']} no longer exists on this company's GRPO.")
        current = normalize(field, current_header_value(document, field))
        if not (same(field, current, change["before"]) or same(field, current, change["after"])):
            raise EditConflict(
                f"{change['label']} was changed in SAP to {_display(current)} after this "
                "edit was made, so nothing was sent. Open the GRPO again and redo the edit."
            )
    for removal in changes.get("attachments_removed", []):
        held = [a for a in document["attachments"] if same_file(a["filename"], removal["filename"])]
        if len(held) > 1:
            raise EditError(
                f"SAP now holds {removal['filename']} on this GRPO more than once, so "
                "nothing was sent. Remove it in SAP itself."
            )
    lines_by_num = {line["line_num"]: line for line in document["lines"]}
    line_fields = {field.key: field for field in LINE_FIELDS}
    for change in changes.get("lines", []):
        field = line_fields[change["field"]]
        line = lines_by_num.get(change["line_num"])
        if line is None:
            raise EditError(f"The GRPO no longer has line {change['line_num'] + 1}.")
        current = normalize(field, current_line_value(line, field))
        if same(field, current, change["after"]):
            continue
        closed = line_closed_reason(line)
        if closed:
            raise EditError(f"Line {change['line_num'] + 1} cannot be changed any more. {closed}")
        if not same(field, current, change["before"]):
            raise EditConflict(
                f"{change['label']} was changed in SAP to {_display(current)} after this "
                "edit was made, so nothing was sent. Open the GRPO again and redo the edit."
            )


def not_saved(changes: dict, document: dict, fields: dict) -> list:
    """The changes SAP does not hold after the PATCH -- taken and then dropped."""
    missing = []
    for key, change in changes.get("header", {}).items():
        field = fields[key]
        if not same(field, normalize(field, current_header_value(document, field)), change["after"]):
            missing.append(change["label"])
    lines_by_num = {line["line_num"]: line for line in document["lines"]}
    line_fields = {field.key: field for field in LINE_FIELDS}
    for change in changes.get("lines", []):
        field = line_fields[change["field"]]
        line = lines_by_num.get(change["line_num"]) or {}
        if not same(field, normalize(field, current_line_value(line, field)), change["after"]):
            missing.append(change["label"])
    return missing


def explain_refusal(message: str, company_code: str) -> str:
    """SAP's refusal, with what to do about the one people will meet most."""
    if "Back Date Entry not allowed" not in message:
        return message
    floor = back_date_floor(company_code)
    since = f" posted on or after {floor:%d %b %Y}" if floor else ""
    return (
        f"{message}. SAP lets the app change only GRPOs{since}. Someone SAP allows to "
        "back-date (Gurcharan or manager) can still change this one in SAP itself."
    )


def _record(edit, status, message, *, posted=False):
    edit.status = status
    edit.message = (message or "")[:4000]
    fields = ["status", "message", "updated_at"]
    if posted:
        edit.posted_at = timezone.now()
        fields.append("posted_at")
    edit.save(update_fields=fields)


def _sap_message(response):
    try:
        error = response.json().get("error", {})
        message = error.get("message")
        if isinstance(message, dict):
            message = message.get("value")
        return str(message or response.text)[:500]
    except Exception:  # noqa: BLE001
        return f"HTTP {response.status_code}: {response.text[:300]}"


def _sl_call(company_code, method, path, payload=None, headers=None):
    """One Service Layer call, with SAP not answering told apart from SAP refusing."""
    from sap_client import health
    from sap_client.context import CompanyContext
    from sap_client.service_layer.auth import ServiceLayerSession

    sl = CompanyContext(company_code).service_layer
    health.guard_service_layer()
    try:
        cookies = ServiceLayerSession(sl).login()
    except requests.RequestException as exc:
        raise SAPUnavailable(f"SAP Service Layer did not answer the login: {exc}") from exc
    try:
        response = requests.request(
            method,
            f"{sl['base_url']}/b1s/v2/{path}",
            json=payload,
            headers=headers,
            cookies=cookies,
            timeout=(10, 180),
            verify=False,
        )
    except requests.ConnectTimeout as exc:
        raise SAPUnavailable(f"SAP Service Layer did not answer: {exc}") from exc
    except requests.RequestException as exc:
        raise SAPOutcomeUnknown(
            f"SAP did not answer in time ({exc}). It may still have saved the change; "
            "the next try reads SAP first."
        ) from exc
    if response.status_code in (502, 503, 504):
        raise SAPUnavailable(f"SAP Service Layer is not available ({response.status_code}).")
    if response.status_code not in (200, 201, 204):
        raise SAPValidationError(_sap_message(response))
    return response.json() if response.content else {}


def patch_grpo(company_code, doc_entry, payload):
    _sl_call(company_code, "PATCH", f"PurchaseDeliveryNotes({int(doc_entry)})", payload)


def _line_filename(line: dict) -> str:
    name, extension = line.get("FileName") or "", line.get("FileExtension") or ""
    return f"{name}.{extension}" if extension else name


def remove_attachment_lines(company_code, absolute_entry, filenames):
    """Take files off an attachment entry: its other lines, sent back as SAP
    gave them, replace the collection. The file itself stays in SAP's folder."""
    entry = _sl_call(company_code, "GET", f"Attachments2({int(absolute_entry)})")
    lines = entry.get("Attachments2_Lines") or []
    keep = [
        {key: value for key, value in line.items() if not key.startswith(("odata", "@"))}
        for line in lines
        if not any(same_file(_line_filename(line), name) for name in filenames)
    ]
    if len(keep) == len(lines):
        return
    if not keep:
        raise EditError(LAST_FILE_MESSAGE)
    _sl_call(
        company_code,
        "PATCH",
        f"Attachments2({int(absolute_entry)})",
        {"Attachments2_Lines": keep},
        headers={"B1S-ReplaceCollectionsOnPatch": "true"},
    )


def attach_files(edit, document):
    """The edit's new files, as lines of the GRPO's SAP attachment entry.

    Returns the attachment entry to link when the GRPO had none, else None.
    Each file is uploaded once: its entry is stored the moment SAP gives it.
    """
    from sap_client.client import SAPClient

    pending = [a for a in edit.attachments.all() if a.sap_absolute_entry is None]
    existing = document["attachment_entry"] or edit.sap_attachment_entry
    if not pending:
        return None if document["attachment_entry"] else edit.sap_attachment_entry
    client = SAPClient(edit.company.code)
    for attachment in pending:
        filename = attachment.original_filename
        if existing:
            client.add_line_to_existing_attachment(
                absolute_entry=existing,
                file_path=attachment.file.path,
                filename=filename,
                allow_metadata_fallback=True,
            )
        else:
            existing = client.upload_attachment(
                file_path=attachment.file.path,
                filename=filename,
                allow_metadata_fallback=True,
            ).get("AbsoluteEntry")
            if not existing:
                raise SAPDataError("SAP did not say where it kept the attachment.")
            existing = int(existing)
            edit.sap_attachment_entry = existing
            edit.save(update_fields=["sap_attachment_entry", "updated_at"])
        attachment.sap_absolute_entry = existing
        attachment.save(update_fields=["sap_absolute_entry"])
    return None if document["attachment_entry"] else existing


def sync_app_records(edit, document):
    """The app's own copies of this GRPO follow SAP's new total."""
    total = document.get("doc_total")
    if total is None:
        return
    GRPOPosting.objects.filter(
        sap_doc_entry=edit.doc_entry, vehicle_entry__company=edit.company
    ).update(sap_doc_total=total)
    ServiceGRPOPosting.objects.filter(
        sap_doc_entry=edit.doc_entry, dispatch_plan__company=edit.company
    ).update(sap_doc_total=total)


class GRPOEditHandler:
    """Sends one :class:`GRPOSapEdit`. Safe to send again: SAP is read first, and
    a change SAP already holds is not sent twice."""

    kind = KIND

    def send(self, posting):
        edit = (
            GRPOSapEdit.objects.select_related("company")
            .filter(pk=posting.source_id)
            .first()
        )
        if edit is None:
            return Outcome.rejected("The GRPO edit no longer exists in the app.")
        if edit.status == GRPOSapEditStatus.POSTED:
            return Outcome.posted(edit.message, result={"doc_nums": [str(edit.doc_num)]})
        try:
            return self._send(edit)
        except (SAPConnectionError, requests.RequestException) as exc:
            message = f"SAP could not be reached ({exc}); the GRPO is not changed yet."
            _record(edit, GRPOSapEditStatus.WAITING, message)
            return Outcome.waiting(message)
        except SAPDataError as exc:
            from sap_mirror.services import hana_unreachable

            if hana_unreachable(exc):
                message = f"SAP could not be read ({exc}); the GRPO is not changed yet."
                _record(edit, GRPOSapEditStatus.WAITING, message)
                return Outcome.waiting(message)
            _record(edit, GRPOSapEditStatus.REFUSED, str(exc))
            return Outcome.rejected(str(exc))
        except (SAPValidationError, EditError) as exc:
            message = explain_refusal(str(exc), edit.company.code)
            _record(edit, GRPOSapEditStatus.REFUSED, message)
            return Outcome.rejected(message)
        except OSError as exc:
            message = f"An attachment could not be read ({exc}); add it again."
            _record(edit, GRPOSapEditStatus.REFUSED, message)
            return Outcome.rejected(message)

    def _send(self, edit):
        reader = reader_for(edit.company.code)
        udfs = reader.header_udfs()
        fields = {field.key: field for field in header_fields(udfs)}
        document = reader.document(edit.doc_entry, udfs)
        if document is None:
            raise EditError(f"GRPO {edit.doc_num} is not in SAP for {edit.company.name}.")

        check_still_applies(edit.changes, document, fields)
        removals = [r["filename"] for r in edit.changes.get("attachments_removed", [])]
        to_remove = [
            name for name in removals
            if any(same_file(a["filename"], name) for a in document["attachments"])
        ]
        if to_remove:
            shared = reader.attachment_shared_with(document["attachment_entry"], edit.doc_entry)
            if shared:
                raise EditError(shared_message(shared))
        payload = payload_for(edit.changes, document, fields)
        # New files first, so a GRPO replacing its only file is never left with none.
        link_entry = attach_files(edit, document)
        if to_remove:
            held = reader.attachments(document["attachment_entry"])
            if len(held) - len(to_remove) < 1:
                raise EditError(LAST_FILE_MESSAGE)
            remove_attachment_lines(edit.company.code, document["attachment_entry"], to_remove)
        if link_entry:
            payload["AttachmentEntry"] = link_entry
        if payload:
            patch_grpo(edit.company.code, edit.doc_entry, payload)
        if payload or to_remove:
            document = reader.document(edit.doc_entry, udfs)

        missing = not_saved(edit.changes, document, fields)
        if link_entry and document.get("attachment_entry") != link_entry:
            missing.append("Attachments")
        still_attached = [
            name for name in removals
            if any(same_file(a["filename"], name) for a in document["attachments"])
        ]
        if still_attached:
            missing.append(f"the removal of {', '.join(still_attached)}")
        if missing:
            raise EditError(
                "SAP took the update but did not save "
                f"{', '.join(missing)}. Nothing else was changed for those; "
                "SAP keeps them fixed on this GRPO."
            )

        sync_app_records(edit, document)
        what = summary(edit.changes, edit.attachments.count())
        message = f"GRPO {edit.doc_num} updated in SAP: {what}." if what else (
            f"GRPO {edit.doc_num} already held these values."
        )
        _record(edit, GRPOSapEditStatus.POSTED, message, posted=True)
        return Outcome.posted(
            message,
            result={"doc_nums": [str(edit.doc_num)], "edit_id": edit.id},
            detail={"doc_num": edit.doc_num, "payload": payload},
            link=link_for(edit),
        )


# ---------------------------------------------------------------------------
# the person's request
# ---------------------------------------------------------------------------

def submit(*, company, document, udfs, data, files, user, month_codes=None):
    """Record the edit and send it now. Returns ``(edit, outcome)``.

    Raises :class:`EditError` (or :class:`EditConflict`) before anything is
    recorded when the edit cannot be made as asked.
    """
    from sap_postings import services as sap_postings
    from .services import _allocate_grpo_document

    files = list(files or [])
    shared_with = ()
    if (data.get("remove_attachments") or []) and document["attachment_entry"]:
        shared_with = reader_for(company.code).attachment_shared_with(
            document["attachment_entry"], document["doc_entry"]
        )
    changes = build_changes(
        document,
        udfs,
        data,
        month_codes=month_codes,
        new_file_names=[upload.name for upload in files],
        shared_with=shared_with,
    )
    if not has_changes(changes) and not files:
        raise EditError("Nothing was changed.")
    earlier = pending_edit(company, document["doc_entry"])
    if earlier:
        raise EditConflict(
            f"An earlier edit of GRPO {document['doc_num']} is still waiting for SAP. "
            "Let it go through, or cancel it on SAP Postings, before making another."
        )

    with transaction.atomic():
        edit = GRPOSapEdit.objects.create(
            company=company,
            doc_entry=document["doc_entry"],
            doc_num=document["doc_num"],
            doc_type=document["doc_type"],
            card_code=document["card_code"][:50],
            card_name=document["card_name"][:150],
            changes=changes,
            requested_by=user,
        )
        for upload in files:
            GRPOSapEditAttachment.objects.create(
                edit=edit,
                file=upload,
                original_filename=upload.name[:255],
                document_code=_allocate_grpo_document(
                    upload=upload, filename=upload.name, user=user, count_pages=True
                ),
            )

    _posting, outcome = sap_postings.post_now(
        kind=KIND,
        company=company,
        source_id=edit.pk,
        title=title_for(edit),
        link=link_for(edit),
        user=user,
    )
    edit.refresh_from_db()
    return edit, outcome
