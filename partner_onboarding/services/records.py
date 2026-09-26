"""
Writing a registration's children: addresses (with their SAP names), bank
accounts, documents, and events. Shared by the public submission, the
verifier's edit and the importer, so all three name addresses the same way.
"""

import logging

from django.core.files.base import ContentFile
from django.utils import timezone

from ..constants import SAP_ADDRESS_NAME_MAX, AddressType
from ..models import (
    PartnerAddress,
    RegistrationAttachment,
    RegistrationEvent,
    VendorBankAccount,
)

logger = logging.getLogger(__name__)


def owner_kwargs(registration) -> dict:
    """``{"customer": reg}`` or ``{"vendor": reg}`` for a child row."""
    return {registration.family: registration}


def actor_label(user) -> str:
    if user is None:
        return ""
    return user.full_name or user.email


def record_event(registration, kind, *, user=None, actor_name="", note="", data=None, at=None):
    return RegistrationEvent.objects.create(
        **owner_kwargs(registration),
        kind=kind,
        actor=user,
        actor_name=actor_name or actor_label(user),
        note=note or "",
        data=data or {},
        at=at or timezone.now(),
    )


def auto_address_name(card_name: str, state: str) -> str:
    """The portal's frozen address name: ``<BUSINESS NAME> - <STATE>``.

    register.html / vendor-register.html computed it in the browser and would
    not let anyone type it. It is built here instead, from the SAP state code
    the form now offers, and cut to SAP's 50 characters — it is a label this
    app makes up, not something anybody typed.
    """
    name = (card_name or "").strip().upper()
    label = f"{name} - {state}" if state else name
    return label[:SAP_ADDRESS_NAME_MAX].strip() or "ADDRESS"


def unique_names(addresses: list[dict], card_name: str) -> list[dict]:
    """Give every address a name, unique within its type.

    SAP refuses a partner with two billing (or two shipping) addresses of the
    same name. The portal named two addresses in one state identically and SAP
    refused the whole partner; the second is numbered here instead.
    """
    seen: dict[str, set] = {AddressType.BILL_TO: set(), AddressType.SHIP_TO: set()}
    for address in addresses:
        base = (address.get("address_name") or "").strip().upper() or auto_address_name(
            card_name, address.get("state", "")
        )
        taken = seen[address["address_type"]]
        name, counter = base, 1
        while name in taken:
            counter += 1
            suffix = f" {counter}"
            name = base[: SAP_ADDRESS_NAME_MAX - len(suffix)].rstrip() + suffix
        taken.add(name)
        address["address_name"] = name
    return addresses


def replace_addresses(registration, addresses: list[dict]):
    """Replace every address with ``addresses`` (each carrying ``address_type``)."""
    registration.addresses.all().delete()
    positions = {AddressType.BILL_TO: 0, AddressType.SHIP_TO: 0}
    rows = []
    for address in unique_names([dict(a) for a in addresses], registration.card_name):
        kind = address["address_type"]
        rows.append(
            PartnerAddress(
                **owner_kwargs(registration),
                address_type=kind,
                position=positions[kind],
                address_name=address["address_name"],
                street=address.get("street", ""),
                block=address.get("block", ""),
                city=address.get("city", ""),
                zip_code=address.get("zip_code", ""),
                state=address.get("state", ""),
                country=address.get("country", "IN") or "IN",
                gstin=address.get("gstin", ""),
            )
        )
        positions[kind] += 1
    PartnerAddress.objects.bulk_create(rows)


def form_addresses(bill: list[dict], ship: list[dict], same_as_bill: bool, gstin: str) -> list[dict]:
    """The public form's two lists as one, the way the portal posted them.

    * "Same as billing" makes the shipping addresses copies of the billing ones
      (register.html: ``allShipAddresses: sameAsBill ? allBill : allShip``).
    * The first billing address takes the registration's GSTIN when its own is
      blank — the portal's rule for a single address (createCustomer /
      createVendor fell back to ``d.gstin``). Without it a B2B customer's
      required GSTIN never reached SAP whenever the address field was left empty.
    """
    bill = [dict(a, address_type=AddressType.BILL_TO) for a in bill]
    if bill and gstin and not bill[0].get("gstin"):
        bill[0]["gstin"] = gstin
    source = bill if same_as_bill else ship
    ship = [dict(a, address_type=AddressType.SHIP_TO) for a in source]
    return bill + ship


def replace_bank_accounts(vendor, accounts: list[dict]):
    vendor.bank_accounts.all().delete()
    VendorBankAccount.objects.bulk_create(
        VendorBankAccount(
            vendor=vendor,
            position=index,
            bank_name=account["bank_name"],
            branch=account.get("branch", ""),
            account_number=account["account_number"],
            ifsc=account["ifsc"],
            account_type=account.get("account_type") or "Current",
            swift_code=account.get("swift_code", ""),
            is_primary=index == 0,
            sap_bank_code=account.get("sap_bank_code", ""),
        )
        for index, account in enumerate(accounts)
    )


def add_attachment(registration, kind, *, name: str, content_type: str, content, size=None):
    """Store one document as a file. ``content`` is an uploaded file or bytes."""
    if isinstance(content, (bytes, bytearray)):
        payload = ContentFile(bytes(content))
        size = len(content)
    else:
        payload = content
        size = size if size is not None else content.size
    attachment = RegistrationAttachment(
        **owner_kwargs(registration),
        kind=kind,
        original_name=(name or "document")[:255],
        content_type=content_type,
        size=size,
    )
    attachment.file.save(name or "document", payload, save=False)
    attachment.save()
    return attachment


def delete_files(attachments):
    """Remove stored files whose rows were rolled back or replaced."""
    for attachment in attachments:
        try:
            attachment.file.delete(save=False)
        except Exception:  # pragma: no cover - best effort clean-up
            logger.warning("Could not delete %s", attachment.file.name, exc_info=True)
