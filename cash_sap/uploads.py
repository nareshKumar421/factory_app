"""Checking a "posting bill" before it is kept.

The rules are the cash book's own -- the same file types and the same size
ceiling -- read from its public constants so the two can never disagree. The
check itself is written here because ``cash_book.services._check_upload`` is
private and this branch does not add a public twin to ``cash_book``.
"""

import pathlib

from rest_framework.exceptions import ValidationError

from cash_book.services import ATTACHMENT_EXTENSIONS, MAX_ATTACHMENT_BYTES


def check_upload(upload, *, noun: str = "bill"):
    """``(name, size)`` for an acceptable file; ``ValidationError({"file": ...})`` otherwise."""
    if upload is None:
        raise ValidationError({"file": "Choose a file to attach."})
    name = getattr(upload, "name", "") or ""
    suffix = pathlib.Path(name).suffix.lower()
    if suffix not in ATTACHMENT_EXTENSIONS:
        raise ValidationError({
            "file": (
                f"{name or 'That file'} is not a {noun}. Attach a photograph or a "
                f"PDF ({', '.join(sorted(ATTACHMENT_EXTENSIONS))})."
            )
        })
    size = getattr(upload, "size", 0) or 0
    if size <= 0:
        raise ValidationError({"file": f"{name} is empty."})
    if size > MAX_ATTACHMENT_BYTES:
        raise ValidationError({
            "file": (
                f"{name} is {size / 1024 / 1024:.1f} MB. The most that can be "
                f"attached is {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB."
            )
        })
    return name[:255], size
