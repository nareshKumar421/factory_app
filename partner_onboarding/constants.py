"""
Choices and the portal's domain rules, in one place.

Everything here is ported from SAP Portal (``backend_v1``); each block names the
portal file it came from. The regular expressions are the portal forms' own
(``public/register.html``, ``public/vendor-register.html``) and its payload
builder's (``services/sapServiceLayer.js``), so a registration JI accepts is one
the portal would have accepted, and SAP sees the same values.
"""

import re

from django.db import models


class RegistrationStatus(models.TextChoices):
    """The portal's two-level flow: submitted, verified by a manager, created in SAP.

    ``REJECTED`` is reachable only from ``PENDING`` or ``VERIFIED``. The portal
    let a rejection land on a partner already created in SAP (server.js:819,
    routes/vendors.js:343), leaving a SAP partner whose registration said
    "rejected".
    """

    PENDING = "PENDING", "Pending verification"
    VERIFIED = "VERIFIED", "Verified, awaiting SAP"
    APPROVED = "APPROVED", "Created in SAP"
    REJECTED = "REJECTED", "Rejected"


#: Statuses a registration can still be edited, verified or rejected in.
OPEN_STATUSES = (RegistrationStatus.PENDING, RegistrationStatus.VERIFIED)


class CustomerType(models.TextChoices):
    """register.html offers B2B / B2C; approvals.html adds the other three."""

    B2B = "B2B", "B2B"
    B2C = "B2C", "B2C"
    GOVERNMENT = "GOVERNMENT", "Government"
    DISTRIBUTOR = "DISTRIBUTOR", "Distributor"
    RETAILER = "RETAILER", "Retailer"


class VendorType(models.TextChoices):
    """vendor-register.html's three cards."""

    SUPPLIER = "SUPPLIER", "Supplier"
    SERVICE = "SERVICE", "Service provider"
    BOTH = "BOTH", "Supplier and service"


class AddressType(models.TextChoices):
    BILL_TO = "BILL_TO", "Billing"
    SHIP_TO = "SHIP_TO", "Shipping"


#: Address type → the Service Layer's ``AddressType`` value.
SAP_ADDRESS_TYPES = {AddressType.BILL_TO: "bo_BillTo", AddressType.SHIP_TO: "bo_ShipTo"}


class AttachmentKind(models.TextChoices):
    """The upload slots on the two forms."""

    PAN = "PAN", "PAN card"
    AADHAAR = "AADHAAR", "Aadhaar card"
    CHEQUE = "CHEQUE", "Cancelled cheque"
    GST = "GST", "GST certificate"
    MSME = "MSME", "MSME / Udyam certificate"
    FSSAI = "FSSAI", "FSSAI licence"
    OTHER = "OTHER", "Other document"


#: Slots that take several files; every other slot takes one.
MULTI_FILE_KINDS = frozenset({AttachmentKind.OTHER, AttachmentKind.FSSAI})

#: What SAP receives in Attachments2. The portal uploaded the keys
#: ``pan, cheque, gst, msme, other`` (sapServiceLayer.js:470): FSSAI arrived on
#: the vendor form later and was simply never added to that list, so it is sent
#: here. Aadhaar never reached SAP from the portal and still does not — it is a
#: national identity document, kept in JI behind the view right only.
KINDS_SENT_TO_SAP = frozenset(
    {
        AttachmentKind.PAN,
        AttachmentKind.CHEQUE,
        AttachmentKind.GST,
        AttachmentKind.MSME,
        AttachmentKind.FSSAI,
        AttachmentKind.OTHER,
    }
)


class EventKind(models.TextChoices):
    SUBMITTED = "SUBMITTED", "Submitted"
    EDITED = "EDITED", "Edited"
    VERIFIED = "VERIFIED", "Verified"
    REJECTED = "REJECTED", "Rejected"
    APPROVED = "APPROVED", "Approved"
    SAP_CREATED = "SAP_CREATED", "Created in SAP"
    SAP_FAILED = "SAP_FAILED", "SAP refused or unreachable"
    IMPORTED = "IMPORTED", "Imported from SAP Portal"


class Currency(models.TextChoices):
    """The forms' five currencies, stored as the ISO code SAP takes.

    The portal stored the label and mapped it at posting time (``mapCurrency``,
    server.js:603); an unknown label silently became INR there.
    """

    INR = "INR", "Indian Rupee"
    USD = "USD", "US Dollar"
    EUR = "EUR", "Euro"
    GBP = "GBP", "British Pound"
    AED = "AED", "UAE Dirham"


#: ``mapCurrency`` — for the importer, which reads the portal's labels.
CURRENCY_BY_LABEL = {label: code for code, label in Currency.choices}


class Country(models.TextChoices):
    """The forms' country list, stored as the code SAP takes (``mapCountry``)."""

    IN = "IN", "India"
    US = "US", "United States"
    GB = "GB", "United Kingdom"
    AE = "AE", "UAE"
    SG = "SG", "Singapore"
    DE = "DE", "Germany"
    JP = "JP", "Japan"
    AU = "AU", "Australia"


COUNTRY_BY_NAME = {label: code for code, label in Country.choices}


class BankAccountType(models.TextChoices):
    """vendor-register.html's account types; sent to SAP as ``UserNo2``."""

    CURRENT = "Current", "Current"
    SAVINGS = "Savings", "Savings"
    CASH_CREDIT = "Cash Credit", "Cash credit"
    OVERDRAFT = "Overdraft", "Overdraft"


#: Type of business, per form (not sent to SAP; kept for the approver).
CUSTOMER_BUSINESS_TYPES = ("Company", "Individual", "Partnership", "LLP", "Proprietorship")
VENDOR_BUSINESS_TYPES = CUSTOMER_BUSINESS_TYPES + ("Trust", "Society")

#: MSME class (``U_MSME_Type``). Both forms offer these four, upper-case.
MSME_TYPES = ("MICRO", "SMALL", "MEDIUM", "LARGE")

#: MSME business type (``U_MSME_BType``). The two forms never agreed on the
#: spelling, and each posted its own to SAP, so each is kept as it was.
CUSTOMER_MSME_BUSINESS_TYPES = ("MANUFACTURING", "SERVICES", "TRADING")
VENDOR_MSME_BUSINESS_TYPES = ("Manufacturing", "Service", "Trading", "Others")

# ---------------------------------------------------------------------------
# Formats (register.html / vendor-register.html validation; buildAddrObj)
# ---------------------------------------------------------------------------
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$")
MOBILE_RE = re.compile(r"^[0-9+\s\-]{8,15}$")
# Not checked by the portal; checked here so SAP never sees a malformed value.
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
SWIFT_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
FSSAI_RE = re.compile(r"^\d{14}$")
CARD_CODE_PREFIX_RE = re.compile(r"^[A-Z0-9]{1,10}$")

# ---------------------------------------------------------------------------
# Portal defaults (server.js:730, routes/vendors.js:225, approvals.html)
# ---------------------------------------------------------------------------
CUSTOMER_CARD_CODE_PREFIX = "CUSTA"
VENDOR_CARD_CODE_PREFIX = "VENDA"
#: "SUNDRY DEBTORS GT" — the AR control account every customer starts on.
CUSTOMER_CONTROL_ACCOUNT = "1101001"
CUSTOMER_CONTROL_ACCOUNT_NAME = "SUNDRY DEBTORS GT"
#: "SUNDRY CREDITOR" — the AP control account createVendor fell back to.
VENDOR_CONTROL_ACCOUNT = "2110005"
VENDOR_CONTROL_ACCOUNT_NAME = "SUNDRY CREDITOR"

#: The only companies a public form may submit to: the three SAP knows
#: (``sap_client.registry.COMPANY_SAP_REGISTRY``). Named here on purpose so a
#: fourth company is opened to strangers by a decision, not by onboarding.
PUBLIC_COMPANY_CODES = ("JIVO_OIL", "JIVO_MART", "JIVO_BEVERAGES")

# ---------------------------------------------------------------------------
# SAP field widths the payload must respect (standard OCRD/CRD1/OCPR/OCRB
# columns). The model's max_length matches, so a value is refused on the form
# rather than by SAP — or silently cut, as the portal's ``substring`` did.
# ---------------------------------------------------------------------------
SAP_CARD_NAME_MAX = 100
SAP_ADDRESS_NAME_MAX = 50
SAP_CONTACT_NAME_MAX = 50
#: ``Notes`` (OCRD.Notes). Longer remarks are kept here and left out of the
#: payload with a warning rather than cut — the width is an assumption the
#: sandbox check in sap_client/docs/sap_portal_port.md should confirm.
SAP_NOTES_MAX = 100

# ---------------------------------------------------------------------------
# Uploads (D2: JI's 15 MB cap, pdf/jpg/jpeg/png only)
# ---------------------------------------------------------------------------
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_ATTACHMENTS_PER_SUBMISSION = 12
#: extension → (content type, magic-byte prefixes). The bytes are checked as
#: well as the name, so a renamed file of another type is refused.
ALLOWED_UPLOADS = {
    "pdf": ("application/pdf", (b"%PDF-",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
}
SAFE_CONTENT_TYPES = frozenset(content_type for content_type, _ in ALLOWED_UPLOADS.values())

# ---------------------------------------------------------------------------
# State names → SAP state codes (``mapStateCode``, sapServiceLayer.js:368).
# New registrations pick the state from SAP's own OCST list; this table is for
# the importer, whose rows carry the portal's state names.
# ---------------------------------------------------------------------------
PORTAL_STATE_CODES = {
    "Andaman and Nicobar Islands": "AN",
    "Andaman & Nicobar Islands": "AN",
    "Andhra Pradesh": "AP",
    "Arunachal Pradesh": "AR",
    "Assam": "AS",
    "Bihar": "BH",
    "Chandigarh": "CH",
    "Chhattisgarh": "CT",
    "Dadra & Nagar Haveli": "DN",
    "Daman & Diu": "DD",
    "Delhi": "DL",
    "Goa": "GA",
    "Gujarat": "GJ",
    "Haryana": "HR",
    "Himachal Pradesh": "HP",
    "Jammu & Kashmir": "JK",
    "Jammu and Kashmir": "JK",
    "Jharkhand": "JH",
    "Karnataka": "KA",
    "Kerala": "KL",
    "Ladakh": "LA",
    "Lakshadweep": "LD",
    "Madhya Pradesh": "MP",
    "Maharashtra": "MH",
    "Manipur": "MN",
    "Meghalaya": "ME",
    "Mizoram": "MZ",
    "Nagaland": "NL",
    "Odisha": "OR",
    "Orissa": "OR",
    "Puducherry": "PY",
    "Pondicherry": "PY",
    "Punjab": "PB",
    "Rajasthan": "RJ",
    "Sikkim": "SK",
    "Tamil Nadu": "TN",
    "Telangana": "TG",
    "Tripura": "TR",
    "Uttar Pradesh": "UP",
    "Uttarakhand": "UT",
    "Uttaranchal": "UT",
    "West Bengal": "WB",
}
#: The short codes ``mapStateCode`` accepted as-is.
PORTAL_SAP_STATE_CODES = frozenset(
    "AN AP AR AS BH CH CT DD DL DN GA GJ HP HR JH JK KA KL LA LD MH MN MP MZ NL OR "
    "PB PY RJ SK TG TN TR UP UT WB".split()
)


def portal_state_code(value: str) -> str:
    """``mapStateCode``: a portal state (name or code) as the code SAP took, or ''."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 3 and text.upper() in PORTAL_SAP_STATE_CODES:
        return text.upper()
    return PORTAL_STATE_CODES.get(text, "")
