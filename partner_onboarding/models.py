"""
Partner Onboarding — customer and vendor registration, ported from SAP Portal.

A customer or vendor fills in a public form (no login), a verifier checks and
corrects it, and an approver sets the SAP master-data fields and creates the
business partner in SAP. What SAP Portal kept in two HANA tables in the Oil
schema (``ZCUST_PORTAL``, ``ZVENDOR_PORTAL``) lives here, in JI's own database,
one row per registration with its company.

Company-scoped: every row belongs to one company (the one the form was sent
to), and the internal screens show only the company in the ``Company-Code``
header. The partner is created in that company's SAP.

Differences from the portal's tables, on purpose:

* Addresses, bank accounts and documents are rows, not JSON blobs, and the
  documents are files on disk — never base64 in the database. The portal kept
  every scan as a data URL in an NCLOB, and never stored a vendor's shipping
  addresses, main group, chain or sales employee at all (its UPDATE had no
  column for them), nor who verified or rejected a customer.
* The card code is reserved on the row *before* SAP is asked to create the
  partner, so a lost answer leaves a trace and a retry reuses the same code
  (SAP refuses a second partner under one code).
* Every step is an event with its actor, so the history the approver's screen
  shows is the record, not a reconstruction.

Children (addresses, documents, events) serve both kinds of registration. Each
has two nullable foreign keys — ``customer`` and ``vendor`` — and a check
constraint that exactly one is set. That keeps one table per concept with real
foreign keys (cascade, joins, admin) instead of a generic relation, and the
constraint makes an orphan or a row owned twice impossible. Bank accounts are
vendor-only, so they hang off the vendor alone.
"""

import os
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from company.models import Company
from gate_core.models.base import BaseModel

from .constants import (
    SAP_ADDRESS_NAME_MAX,
    SAP_CARD_NAME_MAX,
    AddressType,
    AttachmentKind,
    BankAccountType,
    Country,
    Currency,
    CustomerType,
    EventKind,
    RegistrationStatus,
    VendorType,
)

CUSTOMER = "customer"
VENDOR = "vendor"


class PartnerRegistration(BaseModel):
    """What both forms collect, what the approver sets, and what SAP answered."""

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="partner_%(class)s_rows"
    )
    status = models.CharField(
        max_length=20,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.PENDING,
        db_index=True,
    )
    submitted_at = models.DateTimeField(default=timezone.now)

    # ---- the business ---------------------------------------------------
    card_name = models.CharField(max_length=SAP_CARD_NAME_MAX)
    foreign_name = models.CharField(max_length=100, blank=True, default="")
    type_of_business = models.CharField(max_length=30, blank=True, default="")
    industry = models.CharField(max_length=100, blank=True, default="")

    # ---- the contact person ---------------------------------------------
    contact_first_name = models.CharField(max_length=50)
    contact_last_name = models.CharField(max_length=50, blank=True, default="")
    contact_title = models.CharField(max_length=90, blank=True, default="")
    mobile = models.CharField(max_length=20)
    email = models.EmailField(max_length=100)
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.INR)

    # ---- tax and compliance ---------------------------------------------
    gstin = models.CharField(max_length=15, blank=True, default="")
    pan = models.CharField(max_length=10, blank=True, default="")
    has_msme = models.BooleanField(default=False)
    msme_number = models.CharField(max_length=30, blank=True, default="")
    msme_type = models.CharField(max_length=20, blank=True, default="")
    msme_business_type = models.CharField(max_length=30, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    ship_same_as_bill = models.BooleanField(default=True)

    # ---- set by the approver: SAP master data ---------------------------
    card_code_prefix = models.CharField(max_length=10, blank=True, default="")
    bp_group_code = models.IntegerField(null=True, blank=True)
    bp_group_name = models.CharField(max_length=100, blank=True, default="")
    payment_terms_code = models.IntegerField(null=True, blank=True)
    payment_terms_name = models.CharField(max_length=100, blank=True, default="")
    sales_employee_code = models.IntegerField(null=True, blank=True)
    sales_employee_name = models.CharField(max_length=155, blank=True, default="")
    control_account = models.CharField(
        max_length=15, blank=True, default="",
        help_text="AR account for a customer, AP account for a vendor (SAP DebitorAccount).",
    )
    control_account_name = models.CharField(max_length=100, blank=True, default="")
    credit_limit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    main_group = models.CharField(max_length=50, blank=True, default="", help_text="SAP U_Main_Group.")
    chain = models.CharField(max_length=50, blank=True, default="", help_text="SAP U_Chain.")
    sap_currency = models.CharField(
        max_length=3, choices=Currency.choices, blank=True, default="",
        help_text="The approver's currency; blank means the one the form chose.",
    )
    territory = models.CharField(max_length=100, blank=True, default="")
    manager_notes = models.TextField(blank=True, default="")

    # ---- SAP ------------------------------------------------------------
    card_code = models.CharField(
        max_length=15, blank=True, default="",
        help_text="Reserved before SAP is asked to create the partner; reused on a retry.",
    )
    sap_card_code = models.CharField(
        max_length=15, blank=True, default="", help_text="The partner SAP created."
    )
    sap_attachment_entry = models.PositiveIntegerField(
        null=True, blank=True, help_text="Attachments2 AbsoluteEntry holding the documents."
    )
    sap_posting_since = models.DateTimeField(
        null=True, blank=True,
        help_text="Set while a creation in SAP is in flight, so a second click waits.",
    )
    sap_error = models.TextField(blank=True, default="", help_text="SAP's words on the last failure.")
    sap_warning = models.TextField(blank=True, default="")

    # ---- decisions --------------------------------------------------------
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")

    # ---- import -----------------------------------------------------------
    legacy_portal_id = models.PositiveIntegerField(
        null=True, blank=True, unique=True,
        help_text="The row's ID in SAP Portal's table, for imported registrations.",
    )
    legacy_fields = models.JSONField(
        default=dict, blank=True,
        help_text="Portal columns JI has no field for (never sent to SAP), kept as imported.",
    )

    family = ""
    reference_prefix = ""

    class Meta:
        abstract = True
        ordering = ["-submitted_at", "-id"]
        indexes = [models.Index(fields=["company", "status", "submitted_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "card_code"],
                condition=~Q(card_code=""),
                name="%(app_label)s_%(class)s_one_card_code",
            ),
        ]

    def __str__(self):
        return f"{self.reference} {self.card_name}"

    @property
    def reference(self) -> str:
        return f"{self.reference_prefix}-{self.pk or 0:05d}"

    @property
    def is_open(self) -> bool:
        return self.status in (RegistrationStatus.PENDING, RegistrationStatus.VERIFIED)

    @property
    def contact_name(self) -> str:
        return f"{self.contact_first_name} {self.contact_last_name}".strip()


class CustomerRegistration(PartnerRegistration):
    """One customer's registration (SAP Portal's ``ZCUST_PORTAL`` row)."""

    customer_type = models.CharField(
        max_length=15, choices=CustomerType.choices, default=CustomerType.B2B
    )
    website = models.CharField(max_length=100, blank=True, default="")
    contact_mobile = models.CharField(max_length=20, blank=True, default="")
    contact_email = models.EmailField(max_length=100, blank=True, default="")

    family = CUSTOMER
    reference_prefix = "REG"

    class Meta(PartnerRegistration.Meta):
        # Only the rights below: no add/change/delete/view rows that nothing
        # checks sitting beside them in the group editor.
        default_permissions = ()
        permissions = [
            ("can_view_customer_registrations", "Can view customer registrations"),
            (
                "can_verify_customer_registrations",
                "Can verify, edit and reject customer registrations",
            ),
            (
                "can_approve_customer_registrations",
                "Can approve customer registrations and create the customer in SAP",
            ),
        ]


class VendorRegistration(PartnerRegistration):
    """One vendor's registration (SAP Portal's ``ZVENDOR_PORTAL`` row)."""

    vendor_type = models.CharField(
        max_length=10, choices=VendorType.choices, default=VendorType.SUPPLIER
    )
    products = models.CharField(max_length=500, blank=True, default="")
    payment_terms_requested = models.CharField(
        max_length=50, blank=True, default="", help_text="What the vendor asked for, e.g. 30 Days."
    )
    alt_contact = models.CharField(max_length=20, blank=True, default="")
    tan = models.CharField(max_length=10, blank=True, default="")
    has_tds = models.BooleanField(default=False)
    tds_category = models.CharField(max_length=100, blank=True, default="")
    tds_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tds_ldc_number = models.CharField(max_length=50, blank=True, default="")
    fssai_number = models.CharField(max_length=14, blank=True, default="", help_text="SAP U_Fssai.")

    family = VENDOR
    reference_prefix = "VEND"

    class Meta(PartnerRegistration.Meta):
        default_permissions = ()
        permissions = [
            ("can_view_vendor_registrations", "Can view vendor registrations"),
            (
                "can_verify_vendor_registrations",
                "Can verify, edit and reject vendor registrations",
            ),
            (
                "can_approve_vendor_registrations",
                "Can approve vendor registrations and create the vendor in SAP",
            ),
        ]


def _one_owner(name: str) -> models.CheckConstraint:
    """Exactly one of ``customer`` / ``vendor`` is set."""
    return models.CheckConstraint(
        condition=(
            Q(customer__isnull=False, vendor__isnull=True)
            | Q(customer__isnull=True, vendor__isnull=False)
        ),
        name=name,
    )


class _OwnedByRegistration(models.Model):
    """The ``registration`` accessor shared by the children of either kind."""

    class Meta:
        abstract = True

    @property
    def registration(self):
        return self.customer if self.customer_id else self.vendor


class PartnerAddress(_OwnedByRegistration):
    """One billing or shipping address; SAP's BPAddresses line."""

    customer = models.ForeignKey(
        CustomerRegistration, on_delete=models.CASCADE, null=True, blank=True, related_name="addresses"
    )
    vendor = models.ForeignKey(
        VendorRegistration, on_delete=models.CASCADE, null=True, blank=True, related_name="addresses"
    )
    address_type = models.CharField(max_length=10, choices=AddressType.choices)
    position = models.PositiveSmallIntegerField(default=0)
    address_name = models.CharField(max_length=SAP_ADDRESS_NAME_MAX)
    street = models.CharField(max_length=100)
    block = models.CharField(max_length=100, blank=True, default="")
    city = models.CharField(max_length=100)
    zip_code = models.CharField(max_length=20, blank=True, default="")
    state = models.CharField(max_length=3, blank=True, default="", help_text="SAP state code (OCST).")
    country = models.CharField(max_length=3, choices=Country.choices, default=Country.IN)
    gstin = models.CharField(max_length=15, blank=True, default="")

    class Meta:
        ordering = ["address_type", "position", "id"]
        default_permissions = ()
        constraints = [_one_owner("partner_onboarding_address_one_owner")]

    def __str__(self):
        return f"{self.get_address_type_display()} {self.address_name}"


class VendorBankAccount(models.Model):
    """One of a vendor's bank accounts; SAP's BPBankAccounts line."""

    vendor = models.ForeignKey(
        VendorRegistration, on_delete=models.CASCADE, related_name="bank_accounts"
    )
    position = models.PositiveSmallIntegerField(default=0)
    bank_name = models.CharField(max_length=100)
    branch = models.CharField(max_length=50, blank=True, default="")
    account_number = models.CharField(max_length=50)
    ifsc = models.CharField(max_length=11)
    account_type = models.CharField(
        max_length=20, choices=BankAccountType.choices, default=BankAccountType.CURRENT
    )
    swift_code = models.CharField(max_length=11, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    sap_bank_code = models.CharField(
        max_length=30, blank=True, default="",
        help_text="SAP bank master code (ODSC), chosen by the approver.",
    )

    class Meta:
        ordering = ["position", "id"]
        default_permissions = ()

    def __str__(self):
        return f"{self.bank_name} …{self.account_number[-4:]}"


def attachment_upload_to(instance, filename):
    """``partner_onboarding/<customer|vendor>/<yyyy>/<mm>/<uuid>.<ext>``.

    No names in the path: the stored path says nothing about whose document it
    is, and two uploads can never collide.
    """
    ext = os.path.splitext(filename)[1].lower()
    family = CUSTOMER if instance.customer_id else VENDOR
    today = timezone.now()
    return f"partner_onboarding/{family}/{today:%Y}/{today:%m}/{uuid.uuid4().hex}{ext}"


class RegistrationAttachment(_OwnedByRegistration):
    """One uploaded document. Served only through the permission-checked view."""

    customer = models.ForeignKey(
        CustomerRegistration, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments"
    )
    vendor = models.ForeignKey(
        VendorRegistration, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments"
    )
    kind = models.CharField(max_length=10, choices=AttachmentKind.choices)
    file = models.FileField(upload_to=attachment_upload_to, max_length=255)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    uploaded_at = models.DateTimeField(default=timezone.now)
    sent_to_sap_at = models.DateTimeField(
        null=True, blank=True, help_text="When the file became a line of the SAP attachment entry."
    )

    class Meta:
        ordering = ["kind", "id"]
        default_permissions = ()
        constraints = [_one_owner("partner_onboarding_attachment_one_owner")]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.original_name}"


class RegistrationEvent(_OwnedByRegistration):
    """One step in a registration's life, with who took it."""

    customer = models.ForeignKey(
        CustomerRegistration, on_delete=models.CASCADE, null=True, blank=True, related_name="events"
    )
    vendor = models.ForeignKey(
        VendorRegistration, on_delete=models.CASCADE, null=True, blank=True, related_name="events"
    )
    kind = models.CharField(max_length=20, choices=EventKind.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    actor_name = models.CharField(
        max_length=150, blank=True, default="",
        help_text="Who, as a name: the user's, the submitter's, or a portal username.",
    )
    at = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True, default="")
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at", "id"]
        default_permissions = ()
        constraints = [_one_owner("partner_onboarding_event_one_owner")]

    def __str__(self):
        return f"{self.get_kind_display()} {self.at:%Y-%m-%d %H:%M}"
