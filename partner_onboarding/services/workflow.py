"""
The registration workflow: verify, reject, edit, and approve (create in SAP).

Ported from SAP Portal's customer routes (server.js:797–858) and vendor routes
(routes/vendors.js:252–396), with its bugs fixed:

* A rejection lands only on a PENDING or VERIFIED registration. The portal's
  approve route rejected whatever it was given, including a partner already
  created in SAP (server.js:819, routes/vendors.js:343).
* Creating the partner cannot happen twice. The portal read the next card code
  and posted, with no lock and nothing stored first, so a double click or a
  lost answer made a second partner. Here the row is locked, the card code is
  reserved and committed before SAP is asked, a second click while the first is
  in flight is refused, and a retry after a lost answer finds the partner under
  the reserved code and adopts it.
* Existing partners with the same GSTIN or PAN are shown to the approver
  before anything is created (409), unless the approver confirms.
* The lookups and the partner use the registration's own company. The portal
  read Oil's master data whatever the registration's company was.

Approval follows the SAP write rules in the sap-integration skill: refuse up
front what SAP would refuse, ask SAP first, store the reference before
posting, call SAP last, and record a failure only after the transaction that
tried has unwound (otherwise the rollback erases the record of it).
"""

import logging
import os
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from ..constants import (
    CARD_CODE_PREFIX_RE,
    KINDS_SENT_TO_SAP,
    OPEN_STATUSES,
    AddressType,
    EventKind,
    RegistrationStatus,
)
from ..families import VENDOR_FAMILY
from ..models import CustomerRegistration, RegistrationAttachment, VendorRegistration
from ..serializers import ManagerFieldsSerializer
from .records import record_event, replace_addresses, replace_bank_accounts
from .sap_payload import build_payload

logger = logging.getLogger(__name__)

#: A creation in flight longer than this is treated as abandoned (a worker
#: killed mid-post): SAP's POST timeout is two minutes, the uploads a few more.
POSTING_STALE_AFTER = timedelta(minutes=10)
#: OCRD.CardCode is nvarchar(15).
SAP_CARD_CODE_MAX = 15
MAX_CARD_CODE_TRIES = 25
SAP_ERRORS = (SAPValidationError, SAPConnectionError, SAPDataError)


class WorkflowError(Exception):
    """A refusal the API reports as-is: ``detail``, a ``code`` and any extras."""

    status_code = 400
    code = "invalid_state"

    def __init__(self, message, *, status_code=None, extra=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        self.extra = extra or {}

    def body(self) -> dict:
        return {"detail": str(self), "code": self.code, **self.extra}


class PossibleDuplicate(WorkflowError):
    status_code = 409
    code = "possible_duplicate"


class PostingInProgress(WorkflowError):
    status_code = 409
    code = "sap_posting_in_progress"


class DocumentsNotSent(WorkflowError):
    code = "documents_not_sent"


def _status_for(exc) -> int:
    if isinstance(exc, SAPConnectionError):
        return 503
    if isinstance(exc, SAPDataError):
        return 502
    return 400


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def lock(family, pk, company):
    """The row, locked for this transaction, if it is this company's. Else 404.

    ``of=("self",)`` and no ``select_related``: PostgreSQL refuses FOR UPDATE on
    the nullable side of an outer join (4a162bf), and SQLite does not lock at all.
    """
    row = family.model.objects.select_for_update(of=("self",)).filter(pk=pk, company=company).first()
    if row is None:
        raise NotFound(f"No such {family.label} registration.")
    return row


def _in_flight(registration) -> bool:
    since = registration.sap_posting_since
    return since is not None and timezone.now() - since < POSTING_STALE_AFTER


def _refuse_if_posting(registration):
    if _in_flight(registration):
        started = timezone.localtime(registration.sap_posting_since).strftime("%H:%M")
        raise PostingInProgress(
            f"{registration.reference} is being created in SAP right now (started {started}). "
            "Wait a minute and reload before doing anything else with it."
        )


def _require_open(registration, action: str):
    if registration.status == RegistrationStatus.APPROVED:
        raise WorkflowError(
            f"{registration.reference} is already in SAP as {registration.sap_card_code or registration.card_code}; "
            f"it cannot be {action}."
        )
    if registration.status not in OPEN_STATUSES:
        raise WorkflowError(f"{registration.reference} is {registration.get_status_display().lower()}; it cannot be {action}.")


# ---------------------------------------------------------------------------
# verify / reject / edit
# ---------------------------------------------------------------------------


def verify(family, pk, company, user, note: str = ""):
    with transaction.atomic():
        registration = lock(family, pk, company)
        _refuse_if_posting(registration)
        if registration.status != RegistrationStatus.PENDING:
            raise WorkflowError(
                f"Only a pending registration can be verified; {registration.reference} is "
                f"{registration.get_status_display().lower()}."
            )
        now = timezone.now()
        registration.status = RegistrationStatus.VERIFIED
        registration.verified_by = user
        registration.verified_at = now
        registration.updated_by = user
        registration.save(update_fields=["status", "verified_by", "verified_at", "updated_by", "updated_at"])
        record_event(registration, EventKind.VERIFIED, user=user, note=note, at=now)
    return registration


def reject(family, pk, company, user, reason: str):
    with transaction.atomic():
        registration = lock(family, pk, company)
        _refuse_if_posting(registration)
        _require_open(registration, "rejected")
        now = timezone.now()
        previous = registration.status
        registration.status = RegistrationStatus.REJECTED
        registration.rejected_by = user
        registration.rejected_at = now
        registration.rejection_reason = reason
        registration.updated_by = user
        registration.save(
            update_fields=[
                "status",
                "rejected_by",
                "rejected_at",
                "rejection_reason",
                "updated_by",
                "updated_at",
            ]
        )
        record_event(registration, EventKind.REJECTED, user=user, note=reason, at=now, data={"from": previous})
    return registration


def _renamed_addresses(registration, old_name: str) -> list[dict] | None:
    """Addresses whose automatic name followed the old business name, renamed.

    The names are ``<NAME> - <STATE>``; correcting a typo in the name should not
    leave the typo in every address SAP receives.
    """
    old_prefix = (old_name or "").strip().upper()
    addresses, touched = [], False
    for address in registration.addresses.all():
        name = address.address_name
        if old_prefix and name.startswith(old_prefix):
            name = ""
            touched = True
        addresses.append(
            {
                "address_type": address.address_type,
                "address_name": name,
                "street": address.street,
                "block": address.block,
                "city": address.city,
                "zip_code": address.zip_code,
                "state": address.state,
                "country": address.country,
                "gstin": address.gstin,
            }
        )
    return addresses if touched else None


def edit(family, pk, company, user, data: dict):
    """Apply a verifier's corrections to an open registration."""
    with transaction.atomic():
        registration = lock(family, pk, company)
        _refuse_if_posting(registration)
        _require_open(registration, "edited")
        old_name = registration.card_name
        changed = []
        for key, value in data.items():
            if key in ("addresses", "bank_accounts"):
                continue
            if key == "card_code_prefix" and not value:
                value = family.default_prefix
            if getattr(registration, key) != value:
                setattr(registration, key, value)
                changed.append(key)
        registration.updated_by = user
        registration.save()
        if "addresses" in data:
            replace_addresses(registration, data["addresses"])
            changed.append("addresses")
        elif "card_name" in changed:
            renamed = _renamed_addresses(registration, old_name)
            if renamed is not None:
                replace_addresses(registration, renamed)
                changed.append("addresses")
        if family is VENDOR_FAMILY and "bank_accounts" in data:
            replace_bank_accounts(registration, data["bank_accounts"])
            changed.append("bank_accounts")
        if changed:
            record_event(registration, EventKind.EDITED, user=user, data={"fields": changed})
    return registration


# ---------------------------------------------------------------------------
# approve: create the partner in SAP
# ---------------------------------------------------------------------------


def _apply_manager_fields(registration, family, manager: dict):
    for key in ManagerFieldsSerializer.MANAGER_FIELDS:
        if key in manager:
            setattr(registration, key, manager[key])
    if not registration.card_code_prefix:
        registration.card_code_prefix = family.default_prefix


def _apply_bank_codes(registration, bank_codes: list[dict]):
    accounts = {account.id: account for account in registration.bank_accounts.all()}
    for entry in bank_codes:
        account = accounts.get(entry["id"])
        if account is None:
            raise WorkflowError(f"Bank account {entry['id']} is not on {registration.reference}.")
        if account.sap_bank_code != entry["sap_bank_code"]:
            account.sap_bank_code = entry["sap_bank_code"]
            account.save(update_fields=["sap_bank_code"])


def _check_ready(registration, family):
    """Refuse up front what SAP would refuse, or what would silently go missing."""
    if not CARD_CODE_PREFIX_RE.match(registration.card_code_prefix or ""):
        raise WorkflowError("A card-code prefix is 1–10 letters or digits.")
    if not registration.addresses.filter(address_type=AddressType.BILL_TO).exists():
        raise WorkflowError("Add a billing address before creating the partner in SAP.")
    if family is VENDOR_FAMILY:
        missing = [
            f"…{account.account_number[-4:]} ({account.bank_name})"
            for account in registration.bank_accounts.all()
            if account.account_number.strip() and not account.sap_bank_code
        ]
        if missing:
            # createVendor dropped such an account from the partner with only
            # a log line; SAP needs the bank master code.
            raise WorkflowError(
                "Choose the SAP bank for every bank account before creating the vendor: " + ", ".join(missing) + "."
            )


def _reserved_codes(company, exclude) -> set:
    """Codes reserved by this company's registrations that SAP does not hold yet.

    Customers and vendors share SAP's card-code space, so both are read.
    """
    codes = set()
    for model in (CustomerRegistration, VendorRegistration):
        rows = model.objects.filter(company=company, sap_card_code="").exclude(card_code="")
        if isinstance(exclude, model):
            rows = rows.exclude(pk=exclude.pk)
        codes.update(rows.values_list("card_code", flat=True))
    return codes


def _following(code: str, prefix: str) -> str:
    digits = code[len(prefix):]
    if not digits.isdigit():
        raise WorkflowError(f"Card code {code} does not end in a number, so the next one cannot be worked out.")
    return f"{prefix}{str(int(digits) + 1).zfill(len(digits))}"


def _free_card_code(sap, registration, family) -> str:
    """The next code under the prefix that SAP does not hold and nobody here reserved."""
    prefix = registration.card_code_prefix
    reserved = _reserved_codes(registration.company, exclude=registration)
    code = sap.next_card_code(prefix, family.card_type)
    for _ in range(MAX_CARD_CODE_TRIES):
        if len(code) > SAP_CARD_CODE_MAX:
            raise WorkflowError(
                f"Card code {code} is longer than SAP's {SAP_CARD_CODE_MAX} characters; use a shorter prefix."
            )
        # next_card_code reads one card type; a partner of the other type may
        # already hold the code, which is why SAP is asked about each one.
        if code not in reserved and sap.business_partner(code) is None:
            return code
        code = _following(code, prefix)
    raise WorkflowError(f"No free card code found after {prefix}; check the prefix in SAP.")


def _is_ours(existing: dict, registration, family, sap) -> bool:
    """Whether the partner SAP holds under the reserved code is this registration's.

    It is when the type matches and either the name still does or SAP holds
    this registration's GSTIN or PAN on that very code. The tax ids matter
    because a verifier can correct the name after an approval that timed out
    (SAP created the partner, the app never heard): matching on the name alone
    then dropped the reserved code and reserved a new one.
    """
    if existing.get("card_type") != family.card_type:
        return False
    if (existing.get("card_name") or "").strip().upper() == registration.card_name.strip().upper():
        return True
    if not (registration.gstin or registration.pan):
        return False
    matches = sap.partners_with_tax_ids(family.card_type, gstin=registration.gstin, pan=registration.pan)
    return any(match.get("card_code") == existing.get("card_code") for match in matches)


def _sap_file_name(registration, attachment) -> str:
    stem = re.sub(r"[^A-Z0-9]+", "_", registration.card_name.upper()).strip("_")[:40] or "PARTNER"
    extension = os.path.splitext(attachment.original_name or attachment.file.name)[1].lower()
    return f"{stem}_{attachment.kind}_{attachment.pk}{extension}"


def _entry_of(answer) -> int:
    try:
        entry = int((answer or {}).get("AbsoluteEntry") or 0)
    except (TypeError, ValueError):
        entry = 0
    if entry <= 0:
        raise SAPDataError("SAP accepted the document but returned no attachment entry.")
    return entry


def _send_documents(registration, family, sap) -> tuple[int | None, list[str]]:
    """Upload the documents to one Attachments2 entry; each landed file is marked.

    Resumable: a retry sends only the files not yet marked, into the entry
    already recorded, so SAP is not left holding the same scan twice. A
    customer's failure stops the approval; a vendor's is a warning, as in the
    portal.
    """
    entry = registration.sap_attachment_entry
    pending = [
        attachment
        for attachment in registration.attachments.all()
        if attachment.kind in KINDS_SENT_TO_SAP and attachment.sent_to_sap_at is None
    ]
    warnings = []
    try:
        for attachment in pending:
            name = _sap_file_name(registration, attachment)
            if entry is None:
                entry = _entry_of(sap.upload_attachment(attachment.file.path, name))
                family.model.objects.filter(pk=registration.pk).update(sap_attachment_entry=entry)
            else:
                sap.add_line_to_existing_attachment(entry, attachment.file.path, name)
            RegistrationAttachment.objects.filter(pk=attachment.pk).update(sent_to_sap_at=timezone.now())
    except (*SAP_ERRORS, OSError) as exc:
        if family.attachments_fatal:
            raise DocumentsNotSent(
                f"The documents could not be sent to SAP, so the customer was not created: {exc}",
                status_code=_status_for(exc),
            ) from exc
        warnings.append(
            f"Not every document reached SAP ({exc}); the vendor was created without them. "
            "They remain on the registration."
        )
    registration.sap_attachment_entry = entry
    return entry, warnings


def _mark_created(registration, user, card_code, entry, warnings, *, adopted=False):
    now = timezone.now()
    registration.status = RegistrationStatus.APPROVED
    registration.card_code = card_code
    registration.sap_card_code = card_code
    registration.sap_attachment_entry = entry
    registration.approved_by = user
    registration.approved_at = now
    registration.sap_posting_since = None
    registration.sap_error = ""
    registration.sap_warning = "\n".join(warnings)
    registration.updated_by = user
    registration.save()
    note = (
        f"SAP already held {card_code} for this {registration.family} (the answer to an earlier "
        "attempt never arrived), so it was adopted rather than created again."
        if adopted
        else f"Created in SAP as {card_code}."
    )
    record_event(
        registration,
        EventKind.SAP_CREATED,
        user=user,
        note=note,
        at=now,
        data={"card_code": card_code, "attachment_entry": entry, "warnings": warnings, "adopted": adopted},
    )
    record_event(
        registration,
        EventKind.APPROVED,
        user=user,
        at=now,
        data={key: str(getattr(registration, key)) for key in ManagerFieldsSerializer.MANAGER_FIELDS},
    )


def _record_failure(family, pk, user, exc):
    """Clear the in-flight mark and write down what went wrong. Runs after the
    transaction that tried has unwound, so the record survives it."""
    message = str(exc) or type(exc).__name__
    with transaction.atomic():
        registration = family.model.objects.select_for_update(of=("self",)).filter(pk=pk).first()
        if registration is None:
            return
        registration.sap_posting_since = None
        registration.sap_error = message[:4000]
        registration.save(update_fields=["sap_posting_since", "sap_error", "updated_at"])
        record_event(
            registration,
            EventKind.SAP_FAILED,
            user=user,
            note=message,
            data={"card_code": registration.card_code, "error": type(exc).__name__},
        )


def approve(family, pk, company, user, manager: dict, bank_codes: list, confirm_duplicate: bool = False):
    """Create the partner in SAP. Returns ``(registration, warnings)``.

    1. Lock the row; it must be VERIFIED and not already being created.
    2. Apply the approver's SAP fields and bank codes; refuse what SAP would.
    3. If a code is already reserved and SAP holds it for this partner, adopt it.
    4. Ask SAP for partners with the same GSTIN / PAN (409 unless confirmed).
    5. Reserve the code and mark the row in flight — committed before SAP.
    6. Send the documents, then — locked again, last — create the partner.
    """
    sap = None
    try:
        with transaction.atomic():
            registration = lock(family, pk, company)
            if registration.status == RegistrationStatus.APPROVED:
                raise WorkflowError(f"{registration.reference} is already in SAP as {registration.sap_card_code}.")
            if registration.status != RegistrationStatus.VERIFIED:
                raise WorkflowError(
                    f"Only a verified registration can be created in SAP; {registration.reference} is "
                    f"{registration.get_status_display().lower()}."
                )
            _refuse_if_posting(registration)
            _apply_manager_fields(registration, family, manager)
            if family is VENDOR_FAMILY:
                _apply_bank_codes(registration, bank_codes)
            _check_ready(registration, family)
            sap = SAPClient(company_code=registration.company.code)

            if registration.card_code:
                existing = sap.business_partner(registration.card_code)
                if existing is not None and _is_ours(existing, registration, family, sap):
                    _mark_created(
                        registration, user, registration.card_code, registration.sap_attachment_entry, [], adopted=True
                    )
                    return registration, []
                if existing is not None or not registration.card_code.startswith(registration.card_code_prefix):
                    registration.card_code = ""

            if not confirm_duplicate:
                matches = sap.partners_with_tax_ids(family.card_type, gstin=registration.gstin, pan=registration.pan)
                if matches:
                    raise PossibleDuplicate(
                        f"SAP already has {len(matches)} {family.label}(s) with this GSTIN or PAN. "
                        "Check them; confirm to create another anyway.",
                        extra={"matches": matches},
                    )
            if not registration.card_code:
                registration.card_code = _free_card_code(sap, registration, family)
            registration.sap_posting_since = timezone.now()
            registration.sap_error = ""
            registration.updated_by = user
            registration.save()
    except IntegrityError as exc:
        raise WorkflowError(
            "Another registration reserved that card code a moment ago. Try again.",
            status_code=409,
        ) from exc
    except SAP_ERRORS as exc:
        _record_failure(family, pk, user, exc)
        raise

    # The reservation and the in-flight mark are committed. From here on a
    # failure is recorded on the row (and the mark cleared) after it unwinds.
    try:
        entry, warnings = _send_documents(registration, family, sap)
        with transaction.atomic():
            registration = lock(family, pk, company)
            if registration.status != RegistrationStatus.VERIFIED or registration.sap_posting_since is None:
                raise WorkflowError(f"{registration.reference} changed while it was being created; nothing was sent.")
            payload, payload_warnings = build_payload(registration, family, entry)
            warnings += payload_warnings
            result = sap.create_business_partner(payload)
            _mark_created(registration, user, result.get("card_code") or registration.card_code, entry, warnings)
    except Exception as exc:
        _record_failure(family, pk, user, exc)
        raise
    return registration, warnings

