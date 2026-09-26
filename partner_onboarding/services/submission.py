"""
A public form becomes a PENDING registration.

Ported from SAP Portal's ``POST /api/customers/submit`` (server.js:694) and
``POST /api/vendors/submit`` (routes/vendors.js:190). The documents arrive as
files and are stored as files; the portal read them into base64 in the browser
and wrote the data URLs into an NCLOB column.
"""

from django.db import transaction
from rest_framework.exceptions import ValidationError

from ..constants import (
    MAX_ATTACHMENTS_PER_SUBMISSION,
    MULTI_FILE_KINDS,
    AttachmentKind,
    EventKind,
    RegistrationStatus,
)
from ..families import VENDOR_FAMILY
from ..validators import check_upload
from .records import (
    add_attachment,
    delete_files,
    form_addresses,
    record_event,
    replace_addresses,
    replace_bank_accounts,
)

#: Serializer keys that are not columns of the registration itself.
_NOT_COLUMNS = {"company", "bill_addresses", "ship_addresses", "bank_accounts"}


def collect_files(files, family, has_msme: bool) -> list[tuple]:
    """The uploads of one submission as ``(kind, upload, content_type)``.

    File fields are named after the slot (``pan``, ``cheque``, ``other`` …).
    Refuses a field the form does not have, a second file in a single slot, a
    missing required document, and anything ``check_upload`` refuses. Errors
    come back under ``documents``, keyed by slot (``files`` for the whole set),
    so the PAN card's slot is never confused with the PAN number field.
    """
    allowed = {kind.value.lower(): kind for kind in family.attachment_kinds}
    unexpected = sorted(key for key in files.keys() if key not in allowed)
    if unexpected:
        raise ValidationError({"documents": {"files": f"Unexpected file field(s): {', '.join(unexpected)}."}})

    collected, errors = [], {}
    for key, kind in allowed.items():
        uploads = files.getlist(key)
        if len(uploads) > 1 and kind not in MULTI_FILE_KINDS:
            errors[key] = f"Attach one file for the {kind.label}."
            continue
        for upload in uploads:
            try:
                collected.append((kind, upload, check_upload(upload, key)))
            except ValidationError as exc:
                errors.update(exc.detail)

    required = list(family.required_attachments) + ([AttachmentKind.MSME] if has_msme else [])
    present = {kind for kind, _, _ in collected}
    for kind in required:
        key = kind.value.lower()
        if kind not in present and key not in errors:
            errors[key] = f"The {kind.label} is required."
    if len(collected) > MAX_ATTACHMENTS_PER_SUBMISSION:
        errors["files"] = f"At most {MAX_ATTACHMENTS_PER_SUBMISSION} files per registration."
    if errors:
        raise ValidationError({"documents": errors})
    return collected


def submit(family, validated: dict, files: list[tuple], company):
    """Create the registration, its addresses, bank accounts and documents."""
    stored = []
    try:
        with transaction.atomic():
            columns = {key: value for key, value in validated.items() if key not in _NOT_COLUMNS}
            registration = family.model.objects.create(
                company=company,
                status=RegistrationStatus.PENDING,
                # The portal's starting values for the approver (server.js:730,
                # routes/vendors.js:225): the family's card-code prefix and control account.
                card_code_prefix=family.default_prefix,
                control_account=family.default_control_account,
                control_account_name=family.default_control_account_name,
                **columns,
            )
            replace_addresses(
                registration,
                form_addresses(
                    validated["bill_addresses"],
                    validated.get("ship_addresses") or [],
                    validated.get("ship_same_as_bill", True),
                    registration.gstin,
                ),
            )
            if family is VENDOR_FAMILY:
                replace_bank_accounts(registration, validated["bank_accounts"])
            for kind, upload, content_type in files:
                stored.append(
                    add_attachment(
                        registration, kind, name=upload.name, content_type=content_type, content=upload
                    )
                )
            record_event(
                registration,
                EventKind.SUBMITTED,
                actor_name=f"{registration.contact_name} (public form)",
                data={"documents": len(files)},
            )
    except Exception:
        delete_files(stored)
        raise
    return registration
