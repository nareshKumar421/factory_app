"""
Serializers: what the public forms may send, what the verifier may edit, what
the approver sets, and what the screens read back.

The rules are the portal forms' own (``register.html`` / ``vendor-register.html``
``#next1`` handlers), enforced here as well so a script cannot skip them:
required fields, GSTIN / PAN / Udyam formats, the MSME block, at least one
billing address with a street, city and state, and — for vendors — at least one
bank account. The portal checked these only in the browser; its server checked
five fields.

Widths are SAP's, so what is accepted here is what SAP will take: a card name
of at most 100 characters, a contact name of at most 50, address lines of 100.
The portal cut such values with ``substring`` when it posted, and SAP got a
different name from the one on screen.
"""

from decimal import Decimal

from rest_framework import serializers

from .constants import (
    FSSAI_RE,
    GSTIN_RE,
    IFSC_RE,
    MOBILE_RE,
    MSME_TYPES,
    PAN_RE,
    PUBLIC_COMPANY_CODES,
    SAP_CONTACT_NAME_MAX,
    SWIFT_RE,
    TAN_RE,
    UDYAM_RE,
    AddressType,
    BankAccountType,
    Country,
    Currency,
    CustomerType,
    RegistrationStatus,
    VendorType,
    CARD_CODE_PREFIX_RE,
)
from .families import CUSTOMER_FAMILY, VENDOR_FAMILY
from .models import (
    PartnerAddress,
    RegistrationAttachment,
    RegistrationEvent,
    VendorBankAccount,
)

MAX_ADDRESSES_PER_TYPE = 10
MAX_BANK_ACCOUNTS = 5
STATE_CODE_MAX = 3


def upper(value) -> str:
    return (value or "").strip().upper()


def sanitize_mobile(raw) -> str:
    """The portal's ``sanitizeMobile``: the last ten digits of an Indian number.

    ``+91 98xxxxxxxx`` and ``091…`` lose their prefix; anything else keeps its
    last ten digits. Applied to vendors, as the portal did (routes/vendors.js:64).
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 13 and digits.startswith("091"):
        return digits[3:]
    return digits[-10:]


def _canonical(value: str, allowed: tuple, message: str) -> str:
    """``value`` as spelled in ``allowed`` (case-insensitive), or a ValidationError."""
    for option in allowed:
        if option.lower() == (value or "").strip().lower():
            return option
    raise serializers.ValidationError(message)


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


class AddressInputSerializer(serializers.Serializer):
    """One address as a form sends it. The name is worked out by the server."""

    address_type = serializers.ChoiceField(choices=AddressType.choices, required=False)
    address_name = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    street = serializers.CharField(max_length=100)
    block = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    city = serializers.CharField(max_length=100)
    zip_code = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    state = serializers.CharField(max_length=STATE_CODE_MAX, required=False, allow_blank=True, default="")
    country = serializers.ChoiceField(choices=Country.choices, default=Country.IN)
    gstin = serializers.CharField(max_length=15, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        for field in ("address_name", "street", "block", "city", "zip_code", "state", "gstin"):
            attrs[field] = upper(attrs.get(field))
        if attrs.get("country", Country.IN) == Country.IN and not attrs["state"]:
            raise serializers.ValidationError({"state": "Choose the state."})
        if attrs["state"] and not attrs["state"].isalnum():
            raise serializers.ValidationError({"state": "Use the state code from the list."})
        if attrs["gstin"] and not GSTIN_RE.match(attrs["gstin"]):
            raise serializers.ValidationError({"gstin": "Enter a valid GSTIN (e.g. 06ABCDE1234F1Z5)."})
        return attrs


class BankAccountInputSerializer(serializers.Serializer):
    bank_name = serializers.CharField(max_length=100)
    branch = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    account_number = serializers.CharField(max_length=50)
    ifsc = serializers.CharField(max_length=11)
    account_type = serializers.ChoiceField(choices=BankAccountType.choices, default=BankAccountType.CURRENT)
    swift_code = serializers.CharField(max_length=11, required=False, allow_blank=True, default="")
    #: Set by the approver only; ignored on the public form.
    sap_bank_code = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        for field in ("bank_name", "branch", "account_number", "ifsc", "swift_code", "sap_bank_code"):
            attrs[field] = upper(attrs.get(field))
        attrs["account_number"] = attrs["account_number"].replace(" ", "")
        if not attrs["account_number"].isalnum():
            raise serializers.ValidationError({"account_number": "Use letters and digits only."})
        if not IFSC_RE.match(attrs["ifsc"]):
            raise serializers.ValidationError({"ifsc": "Enter a valid IFSC code (e.g. SBIN0001234)."})
        if attrs["swift_code"] and not SWIFT_RE.match(attrs["swift_code"]):
            raise serializers.ValidationError({"swift_code": "Enter a valid SWIFT code (8 or 11 characters)."})
        return attrs


# ---------------------------------------------------------------------------
# The registration's own fields, shared by the public form and the edit
# ---------------------------------------------------------------------------


class _RegistrationFieldsSerializer(serializers.Serializer):
    """Fields both forms send. Cross-field rules run in ``validate`` on the
    merged record, so a partial edit is held to the same rules as a submission."""

    family = None

    card_name = serializers.CharField(max_length=100)
    foreign_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    type_of_business = serializers.CharField(max_length=30, required=False, allow_blank=True, default="Company")
    industry = serializers.CharField(max_length=100)
    contact_first_name = serializers.CharField(max_length=50)
    contact_last_name = serializers.CharField(max_length=50)
    contact_title = serializers.CharField(max_length=90, required=False, allow_blank=True, default="")
    mobile = serializers.CharField(max_length=20)
    email = serializers.EmailField(max_length=100)
    currency = serializers.ChoiceField(choices=Currency.choices, default=Currency.INR)
    gstin = serializers.CharField(max_length=15, required=False, allow_blank=True, default="")
    pan = serializers.CharField(max_length=10)
    has_msme = serializers.BooleanField(default=False)
    msme_number = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    msme_type = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    msme_business_type = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    remarks = serializers.CharField(max_length=1000)

    UPPER_FIELDS = (
        "card_name",
        "foreign_name",
        "industry",
        "contact_first_name",
        "contact_last_name",
        "contact_title",
        "gstin",
        "pan",
        "msme_number",
        "remarks",
    )

    def to_internal_value(self, data):
        attrs = super().to_internal_value(data)
        for field in self.UPPER_FIELDS:
            if field in attrs:
                attrs[field] = upper(attrs[field])
        if "email" in attrs:
            attrs["email"] = attrs["email"].strip().lower()
        return attrs

    def merged(self, attrs) -> dict:
        """The record as it will be: the instance's values under the new ones."""
        instance = self.context.get("instance")
        if instance is None:
            return attrs
        current = {name: getattr(instance, name) for name in self.fields if hasattr(instance, name)}
        current.update(attrs)
        return current

    def validate(self, attrs):
        data = self.merged(attrs)
        errors = {}
        family = self.family

        if "mobile" in attrs and not MOBILE_RE.match(attrs["mobile"]):
            errors["mobile"] = "Enter a valid mobile number."
        if "type_of_business" in attrs and attrs["type_of_business"]:
            try:
                attrs["type_of_business"] = _canonical(
                    attrs["type_of_business"], family.business_types, "Choose a type of business from the list."
                )
            except serializers.ValidationError as exc:
                errors["type_of_business"] = exc.detail[0]
        if data.get("pan") and not PAN_RE.match(data["pan"]):
            errors["pan"] = "Invalid PAN format (e.g. ABCDE1234F)."
        if data.get("gstin") and not GSTIN_RE.match(data["gstin"]):
            errors["gstin"] = "Enter a valid GSTIN (e.g. 06ABCDE1234F1Z5)."
        if not data.get("gstin") and self.gstin_required(data):
            errors["gstin"] = self.gstin_required_message
        contact = f"{data.get('contact_first_name', '')} {data.get('contact_last_name', '')}".strip()
        if len(contact) > SAP_CONTACT_NAME_MAX:
            errors["contact_last_name"] = (
                f"First and last name together may be at most {SAP_CONTACT_NAME_MAX} characters (SAP's limit)."
            )
        errors.update(self._msme_errors(attrs, data))
        errors.update(self.family_errors(attrs, data))
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def _msme_errors(self, attrs, data) -> dict:
        errors = {}
        if not data.get("has_msme"):
            if "has_msme" in attrs:
                attrs.update(msme_number="", msme_type="", msme_business_type="")
            return errors
        if not data.get("msme_number"):
            errors["msme_number"] = "Udyam registration number is required."
        elif not UDYAM_RE.match(data["msme_number"]):
            errors["msme_number"] = "Format: UDYAM-HR-18-0040140."
        msme_type = data.get("msme_type") or ""
        if msme_type:
            try:
                attrs["msme_type"] = _canonical(msme_type, MSME_TYPES, "Choose the MSME type from the list.")
            except serializers.ValidationError as exc:
                errors["msme_type"] = exc.detail[0]
        elif self.family is VENDOR_FAMILY:
            errors["msme_type"] = "MSME type is required."
        business_type = data.get("msme_business_type") or ""
        if business_type:
            try:
                attrs["msme_business_type"] = _canonical(
                    business_type, self.family.msme_business_types, "Choose the MSME business type from the list."
                )
            except serializers.ValidationError as exc:
                errors["msme_business_type"] = exc.detail[0]
        elif self.family is VENDOR_FAMILY:
            errors["msme_business_type"] = "MSME business type is required."
        return errors

    gstin_required_message = "GSTIN is required."

    def gstin_required(self, data) -> bool:
        return True

    def family_errors(self, attrs, data) -> dict:
        return {}


class _CustomerFieldsMixin(serializers.Serializer):
    family = CUSTOMER_FAMILY

    website = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    contact_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    contact_email = serializers.EmailField(max_length=100, required=False, allow_blank=True, default="")

    gstin_required_message = "GSTIN is required for B2B customers."

    def gstin_required(self, data) -> bool:
        # register.html: GSTIN is required for B2B only.
        return data.get("customer_type", CustomerType.B2B) == CustomerType.B2B

    def family_errors(self, attrs, data) -> dict:
        errors = {}
        if attrs.get("contact_mobile") and not MOBILE_RE.match(attrs["contact_mobile"]):
            errors["contact_mobile"] = "Enter a valid mobile number."
        if "contact_email" in attrs:
            attrs["contact_email"] = (attrs["contact_email"] or "").strip().lower()
        if "website" in attrs:
            attrs["website"] = (attrs["website"] or "").strip()
        return errors


class _VendorFieldsMixin(serializers.Serializer):
    family = VENDOR_FAMILY

    vendor_type = serializers.ChoiceField(choices=VendorType.choices, default=VendorType.SUPPLIER)
    products = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    payment_terms_requested = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    alt_contact = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    tan = serializers.CharField(max_length=10, required=False, allow_blank=True, default="")
    fssai_number = serializers.CharField(max_length=14, required=False, allow_blank=True, default="")
    has_tds = serializers.BooleanField(default=False)
    tds_category = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    tds_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"), default=Decimal("0")
    )
    tds_ldc_number = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")

    def family_errors(self, attrs, data) -> dict:
        errors = {}
        # routes/vendors.js stores the mobile numbers as ten digits.
        for field in ("mobile", "alt_contact"):
            if attrs.get(field):
                if field == "alt_contact" and not MOBILE_RE.match(attrs[field]):
                    errors[field] = "Enter a valid mobile number."
                    continue
                attrs[field] = sanitize_mobile(attrs[field])
        for field in ("tan", "fssai_number", "tds_ldc_number", "products"):
            if field in attrs:
                attrs[field] = upper(attrs[field])
        if attrs.get("tan") and not TAN_RE.match(attrs["tan"]):
            errors["tan"] = "Enter a valid TAN (e.g. BLRA12345B)."
        if attrs.get("fssai_number") and not FSSAI_RE.match(attrs["fssai_number"]):
            errors["fssai_number"] = "An FSSAI licence number is 14 digits."
        return errors


def _address_lists_errors(bill, ship, same_as_bill) -> dict:
    errors = {}
    if not bill:
        errors["bill_addresses"] = "Give at least one billing address."
    elif len(bill) > MAX_ADDRESSES_PER_TYPE:
        errors["bill_addresses"] = f"At most {MAX_ADDRESSES_PER_TYPE} billing addresses."
    if not same_as_bill and not ship:
        errors["ship_addresses"] = "Give a shipping address, or tick 'same as billing'."
    elif len(ship or []) > MAX_ADDRESSES_PER_TYPE:
        errors["ship_addresses"] = f"At most {MAX_ADDRESSES_PER_TYPE} shipping addresses."
    return errors


# ---------------------------------------------------------------------------
# Public submissions
# ---------------------------------------------------------------------------


class _PublicSubmitMixin(serializers.Serializer):
    company = serializers.ChoiceField(choices=[(code, code) for code in PUBLIC_COMPANY_CODES])
    bill_addresses = AddressInputSerializer(many=True)
    ship_same_as_bill = serializers.BooleanField(default=True)
    ship_addresses = AddressInputSerializer(many=True, required=False, default=list)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        errors = _address_lists_errors(
            attrs.get("bill_addresses"), attrs.get("ship_addresses"), attrs.get("ship_same_as_bill", True)
        )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class CustomerSubmitSerializer(_PublicSubmitMixin, _CustomerFieldsMixin, _RegistrationFieldsSerializer):
    # The public form offers B2B and B2C; the other types are the approver's.
    customer_type = serializers.ChoiceField(
        choices=[(CustomerType.B2B, "B2B"), (CustomerType.B2C, "B2C")], default=CustomerType.B2B
    )


class VendorSubmitSerializer(_PublicSubmitMixin, _VendorFieldsMixin, _RegistrationFieldsSerializer):
    bank_accounts = BankAccountInputSerializer(many=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        banks = attrs.get("bank_accounts") or []
        if not banks:
            raise serializers.ValidationError({"bank_accounts": "Give at least one bank account."})
        if len(banks) > MAX_BANK_ACCOUNTS:
            raise serializers.ValidationError({"bank_accounts": f"At most {MAX_BANK_ACCOUNTS} bank accounts."})
        for bank in banks:
            bank["sap_bank_code"] = ""  # the approver's field, never the public's
        return attrs


# ---------------------------------------------------------------------------
# Manager (approver) fields
# ---------------------------------------------------------------------------


class ManagerFieldsSerializer(serializers.Serializer):
    """The SAP master data set on the approval screen (approvals.html's SAP tab)."""

    card_code_prefix = serializers.CharField(max_length=10, required=False, allow_blank=True)
    bp_group_code = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    bp_group_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    payment_terms_code = serializers.IntegerField(required=False, allow_null=True, min_value=-1)
    payment_terms_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    sales_employee_code = serializers.IntegerField(required=False, allow_null=True, min_value=-1)
    sales_employee_name = serializers.CharField(max_length=155, required=False, allow_blank=True)
    control_account = serializers.CharField(max_length=15, required=False, allow_blank=True)
    control_account_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    credit_limit = serializers.DecimalField(
        max_digits=18, decimal_places=2, min_value=Decimal("0"), required=False
    )
    main_group = serializers.CharField(max_length=50, required=False, allow_blank=True)
    chain = serializers.CharField(max_length=50, required=False, allow_blank=True)
    sap_currency = serializers.ChoiceField(choices=Currency.choices, required=False, allow_blank=True)
    territory = serializers.CharField(max_length=100, required=False, allow_blank=True)
    manager_notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)

    MANAGER_FIELDS = (
        "card_code_prefix",
        "bp_group_code",
        "bp_group_name",
        "payment_terms_code",
        "payment_terms_name",
        "sales_employee_code",
        "sales_employee_name",
        "control_account",
        "control_account_name",
        "credit_limit",
        "main_group",
        "chain",
        "sap_currency",
        "territory",
        "manager_notes",
    )

    def validate_card_code_prefix(self, value):
        value = upper(value)
        if value and not CARD_CODE_PREFIX_RE.match(value):
            raise serializers.ValidationError("A card-code prefix is 1–10 letters or digits.")
        return value

    def validate_control_account(self, value):
        return (value or "").strip()


# ---------------------------------------------------------------------------
# Internal edit (PATCH) and actions
# ---------------------------------------------------------------------------


class _EditMixin(ManagerFieldsSerializer):
    """Everything a verifier may correct. Lists, when sent, replace what is there."""

    addresses = AddressInputSerializer(many=True, required=False)
    ship_same_as_bill = serializers.BooleanField(required=False)

    def validate_addresses(self, value):
        for index, address in enumerate(value):
            if not address.get("address_type"):
                raise serializers.ValidationError({index: {"address_type": "Say whether it is billing or shipping."}})
        bill = [a for a in value if a["address_type"] == AddressType.BILL_TO]
        ship = [a for a in value if a["address_type"] == AddressType.SHIP_TO]
        errors = _address_lists_errors(bill, ship, same_as_bill=True)
        if errors:
            raise serializers.ValidationError(next(iter(errors.values())))
        return value


class CustomerEditSerializer(_EditMixin, _CustomerFieldsMixin, _RegistrationFieldsSerializer):
    customer_type = serializers.ChoiceField(choices=CustomerType.choices, required=False)


class VendorEditSerializer(_EditMixin, _VendorFieldsMixin, _RegistrationFieldsSerializer):
    bank_accounts = BankAccountInputSerializer(many=True, required=False)

    def validate_bank_accounts(self, value):
        if not value:
            raise serializers.ValidationError("Keep at least one bank account.")
        if len(value) > MAX_BANK_ACCOUNTS:
            raise serializers.ValidationError(f"At most {MAX_BANK_ACCOUNTS} bank accounts.")
        return value


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000)


class VerifySerializer(serializers.Serializer):
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class BankCodeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    sap_bank_code = serializers.CharField(max_length=30, allow_blank=True)

    def validate_sap_bank_code(self, value):
        return upper(value)


class ApproveSerializer(ManagerFieldsSerializer):
    """The approver's SAP fields, the vendor's bank codes, and the duplicate override."""

    bank_accounts = BankCodeSerializer(many=True, required=False, default=list)
    confirm_duplicate = serializers.BooleanField(default=False)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


class AddressSerializer(serializers.ModelSerializer):
    address_type_label = serializers.CharField(source="get_address_type_display", read_only=True)

    class Meta:
        model = PartnerAddress
        fields = [
            "id",
            "address_type",
            "address_type_label",
            "position",
            "address_name",
            "street",
            "block",
            "city",
            "zip_code",
            "state",
            "country",
            "gstin",
        ]


class BankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorBankAccount
        fields = [
            "id",
            "position",
            "bank_name",
            "branch",
            "account_number",
            "ifsc",
            "account_type",
            "swift_code",
            "is_primary",
            "sap_bank_code",
        ]


class AttachmentSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = RegistrationAttachment
        fields = [
            "id",
            "kind",
            "kind_label",
            "original_name",
            "content_type",
            "size",
            "uploaded_at",
            "sent_to_sap_at",
        ]


class EventSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = RegistrationEvent
        fields = ["id", "kind", "kind_label", "actor_name", "at", "note", "data"]


def _person(user, legacy_name: str = "") -> str:
    if user is not None:
        return user.full_name or user.email
    return legacy_name or ""


def _partner_type(obj) -> tuple[str, str]:
    if obj.family == CUSTOMER_FAMILY.key:
        return obj.customer_type, obj.get_customer_type_display()
    return obj.vendor_type, obj.get_vendor_type_display()


class RegistrationListSerializer(serializers.Serializer):
    """One row of the approvals queue (either kind)."""

    def to_representation(self, obj):
        bill = next((a for a in obj.addresses.all() if a.address_type == AddressType.BILL_TO), None)
        partner_type, partner_type_label = _partner_type(obj)
        return {
            "id": obj.id,
            "reference": obj.reference,
            "family": obj.family,
            "status": obj.status,
            "status_label": obj.get_status_display(),
            "company_code": obj.company.code,
            "card_name": obj.card_name,
            "partner_type": partner_type,
            "partner_type_label": partner_type_label,
            "industry": obj.industry,
            "contact_name": obj.contact_name,
            "mobile": obj.mobile,
            "email": obj.email,
            "gstin": obj.gstin,
            "pan": obj.pan,
            "city": bill.city if bill else "",
            "state": bill.state if bill else "",
            "submitted_at": obj.submitted_at,
            "card_code": obj.card_code,
            "sap_card_code": obj.sap_card_code,
            "sap_posting": obj.sap_posting_since is not None,
            "sap_error": obj.sap_error,
            "attachment_count": getattr(obj, "attachment_count", None),
            "legacy_portal_id": obj.legacy_portal_id,
        }


class RegistrationDetailSerializer(serializers.Serializer):
    """Everything the detail screen shows, plus what this user may do with it.

    ``actions`` follow the server's own rules (status, in-flight posting, the
    user's rights), so the buttons on screen are the ones that will work.
    """

    COMMON_FIELDS = (
        "card_name",
        "foreign_name",
        "type_of_business",
        "industry",
        "contact_first_name",
        "contact_last_name",
        "contact_title",
        "mobile",
        "email",
        "currency",
        "gstin",
        "pan",
        "has_msme",
        "msme_number",
        "msme_type",
        "msme_business_type",
        "remarks",
        "ship_same_as_bill",
        "card_code",
        "sap_card_code",
        "sap_attachment_entry",
        "sap_error",
        "sap_warning",
        "rejection_reason",
        "legacy_portal_id",
        "submitted_at",
        "verified_at",
        "approved_at",
        "rejected_at",
    ) + ManagerFieldsSerializer.MANAGER_FIELDS
    CUSTOMER_FIELDS = ("customer_type", "website", "contact_mobile", "contact_email")
    VENDOR_FIELDS = (
        "vendor_type",
        "products",
        "payment_terms_requested",
        "alt_contact",
        "tan",
        "has_tds",
        "tds_category",
        "tds_rate",
        "tds_ldc_number",
        "fssai_number",
    )

    def to_representation(self, obj):
        from .permissions import can_approve, can_reject, can_verify

        family = CUSTOMER_FAMILY if obj.family == CUSTOMER_FAMILY.key else VENDOR_FAMILY
        own = self.CUSTOMER_FIELDS if family is CUSTOMER_FAMILY else self.VENDOR_FIELDS
        data = {name: getattr(obj, name) for name in self.COMMON_FIELDS + own}
        for name in ("credit_limit", "tds_rate"):
            if name in data and data[name] is not None:
                data[name] = str(data[name])
        partner_type, partner_type_label = _partner_type(obj)
        legacy = obj.legacy_fields or {}
        user = self.context["request"].user
        posting = obj.sap_posting_since is not None
        open_ = obj.status in (RegistrationStatus.PENDING, RegistrationStatus.VERIFIED)
        data.update(
            id=obj.id,
            reference=obj.reference,
            family=obj.family,
            status=obj.status,
            status_label=obj.get_status_display(),
            company_code=obj.company.code,
            company_name=obj.company.name,
            partner_type=partner_type,
            partner_type_label=partner_type_label,
            contact_name=obj.contact_name,
            sap_posting=posting,
            sap_posting_since=obj.sap_posting_since,
            default_card_code_prefix=family.default_prefix,
            default_control_account=family.default_control_account,
            verified_by_name=_person(obj.verified_by, legacy.get("VERIFIED_BY", "")),
            approved_by_name=_person(obj.approved_by, legacy.get("APPROVED_BY", "")),
            rejected_by_name=_person(obj.rejected_by, legacy.get("REJECTED_BY", "")),
            addresses=AddressSerializer(obj.addresses.all(), many=True).data,
            attachments=AttachmentSerializer(obj.attachments.all(), many=True).data,
            events=EventSerializer(obj.events.all(), many=True).data,
            actions={
                "can_edit": open_ and not posting and can_verify(user, family),
                "can_verify": obj.status == RegistrationStatus.PENDING and not posting and can_verify(user, family),
                "can_reject": open_ and not posting and can_reject(user, family),
                "can_approve": obj.status == RegistrationStatus.VERIFIED and not posting and can_approve(user, family),
            },
        )
        if family is VENDOR_FAMILY:
            data["bank_accounts"] = BankAccountSerializer(obj.bank_accounts.all(), many=True).data
        return data
