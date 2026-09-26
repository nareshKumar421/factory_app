"""
The two kinds of registration, side by side.

Customers and vendors walk the same workflow and differ in data: which rights
guard them, which SAP card type and default codes they get, which documents
the form insists on, and whether a failed document upload stops the partner
from being created. Each view, service and importer takes a ``Family`` rather
than branching on a string, so the difference is written down once, here.
"""

from dataclasses import dataclass

from .constants import (
    CUSTOMER_BUSINESS_TYPES,
    CUSTOMER_CARD_CODE_PREFIX,
    CUSTOMER_CONTROL_ACCOUNT,
    CUSTOMER_CONTROL_ACCOUNT_NAME,
    CUSTOMER_MSME_BUSINESS_TYPES,
    VENDOR_BUSINESS_TYPES,
    VENDOR_CARD_CODE_PREFIX,
    VENDOR_CONTROL_ACCOUNT,
    VENDOR_CONTROL_ACCOUNT_NAME,
    VENDOR_MSME_BUSINESS_TYPES,
    AttachmentKind,
)
from .models import CUSTOMER, VENDOR, CustomerRegistration, VendorRegistration

APP = "partner_onboarding"


@dataclass(frozen=True)
class Family:
    key: str
    label: str
    model: type
    #: OCRD.CardType, as the lookups and duplicate checks take it.
    card_type: str
    #: BusinessPartners.CardType, as the Service Layer takes it.
    sap_card_type: str
    default_prefix: str
    default_control_account: str
    default_control_account_name: str
    view_codename: str
    verify_codename: str
    approve_codename: str
    #: Slots a submission may use, and those it must fill.
    attachment_kinds: tuple
    required_attachments: tuple
    business_types: tuple
    msme_business_types: tuple
    #: The portal stopped a customer when its documents could not reach SAP
    #: (createCustomer awaited the upload) and went on without them for a
    #: vendor (createVendor caught it as "non-fatal").
    attachments_fatal: bool
    #: The portal's table, for the importer's messages.
    portal_table: str

    @property
    def view_permission(self) -> str:
        return f"{APP}.{self.view_codename}"

    @property
    def verify_permission(self) -> str:
        return f"{APP}.{self.verify_codename}"

    @property
    def approve_permission(self) -> str:
        return f"{APP}.{self.approve_codename}"

    @property
    def permissions(self) -> tuple:
        return (self.view_permission, self.verify_permission, self.approve_permission)


CUSTOMER_FAMILY = Family(
    key=CUSTOMER,
    label="customer",
    model=CustomerRegistration,
    card_type="C",
    sap_card_type="cCustomer",
    default_prefix=CUSTOMER_CARD_CODE_PREFIX,
    default_control_account=CUSTOMER_CONTROL_ACCOUNT,
    default_control_account_name=CUSTOMER_CONTROL_ACCOUNT_NAME,
    view_codename="can_view_customer_registrations",
    verify_codename="can_verify_customer_registrations",
    approve_codename="can_approve_customer_registrations",
    attachment_kinds=(
        AttachmentKind.PAN,
        AttachmentKind.AADHAAR,
        AttachmentKind.CHEQUE,
        AttachmentKind.MSME,
        AttachmentKind.OTHER,
    ),
    # register.html: PAN card, Aadhaar card and a cancelled cheque (+ the MSME
    # certificate when registered under MSME).
    required_attachments=(AttachmentKind.PAN, AttachmentKind.AADHAAR, AttachmentKind.CHEQUE),
    business_types=CUSTOMER_BUSINESS_TYPES,
    msme_business_types=CUSTOMER_MSME_BUSINESS_TYPES,
    attachments_fatal=True,
    portal_table="ZCUST_PORTAL",
)

VENDOR_FAMILY = Family(
    key=VENDOR,
    label="vendor",
    model=VendorRegistration,
    card_type="S",
    sap_card_type="cSupplier",
    default_prefix=VENDOR_CARD_CODE_PREFIX,
    default_control_account=VENDOR_CONTROL_ACCOUNT,
    default_control_account_name=VENDOR_CONTROL_ACCOUNT_NAME,
    view_codename="can_view_vendor_registrations",
    verify_codename="can_verify_vendor_registrations",
    approve_codename="can_approve_vendor_registrations",
    attachment_kinds=(
        AttachmentKind.PAN,
        AttachmentKind.CHEQUE,
        AttachmentKind.GST,
        AttachmentKind.MSME,
        AttachmentKind.FSSAI,
        AttachmentKind.OTHER,
    ),
    # vendor-register.html: PAN card, cancelled cheque and GST certificate
    # (+ the MSME certificate when registered under MSME).
    required_attachments=(AttachmentKind.PAN, AttachmentKind.CHEQUE, AttachmentKind.GST),
    business_types=VENDOR_BUSINESS_TYPES,
    msme_business_types=VENDOR_MSME_BUSINESS_TYPES,
    attachments_fatal=False,
    portal_table="ZVENDOR_PORTAL",
)

FAMILIES = {CUSTOMER: CUSTOMER_FAMILY, VENDOR: VENDOR_FAMILY}

#: Every right the API checks.
ALL_PERMISSIONS = CUSTOMER_FAMILY.permissions + VENDOR_FAMILY.permissions


def family_of(registration) -> Family:
    return FAMILIES[registration.family]
