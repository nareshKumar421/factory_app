"""
Database-backed allocation of controlled-document codes.

This is the ONE place that mints a new code. It guarantees:

* sequential numbering  -- next NN = (max existing NN in the group) + 1,
* uniqueness            -- enforced by the DB unique constraint, with a retry
                           loop to survive concurrent allocations,
* correct persistence   -- revision (00), issue date (today) and page count.

Every module (GATE / QC / GRPO) calls :func:`allocate_for_module`; none of them
re-implement numbering.
"""

from datetime import date as date_cls

from django.db import IntegrityError, transaction
from django.db.models import Max

from . import numbering
from .config import get_module_document_type
from .models import DocumentCode

# How many times to retry when two requests race for the same next serial.
_MAX_ALLOC_RETRIES = 5


def peek_next_serial(section: str, doctype: str, clause: str) -> int:
    """Return the serial that *would* be issued next for a group (no write)."""
    cc, ss, gg = numbering.split_clause(clause)
    current_max = DocumentCode.objects.filter(
        section=section, doctype=doctype, cc=cc, ss=ss, gg=gg
    ).aggregate(m=Max("nn"))["m"]
    return (current_max or 0) + 1


def peek_next_code(section: str, doctype: str, clause: str) -> str:
    """Return the code that *would* be issued next for a group (no write)."""
    return numbering.format_code(
        section, doctype, clause, peek_next_serial(section, doctype, clause)
    )


def _allocate_once(*, section, doctype, cc, ss, gg, clause, revision_number,
                   issue_date, total_pages, module, source_reference, created_by):
    with transaction.atomic():
        # Lock the group's existing rows so a concurrent allocator waits for us
        # instead of reading the same max serial.
        last = (
            DocumentCode.objects.select_for_update()
            .filter(section=section, doctype=doctype, cc=cc, ss=ss, gg=gg)
            .order_by("-nn")
            .first()
        )
        next_nn = (last.nn + 1) if last else 1
        code = numbering.format_code(section, doctype, clause, next_nn)
        return DocumentCode.objects.create(
            code=code,
            section=section,
            doctype=doctype,
            cc=cc,
            ss=ss,
            gg=gg,
            nn=next_nn,
            revision_number=revision_number,
            issue_date=issue_date,
            total_pages=total_pages,
            module=module,
            source_reference=source_reference,
            created_by=created_by,
        )


def allocate_code(
    *,
    section: str,
    doctype: str,
    clause: str,
    revision_number: int = 0,
    issue_date=None,
    total_pages: int = 1,
    module: str = "",
    source_reference: str = "",
    created_by=None,
) -> DocumentCode:
    """Allocate and persist the next controlled-document code for a group.

    Sequential within (SECTION, DOCTYPE, clause). Retries on the unlikely
    race where two callers try the same serial at once (the DB unique
    constraint rejects the loser, and we recompute).
    """
    numbering.validate_parts(section, doctype, clause)
    cc, ss, gg = numbering.split_clause(clause)
    issue_date = issue_date or date_cls.today()
    total_pages = total_pages or 1

    last_error = None
    for _ in range(_MAX_ALLOC_RETRIES):
        try:
            return _allocate_once(
                section=section,
                doctype=doctype,
                cc=cc,
                ss=ss,
                gg=gg,
                clause=clause,
                revision_number=revision_number,
                issue_date=issue_date,
                total_pages=total_pages,
                module=module,
                source_reference=source_reference,
                created_by=created_by,
            )
        except IntegrityError as exc:  # pragma: no cover - race path
            last_error = exc
            continue
    raise RuntimeError(
        f"Could not allocate a document code for {section}-{doctype}-{clause} "
        f"after {_MAX_ALLOC_RETRIES} attempts."
    ) from last_error


def allocate_for_module(module_key: str, **kwargs) -> DocumentCode:
    """Allocate the next code for a mapped application module.

    ``module_key`` is a key in ``config.MODULE_DOCUMENT_TYPES`` (e.g. "GATE",
    "QC", "GRPO"). Extra keyword args (``total_pages``, ``created_by``,
    ``source_reference`` ...) are passed through to :func:`allocate_code`.
    """
    cfg = get_module_document_type(module_key)
    return allocate_code(
        section=cfg["section"],
        doctype=cfg["doctype"],
        clause=cfg["clause"],
        module=module_key,
        **kwargs,
    )
