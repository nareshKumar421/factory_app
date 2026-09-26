"""
The BusinessPartners payload, built from a registration.

Ported field for field from SAP Portal's ``createCustomer`` and ``createVendor``
(``backend_v1/services/sapServiceLayer.js:662`` and ``:774``) and the
``doCreateCustomerInSAP`` / ``doApproveVendor`` callers that fed them
(server.js:610, routes/vendors.js:272):

=====================  ====================================================
SAP property           From
=====================  ====================================================
CardCode / CardName    the reserved code / the business name
CardType               ``cCustomer`` / ``cSupplier``
Currency               the approver's currency, else the form's (ISO code)
Phone1                 mobile (a vendor's as ten digits)
EmailAddress           email
Website                customer only
CreditLimit            when above zero
Notes                  remarks, when they fit (see SAP_NOTES_MAX)
GroupCode              BP group
PayTermsGrpCode        payment terms, when zero or more
SalesPersonCode        sales employee, when above zero
AttachmentEntry        the Attachments2 entry holding the documents
DebitorAccount         AR account (customer, when set); AP account (vendor,
                       ``2110005`` when blank)
ContactEmployees       one: the contact person, ``Active = tYES``
BPAddresses            every billing, then every shipping address; GSTIN +
                       ``GstType = gstRegularTDSISD`` when the address's
                       GSTIN is valid
U_Main_Group, U_Chain  when set
U_MSME, U_MSME_Type,   when registered under MSME with a number
U_MSME_BType
U_Fssai                vendor, when given
BPFiscalTaxIDCollection  PAN as ``TaxId0`` on the first billing address
BPBankAccounts         vendor: BankCode, AccountNo, Branch, AccountName (the
                       bank's name, as the portal sent it), BICSwiftCode and
                       UserNo1 (IFSC), UserNo2 (account type), IBAN (SWIFT)
=====================  ====================================================

What changed, on purpose: values are no longer cut to fit (the forms refuse
what SAP would), a bank account without a SAP bank code stops the approval
instead of vanishing from the partner (createVendor skipped it with a log
line), and remarks too long for SAP's field are left out with a warning
rather than refused by SAP.
"""

from ..constants import (
    GSTIN_RE,
    PAN_RE,
    SAP_ADDRESS_TYPES,
    SAP_NOTES_MAX,
    AddressType,
)
from ..families import CUSTOMER_FAMILY, VENDOR_FAMILY
from ..serializers import sanitize_mobile

GST_TYPE_REGULAR = "gstRegularTDSISD"


def _address(address, sap_type: str, gstin: str) -> dict:
    line = {
        "AddressName": address.address_name,
        "AddressType": sap_type,
        "Street": address.street,
        "Block": address.block,
        "City": address.city,
        "ZipCode": address.zip_code,
        "State": address.state,
        "Country": address.country,
    }
    gstin = (gstin or "").strip().upper()
    if gstin and GSTIN_RE.match(gstin):
        line["GSTIN"] = gstin
        line["GstType"] = GST_TYPE_REGULAR
    return line


def _addresses(registration, family) -> list[dict]:
    rows = list(registration.addresses.all())
    bill = [a for a in rows if a.address_type == AddressType.BILL_TO]
    ship = [a for a in rows if a.address_type == AddressType.SHIP_TO]
    lines = []
    for index, address in enumerate(bill):
        # The first billing address carries the registration's GSTIN when its
        # own is blank (the portal's single-address rule).
        gstin = address.gstin or (registration.gstin if index == 0 else "")
        lines.append(_address(address, SAP_ADDRESS_TYPES[AddressType.BILL_TO], gstin))
    for address in ship:
        lines.append(_address(address, SAP_ADDRESS_TYPES[AddressType.SHIP_TO], address.gstin))
    if bill and not ship:
        # No shipping address on file: ship to the first billing address, as
        # both builders did. The vendor's copy kept the GSTIN, the customer's not.
        first = bill[0]
        gstin = (first.gstin or registration.gstin) if family is VENDOR_FAMILY else ""
        lines.append(_address(first, SAP_ADDRESS_TYPES[AddressType.SHIP_TO], gstin))
    return lines


def _bank_accounts(registration) -> list[dict]:
    accounts = []
    for bank in registration.bank_accounts.all():
        if not bank.account_number.strip():
            continue
        ifsc = bank.ifsc.strip().upper()
        accounts.append(
            {
                "BankCode": bank.sap_bank_code,
                "AccountNo": bank.account_number.strip(),
                "Branch": bank.branch.strip(),
                "AccountName": (bank.bank_name or registration.card_name).strip(),
                "BICSwiftCode": ifsc,
                "UserNo1": ifsc,
                "UserNo2": bank.account_type or "Current",
                "IBAN": bank.swift_code.strip(),
            }
        )
    return accounts


def build_payload(registration, family, attachment_entry=None) -> tuple[dict, list[str]]:
    """Return ``(payload, warnings)``. Assumes the approval checks have passed."""
    warnings: list[str] = []
    is_customer = family is CUSTOMER_FAMILY
    payload = {
        "CardCode": registration.card_code,
        "CardName": registration.card_name,
        "CardType": family.sap_card_type,
    }

    currency = registration.sap_currency or registration.currency
    if currency:
        payload["Currency"] = currency
    phone = registration.mobile if is_customer else sanitize_mobile(registration.mobile)
    if phone:
        payload["Phone1"] = phone
    if registration.email:
        payload["EmailAddress"] = registration.email
    if is_customer and registration.website:
        payload["Website"] = registration.website
    if registration.credit_limit and registration.credit_limit > 0:
        payload["CreditLimit"] = float(registration.credit_limit)
    remarks = (registration.remarks or "").strip()
    if remarks:
        if len(remarks) <= SAP_NOTES_MAX:
            payload["Notes"] = remarks
        else:
            warnings.append(
                f"The remarks are longer than SAP's {SAP_NOTES_MAX}-character Remarks field, so they "
                "were not sent to SAP. They stay on the registration."
            )

    if registration.bp_group_code is not None:
        payload["GroupCode"] = int(registration.bp_group_code)
    if registration.payment_terms_code is not None and registration.payment_terms_code >= 0:
        payload["PayTermsGrpCode"] = int(registration.payment_terms_code)
    if registration.sales_employee_code is not None and registration.sales_employee_code > 0:
        payload["SalesPersonCode"] = int(registration.sales_employee_code)
    if attachment_entry:
        payload["AttachmentEntry"] = int(attachment_entry)

    if is_customer:
        if registration.control_account:
            payload["DebitorAccount"] = registration.control_account
    else:
        payload["DebitorAccount"] = registration.control_account or family.default_control_account

    contact = registration.contact_name
    if contact:
        if is_customer:
            mobile = registration.contact_mobile or registration.mobile
            email = registration.contact_email or registration.email
        else:
            mobile = sanitize_mobile(registration.mobile)
            email = registration.email
        payload["ContactEmployees"] = [
            {
                "Name": contact,
                "FirstName": registration.contact_first_name,
                "LastName": registration.contact_last_name,
                "MobilePhone": mobile or "",
                "E_Mail": email or "",
                "Active": "tYES",
            }
        ]

    addresses = _addresses(registration, family)
    payload["BPAddresses"] = addresses

    if registration.main_group:
        payload["U_Main_Group"] = registration.main_group.strip()
    if registration.chain:
        payload["U_Chain"] = registration.chain.strip()
    if registration.has_msme and registration.msme_number:
        payload["U_MSME"] = registration.msme_number.strip()
        payload["U_MSME_Type"] = registration.msme_type or ""
        payload["U_MSME_BType"] = registration.msme_business_type or ""
    if not is_customer and registration.fssai_number:
        payload["U_Fssai"] = registration.fssai_number.strip().upper()

    pan = (registration.pan or "").strip().upper()
    first_bill = next((a for a in addresses if a["AddressType"] == "bo_BillTo"), None)
    if pan and PAN_RE.match(pan) and first_bill:
        # TaxId0 is the PAN in SAP B1's India localisation (CRD7). Its Address
        # must be an existing billing address's name.
        payload["BPFiscalTaxIDCollection"] = [
            {"Address": first_bill["AddressName"], "AddrType": "bo_BillTo", "TaxId0": pan}
        ]

    if not is_customer:
        banks = _bank_accounts(registration)
        if banks:
            payload["BPBankAccounts"] = banks

    return payload, warnings
