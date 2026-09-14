"""
Every write to the artwork register goes through here.

Two things live in this module rather than in the view. The first is the merge
that answers "for every item": SAP's list of label and carton items, left-joined
onto what the register holds, so an item with no artwork on file is a visible
row marked PENDING instead of an absence nobody notices. The second is the
revision rule -- a change always snapshots what it is about to overwrite.
"""

import logging
import os
from typing import Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from sap_client.exceptions import SAPConnectionError, SAPDataError

from .constants import (
    ARTWORK_SUB_GROUPS,
    CDR_EXTENSIONS,
    MAX_CDR_BYTES,
    MAX_PDF_BYTES,
    PDF_CONTENT_TYPES,
    PDF_EXTENSIONS,
)
from .hana_reader import ArtworkItemReader
from .models import ArtworkRecord

logger = logging.getLogger(__name__)

#: Capture status of one item, as the page filters on it.
STATUS_CAPTURED = "CAPTURED"
STATUS_PENDING = "PENDING"


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

def _human_mb(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.0f} MB"


def validate_pdf(upload) -> None:
    """Refuse anything that is not a PDF, or is too big to be one."""
    if upload.size > MAX_PDF_BYTES:
        raise ValidationError(
            f"PDF is too large ({_human_mb(upload.size)}). "
            f"The limit is {_human_mb(MAX_PDF_BYTES)}."
        )
    extension = os.path.splitext(upload.name or "")[1].lower()
    content_type = (getattr(upload, "content_type", "") or "").lower()
    # A correct extension is accepted on its own: browsers are inconsistent
    # about the MIME type they attach to a dragged or pasted file.
    if extension not in PDF_EXTENSIONS and content_type not in PDF_CONTENT_TYPES:
        raise ValidationError("The artwork file must be a PDF.")


def validate_cdr(upload) -> None:
    """Refuse anything that is not a .cdr, or is too big to be one.

    The extension is the only check that means anything here. CorelDRAW has no
    registered media type, so browsers send ``application/octet-stream`` (or
    an empty string) for a perfectly valid .cdr -- testing the content type
    would reject the real files and accept nothing extra.
    """
    if upload.size > MAX_CDR_BYTES:
        raise ValidationError(
            f"CDR is too large ({_human_mb(upload.size)}). "
            f"The limit is {_human_mb(MAX_CDR_BYTES)}."
        )
    extension = os.path.splitext(upload.name or "")[1].lower()
    if extension not in CDR_EXTENSIONS:
        raise ValidationError("The source file must be a CorelDRAW .cdr file.")


def _attach_pdf(record: ArtworkRecord, upload) -> None:
    validate_pdf(upload)
    record.pdf_file = upload
    record.pdf_original_name = upload.name or ""
    record.pdf_size = upload.size


def _attach_cdr(record: ArtworkRecord, upload) -> None:
    validate_cdr(upload)
    record.cdr_file = upload
    record.cdr_original_name = upload.name or ""
    record.cdr_size = upload.size


# ---------------------------------------------------------------------------
# Reading the register
# ---------------------------------------------------------------------------

def list_records(
    *,
    company,
    sub_group: str = "",
    search: str = "",
    include_inactive: bool = False,
):
    """The artwork on file for one company, filtered the way the page filters."""
    rows = ArtworkRecord.objects.filter(company=company).select_related(
        "company", "created_by", "updated_by"
    )
    if not include_inactive:
        rows = rows.filter(is_active=True)

    kind = (sub_group or "").strip().upper()
    if kind:
        rows = rows.filter(sub_group=kind)

    term = (search or "").strip()
    if term:
        rows = rows.filter(
            Q(item_code__icontains=term)
            | Q(item_name__icontains=term)
            | Q(document_number__icontains=term)
            | Q(barcode__icontains=term)
        )
    return rows.order_by("sub_group", "item_code")


def list_items(
    *,
    company,
    sub_group: str = "",
    search: str = "",
    status: str = "",
) -> Dict:
    """Every label and carton item, with its artwork record if it has one.

    This is the page's main list. SAP supplies the universe of items and the
    register supplies what has been captured, so an item nobody has filed
    artwork for appears as a PENDING row rather than not appearing at all.

    SAP being unreachable does not empty the screen: the captured records are
    still returned, flagged ``sap_available = False``, because artwork already
    on file stays readable when the item master cannot be reached. What is lost
    is only the ability to see the gaps, and the response says so.
    """
    kind = (sub_group or "").strip().upper()
    if kind and kind not in ARTWORK_SUB_GROUPS:
        raise ValidationError(
            {"sub_group": f"Must be one of {', '.join(ARTWORK_SUB_GROUPS)}."}
        )

    wanted = (status or "").strip().upper()
    if wanted and wanted not in (STATUS_CAPTURED, STATUS_PENDING):
        raise ValidationError(
            {"status": f"Must be {STATUS_CAPTURED} or {STATUS_PENDING}."}
        )

    records = {
        record.item_code.upper(): record
        for record in list_records(company=company, sub_group=kind)
    }

    # The whole item list, unsearched. The search is applied to the merged rows
    # further down rather than pushed into the SQL, because half the fields it
    # has to match -- document number, barcode -- exist only in the register.
    # Narrowing SAP first would make an item look retired from the item master
    # merely because its name did not contain what was typed. The volume makes
    # this affordable: a few hundred rows per company (see constants).
    sap_items: List[Dict] = []
    sap_available = True
    sap_error = ""
    try:
        sap_items = ArtworkItemReader(company.code).list_artwork_items(
            sub_group=kind or None
        )
    except (SAPConnectionError, SAPDataError) as exc:
        sap_available = False
        sap_error = str(exc)
        logger.warning("[Artwork] item master unavailable for %s: %s", company.code, exc)

    rows: List[Dict] = []
    seen = set()

    for item in sap_items:
        code = item["item_code"].upper()
        seen.add(code)
        rows.append(_merge_row(item, records.get(code)))

    # Records whose item the SAP list does not carry -- deactivated in the item
    # master, or renamed out of the sub-group. Shown rather than dropped:
    # artwork filed against a since-retired item is exactly what somebody comes
    # here looking for. Flagged, so the row says why it looks odd.
    for code, record in records.items():
        if code in seen:
            continue
        rows.append(
            _merge_row(
                {
                    "item_code": record.item_code,
                    "item_name": record.item_name,
                    "sub_group": record.sub_group,
                    "uom": "",
                },
                record,
                # Only a claim worth making when SAP actually answered. During
                # an outage nothing is known about the item master, and saying
                # "not in SAP any more" would be a guess dressed up as a fact.
                in_sap=not sap_available,
            )
        )

    term = (search or "").strip().upper()
    if term:
        rows = [row for row in rows if _row_matches(row, term)]
    if wanted:
        rows = [row for row in rows if row["status"] == wanted]

    rows.sort(key=lambda row: (row["sub_group"], row["item_code"]))

    captured = sum(1 for row in rows if row["status"] == STATUS_CAPTURED)
    return {
        "sap_available": sap_available,
        "sap_error": sap_error,
        "summary": {
            "total": len(rows),
            "captured": captured,
            "pending": len(rows) - captured,
        },
        "rows": rows,
    }


def _row_matches(row: Dict, term: str) -> bool:
    """Search one merged row across both halves of it.

    The item code and name come from SAP, the document number and barcode from
    the register, and somebody looking for an artwork will type whichever of
    the four they happen to have in front of them.
    """
    haystack = " ".join(
        [
            row["item_code"],
            row["item_name"],
            row["document_number"],
            row["barcode"],
        ]
    ).upper()
    return term in haystack


def _merge_row(item: Dict, record: Optional[ArtworkRecord], in_sap: bool = True) -> Dict:
    """One item row: what SAP says about the item, plus what is on file."""
    return {
        "item_code": item["item_code"],
        # SAP's current name wins over the snapshot -- the snapshot exists so a
        # record can still name itself when SAP is down, not to freeze a rename.
        "item_name": item.get("item_name") or (record.item_name if record else ""),
        "sub_group": item.get("sub_group") or (record.sub_group if record else ""),
        "uom": item.get("uom", ""),
        "in_sap": in_sap,
        "status": STATUS_CAPTURED if record else STATUS_PENDING,
        "record_id": record.id if record else None,
        "document_number": record.document_number if record else "",
        "revision_number": record.revision_number if record else None,
        "revision_label": record.revision_label if record else "",
        "revision_date": record.revision_date if record else None,
        "barcode": record.barcode if record else "",
        "has_pdf": bool(record and record.pdf_file),
        "has_cdr": bool(record and record.cdr_file),
        "updated_at": record.updated_at if record else None,
    }


# ---------------------------------------------------------------------------
# Writing the register
# ---------------------------------------------------------------------------

def _resolve_sap_item(company, item_code: str) -> Optional[Dict]:
    """The item as SAP has it, or ``None`` if SAP could not be asked.

    A code SAP *does* answer for but which is not a label or a carton is
    refused here. A code SAP cannot be asked about at all is allowed through,
    with a warning logged: blocking every capture for the length of a HANA
    outage would be a worse failure than accepting a code that is checked
    against the item list the capturer picked it from anyway.
    """
    try:
        item = ArtworkItemReader(company.code).get_item(item_code)
    except (SAPConnectionError, SAPDataError) as exc:
        logger.warning(
            "[Artwork] could not verify %s against SAP (%s): %s",
            item_code,
            company.code,
            exc,
        )
        return None

    if item is None:
        raise ValidationError(
            {
                "item_code": (
                    f"{item_code} is not a label or carton item in "
                    f"{company.code}. Only SAP packaging items whose sub-group "
                    f"is {' or '.join(ARTWORK_SUB_GROUPS)} carry artwork."
                )
            }
        )
    return item


def _assert_document_number_free(company, document_number: str, exclude_pk=None) -> None:
    clash = ArtworkRecord.objects.filter(
        company=company, is_active=True, document_number__iexact=document_number
    )
    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)
    other = clash.first()
    if other is not None:
        raise ValidationError(
            {
                "document_number": (
                    f"Document number {document_number} is already on file for "
                    f"{other.item_code}. A controlled document number "
                    f"identifies one artwork."
                )
            }
        )


@transaction.atomic
def capture(
    *,
    user,
    company,
    item_code: str,
    document_number: str,
    revision_date,
    revision_number: int = 0,
    barcode: str = "",
    remarks: str = "",
    pdf_upload=None,
    cdr_upload=None,
) -> ArtworkRecord:
    """File the artwork for an item that has none yet.

    Both files are mandatory: a record that names a document number without
    holding the artwork is a promise, not a record, and the page exists to
    answer "show me the artwork".
    """
    item_code = (item_code or "").strip()
    document_number = (document_number or "").strip()

    errors = {}
    if not item_code:
        errors["item_code"] = "Pick the item from SAP."
    if not document_number:
        errors["document_number"] = "The document number is required."
    if revision_date is None:
        errors["revision_date"] = "The revision date is required."
    if pdf_upload is None:
        errors["pdf_file"] = "Attach the print-ready PDF."
    if cdr_upload is None:
        errors["cdr_file"] = "Attach the CorelDRAW .cdr source."
    if errors:
        raise ValidationError(errors)

    existing = ArtworkRecord.objects.filter(
        company=company, item_code__iexact=item_code, is_active=True
    ).first()
    if existing is not None:
        raise ValidationError(
            {
                "item_code": (
                    f"{item_code} already has artwork on file "
                    f"({existing.document_number} rev {existing.revision_label}). "
                    f"Revise that record instead of filing a second one."
                )
            }
        )

    _assert_document_number_free(company, document_number)
    item = _resolve_sap_item(company, item_code)
    if item is None:
        # SAP could not be asked, so this item cannot be confirmed as a label
        # or a carton -- and the record has to declare which it is, because the
        # page filters on it. Refused rather than filed under a guess.
        raise ValidationError(
            {
                "item_code": (
                    "SAP is unreachable, so this item cannot be confirmed as a "
                    "label or a carton. Try again once SAP is back; artwork "
                    "already on file is unaffected."
                )
            }
        )

    record = ArtworkRecord(
        company=company,
        item_code=item["item_code"],
        item_name=item["item_name"],
        sub_group=item["sub_group"],
        document_number=document_number,
        revision_number=revision_number or 0,
        revision_date=revision_date,
        barcode=(barcode or "").strip(),
        remarks=(remarks or "").strip(),
        created_by=user,
        updated_by=user,
    )
    _attach_pdf(record, pdf_upload)
    _attach_cdr(record, cdr_upload)
    record.save()
    return record


@transaction.atomic
def revise(
    *,
    user,
    record: ArtworkRecord,
    document_number: Optional[str] = None,
    revision_number: Optional[int] = None,
    revision_date=None,
    barcode: Optional[str] = None,
    remarks: Optional[str] = None,
    pdf_upload=None,
    cdr_upload=None,
) -> ArtworkRecord:
    """Change an artwork record, keeping the state it is replacing.

    Every field is optional -- a correction to a mistyped barcode is as valid a
    reason to come here as a genuinely new revision. What is never optional is
    the history row: it is written before anything changes, so the superseded
    document number, revision and files stay readable for good.

    Files are kept unless a new one is uploaded, which is what allows that
    barcode correction without demanding the artwork be re-attached.
    """
    if document_number is not None:
        document_number = document_number.strip()
        if not document_number:
            raise ValidationError({"document_number": "The document number is required."})
        _assert_document_number_free(record.company, document_number, exclude_pk=record.pk)

    if pdf_upload is not None:
        validate_pdf(pdf_upload)
    if cdr_upload is not None:
        validate_cdr(cdr_upload)

    record.snapshot_revision(user=user)

    if document_number is not None:
        record.document_number = document_number
    if revision_number is not None:
        record.revision_number = revision_number
    if revision_date is not None:
        record.revision_date = revision_date
    if barcode is not None:
        record.barcode = barcode.strip()
    if remarks is not None:
        record.remarks = remarks.strip()
    if pdf_upload is not None:
        _attach_pdf(record, pdf_upload)
    if cdr_upload is not None:
        _attach_cdr(record, cdr_upload)

    record.updated_by = user
    record.save()
    return record


@transaction.atomic
def retire(*, user, record: ArtworkRecord) -> ArtworkRecord:
    """Take an artwork out of the register without erasing it.

    Soft, like every other controlled document in this codebase: the row and
    its whole revision history stay, and the item goes back to PENDING so a
    replacement artwork can be filed against it.
    """
    record.snapshot_revision(user=user)
    record.is_active = False
    record.updated_by = user
    record.save(update_fields=["is_active", "updated_by", "updated_at"])
    return record


def next_revision_number(record: ArtworkRecord) -> int:
    """What the next revision would be numbered, offered as the form default."""
    return record.revision_number + 1


def coverage_summary(*, company) -> Dict:
    """How much of the item master has artwork on file, by kind."""
    result = list_items(company=company)
    by_kind: Dict[str, Dict[str, int]] = {}
    for row in result["rows"]:
        bucket = by_kind.setdefault(
            row["sub_group"] or "UNKNOWN", {"total": 0, "captured": 0, "pending": 0}
        )
        bucket["total"] += 1
        if row["status"] == STATUS_CAPTURED:
            bucket["captured"] += 1
        else:
            bucket["pending"] += 1
    return {
        "as_of": timezone.now(),
        "sap_available": result["sap_available"],
        "overall": result["summary"],
        "by_sub_group": by_kind,
    }
