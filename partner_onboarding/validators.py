"""Checks on what a stranger uploads through the public forms."""

import os

from rest_framework.exceptions import ValidationError

from .constants import ALLOWED_UPLOADS, MAX_ATTACHMENT_BYTES


def upload_extension(name: str) -> str:
    return os.path.splitext(name or "")[1].lower().lstrip(".")


def check_upload(upload, label: str) -> str:
    """Return the content type of an acceptable upload, or raise a ValidationError.

    Acceptable means: at most 15 MB (JI's cap), named .pdf/.jpg/.jpeg/.png, and
    its first bytes really are that kind of file. The browser's claimed type
    is ignored — it is whatever the client says.
    """
    name = getattr(upload, "name", "") or ""
    size = getattr(upload, "size", 0) or 0
    extension = upload_extension(name)
    if extension not in ALLOWED_UPLOADS:
        raise ValidationError({label: f"{name or 'The file'}: only PDF, JPG and PNG files are accepted."})
    if size <= 0:
        raise ValidationError({label: f"{name}: the file is empty."})
    if size > MAX_ATTACHMENT_BYTES:
        raise ValidationError(
            {label: f"{name}: files may be at most {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB."}
        )
    content_type, signatures = ALLOWED_UPLOADS[extension]
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    if not any(head.startswith(signature) for signature in signatures):
        raise ValidationError({label: f"{name}: the file is not a real {extension.upper()} file."})
    return content_type
