"""The SAP document browser: what SAP Portal's ``/api/sap/documents/*``,
``/payment-drafts/*`` and ``/attachments/*`` routes did, the JI way.

* Lists and detail are read through the Service Layer, as the portal read them
  (``backend_v1/routes/sap.js:2275-2344``): its ``$select`` lists were fixed
  against live SAP one refusal at a time, and a document's Service Layer shape
  already carries its lines, UDFs and India localisation (``TaxExtension``,
  ``EWayBillDetails``). Moving the fourteen types to HANA would mean fourteen new
  column mappings nobody here can check against SAP.
* HANA fills in what the Service Layer does not give — names for codes, the
  settlement, the documents the lines were copied from, the journal — in one
  batched session (``sap_client.hana.document_reader``). That half is
  decoration: when HANA cannot be read the document still opens, with a warning.
* Outgoing-payment drafts come from HANA alone (the Service Layer does not
  expose them as payments), and so does the attachment list (``ATC1``), whose
  ``AbsEntry`` + ``Line`` is the key the file service serves by.

The shaping below turns SAP's PascalCase documents into the fields the screen
shows. Each rule that came from the portal cites where.
"""

import logging
import re
from urllib.parse import quote

from sap_client.approval_status import draft_status_from_sl
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.document_reader import (
    OBJECT_LABELS,
    PAYMENT_DRAFT_APPROVAL,
    as_int,
    clean,
    extract_tds_section,
    iso_date,
    number,
)
from sap_client.service_layer.entity_client import odata_string
from sap_client.service_layer.file_service_client import guess_content_type

from .constants import (
    DOCUMENT_SUBTYPES,
    DOCUMENT_TYPES,
    DRAFT,
    IN_TRANSIT_BASE_TYPES,
    INLINE_CONTENT_TYPES,
    JOURNAL,
    MARKETING,
    OBJECT_CODE_LABELS,
    OBJECT_CODE_TYPES,
    PAYMENT,
    PAYMENT_DRAFT,
    PAYMENT_INVOICE_TYPES,
    PURCHASE_OBJECTS,
    TRANSFER,
    DocumentType,
)
from .models import SapAttachmentDownload

logger = logging.getLogger(__name__)

HANA_UNAVAILABLE = (
    "SAP HANA could not be read, so names, settlement, base documents and the journal entry are missing."
)


class AttachmentNotFound(Exception):
    """The attachment entry has no such line in SAP."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _missing(value) -> bool:
    return value is None or value == ""


def _value(source: dict, *keys):
    """First non-empty value under ``keys``, matched without regard to case."""
    if not source:
        return None
    lowered = {str(key).lower(): value for key, value in source.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if not _missing(value):
            return value
    return None


def _pick(sources, keys, fragments=(), *, udf_only=True):
    """The portal's ``setLineValue``/``rowValueLike``: the Service Layer line
    first, then the HANA line row; failing an exact column, a column whose name
    holds every fragment of one group. The fuzzy match looks only at
    user-defined (``U_``) columns, which is what it was for — their names differ
    per company schema — so a standard column can never be mistaken for one."""
    for source in sources:
        value = _value(source, *keys)
        if not _missing(value):
            return value
    for source in sources:
        for key, value in (source or {}).items():
            if _missing(value) or (udf_only and not str(key).upper().startswith("U_")):
                continue
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(all(fragment in normalized for fragment in group) for group in fragments):
                return value
    return None


def _positive(value):
    number_ = as_int(value)
    return number_ if number_ and number_ > 0 else None


def document_lines(doc: dict) -> list:
    return (doc or {}).get("DocumentLines") or (doc or {}).get("StockTransferLines") or []


def object_label(code) -> str:
    code = clean(code)
    return OBJECT_CODE_LABELS.get(code) or OBJECT_LABELS.get(code) or code


def _status(doc: dict) -> str:
    """open / closed / cancelled. A cancelled document is closed in SAP too;
    it reads as cancelled, once (portal documents.html:316-325)."""
    if clean(doc.get("Cancelled")) == "tYES" or clean(doc.get("CancelStatus")) == "csYes":
        return "cancelled"
    status = clean(doc.get("DocumentStatus"))
    return {"bost_Open": "open", "bost_Close": "closed"}.get(status, "")


def _approval_label(value) -> str:
    code = draft_status_from_sl(value)
    return PAYMENT_DRAFT_APPROVAL.get(code, clean(value)) if code else clean(value)


# ---------------------------------------------------------------------------
# Document types and lists
# ---------------------------------------------------------------------------


def build_filter(doc_type: DocumentType, filters: dict) -> str:
    """The OData ``$filter`` for a list (portal ``routes/sap.js:2286-2302``).

    Text is quoted with ``odata_string``; the portal escaped the partner name but
    built the rest by hand. Two deliberate changes: the partner search matches
    the code as well as the name, as the screen's "BP Code or Name" label always
    promised, in the case typed and in capitals (SAP's names are mostly
    capitals and the Service Layer compares case-sensitively); and "Cancelled"
    filters on ``Cancelled eq 'tYES'`` — the portal sent ``bost_Cancel``, which
    is not a document status, so SAP refused the query and the list came back
    empty.
    """
    parts = []
    number_ = filters.get("number")
    if number_ is not None:
        parts.append(f"{doc_type.number_field} eq {int(number_)}")
    partner = (filters.get("partner") or "").strip()
    if partner and doc_type.has_partner:
        terms = list(dict.fromkeys([partner, partner.upper()]))
        matches = [f"contains({field},{odata_string(term)})" for field in ("CardCode", "CardName") for term in terms]
        parts.append(f"({' or '.join(matches)})")
    if filters.get("date_from"):
        parts.append(f"{doc_type.date_field} ge {odata_string(filters['date_from'].isoformat())}")
    if filters.get("date_to"):
        parts.append(f"{doc_type.date_field} le {odata_string(filters['date_to'].isoformat())}")
    status = filters.get("status") or ""
    if status == "O":
        parts.append("DocumentStatus eq 'bost_Open'")
    elif status == "C":
        closed = "DocumentStatus eq 'bost_Close'"
        parts.append(f"{closed} and Cancelled eq 'tNO'" if "L" in doc_type.statuses else closed)
    elif status == "L":
        parts.append("Cancelled eq 'tYES'")
    return " and ".join(parts)


def list_row(doc_type: DocumentType, row: dict) -> dict:
    """One list row in this app's field names."""
    if doc_type.kind == JOURNAL:
        return {
            "doc_entry": as_int(row.get("JdtNum")),
            "doc_num": as_int(row.get("Number")),
            "doc_date": iso_date(row.get("ReferenceDate")),
            "due_date": iso_date(row.get("DueDate")),
            "memo": clean(row.get("Memo")),
            "reference": clean(row.get("Reference")),
            "reference2": clean(row.get("Reference2")),
            "reversal_date": iso_date(row.get("StornoDate")),
            "transaction_code": clean(row.get("TransactionCode")),
            "project": clean(row.get("ProjectCode")),
            "attachment_entry": None,
            "status": "",
        }
    out = {
        "doc_entry": as_int(row.get("DocEntry")),
        "doc_num": as_int(row.get("DocNum")),
        "doc_date": iso_date(row.get("DocDate")),
        "due_date": iso_date(row.get("DocDueDate") or row.get("DueDate")),
        "card_code": clean(row.get("CardCode")),
        "card_name": clean(row.get("CardName")),
        "comments": clean(row.get("Comments") or row.get("Remarks")),
        "attachment_entry": _positive(row.get("AttachmentEntry")),
        "status": _status(row),
    }
    if doc_type.kind in (MARKETING, DRAFT):
        out.update(
            total=number(row.get("DocTotal")),
            currency=clean(row.get("DocCurrency")) or "INR",
            branch_id=_positive(row.get("BPL_IDAssignedToInvoice")),
        )
    if doc_type.kind == DRAFT:
        out.update(object_code=clean(row.get("DocObjectCode")), object_label=object_label(row.get("DocObjectCode")))
    if doc_type.kind == TRANSFER:
        out.update(
            from_warehouse=clean(row.get("FromWarehouse")),
            to_warehouse=clean(row.get("ToWarehouse")),
            journal_memo=clean(row.get("JournalMemo")),
        )
    if doc_type.kind in (PAYMENT, PAYMENT_DRAFT):
        cash, transfer = number(row.get("CashSum")) or 0.0, number(row.get("TransferSum")) or 0.0
        out.update(
            currency=clean(row.get("DocCurrency")) or "INR",
            cash_sum=cash,
            transfer_sum=transfer,
            # Cheques sit on PaymentChecks lines, which a list cannot select
            # (portal 1420-1422); the detail shows them.
            total=round(cash + transfer, 2),
            total_note="Cash and transfer only; cheques are on the document",
            transfer_reference=clean(row.get("TransferReference")),
            journal_memo=clean(row.get("JournalRemarks")),
            branch_name=clean(row.get("BPLName")),
            approval_status=_approval_label(row.get("AuthorizationStatus")),
        )
    return out


def list_documents(company_code: str, doc_type: DocumentType, filters: dict) -> dict:
    top, skip = filters.get("top", 20), filters.get("skip", 0)
    rows = SAPClient(company_code=company_code).list_sap_documents(
        doc_type.key,
        select=doc_type.select,
        filter=build_filter(doc_type, filters),
        orderby=f"{doc_type.key_field} desc",
        top=top,
        skip=skip,
    )
    return {
        "type": doc_type.key,
        "label": doc_type.label,
        "kind": doc_type.kind,
        "results": [list_row(doc_type, row) for row in rows],
        "top": top,
        "skip": skip,
        # The portal offered "Next" whenever a page came back full.
        "has_more": len(rows) >= top,
    }


# ---------------------------------------------------------------------------
# Document detail
# ---------------------------------------------------------------------------


def _empty_lookups() -> dict:
    return {
        "line_rows": [],
        "header": None,
        "tds": [],
        "accounts": {},
        "sac": {},
        "dimensions": {},
        "locations": {},
        "branches": {},
        "header_names": {},
        "warehouses": {},
        "partners": [],
        "base_documents": [],
        "payment_invoices": {},
        "journal_entry": None,
        "in_transit_journal_entries": [],
        "posted_as": None,
        "journal_preview": None,
        "warnings": [],
    }


def _line_account(line: dict) -> str:
    return clean(_value(line, "AccountCode", "GLAccount", "AcctCode", "Account"))


def lookup_request(doc_type: DocumentType, doc: dict) -> dict:
    """What to ask HANA for, from the Service Layer document."""
    request = {
        "doc_entry": as_int(doc.get(doc_type.key_field)),
        "line_table": doc_type.line_table,
        "header_table": doc_type.header_table,
        "tds_table": doc_type.tds_table,
        "draft": doc_type.kind == DRAFT,
    }
    if doc_type.kind == JOURNAL:
        request["journal_trans_ids"] = [request["doc_entry"]]
        return request
    if doc_type.kind == PAYMENT:
        request["journal_created_by"] = ("46", request["doc_entry"])
        request["payment_invoices"] = [
            (PAYMENT_INVOICE_TYPES.get(clean(row.get("InvoiceType")), ""), row.get("DocEntry"))
            for row in doc.get("PaymentInvoices") or []
        ]
        request["account_codes"] = [clean(row.get("AccountCode")) for row in doc.get("PaymentAccounts") or []]
        return request

    lines = document_lines(doc)
    request.update(
        account_codes=[_line_account(line) for line in lines],
        sac_entries=[_value(line, "SACEntry", "SacEntry", "ServiceAccountingCodeEntry") for line in lines],
        warehouse_codes=[
            *(_value(line, "WarehouseCode", "Warehouse", "WhsCode") for line in lines),
            *(_value(line, "FromWarehouseCode") for line in lines),
            doc.get("FromWarehouse"),
            doc.get("ToWarehouse"),
        ],
        dimension_codes=[
            _value(line, f"CostingCode{n}" if n > 1 else "CostingCode") for line in lines for n in range(1, 6)
        ],
        location_codes=[_value(line, "LocationCode", "LocCode") for line in lines],
        branch_ids=[]
        if clean(doc.get("BPLName") or doc.get("BranchName"))
        else [_value(doc, "BPL_IDAssignedToInvoice", "BPLId", "BPLID", "BranchID")],
        sales_person=doc.get("SalesPersonCode"),
        payment_group=doc.get("PaymentGroupCode"),
        transport=doc.get("TransportationCode"),
        card_code=clean(doc.get("CardCode")),
        card_name=clean(doc.get("CardName")),
        base_refs=[
            (str(as_int(line.get("BaseType"))), line.get("BaseEntry"), clean(_value(line, "BaseRef", "BaseDocNum")))
            for line in lines
            if (as_int(line.get("BaseType")) or 0) > 0 and (as_int(line.get("BaseEntry")) or 0) > 0
        ],
        in_transit_base_type=IN_TRANSIT_BASE_TYPES.get(doc_type.object_type),
    )
    if doc_type.key == "StockTransfers":
        request["journal_created_by"] = ("67", request["doc_entry"])
    return request


def shape_line(line: dict, row: dict | None, lookups: dict) -> dict:
    """One document line: the Service Layer's values, backfilled from the HANA
    line row where the Service Layer left them out (it omits custom row UDFs in
    draft reads), then named (portal ``routes/sap.js:1496-1551``, 1999-2027)."""
    sources = [line or {}, row or {}]
    account = clean(_pick(sources, ["AccountCode", "GLAccount", "AcctCode"]))
    warehouse = clean(_pick(sources, ["WarehouseCode", "Warehouse", "WhsCode"]))
    from_warehouse = clean(_pick(sources, ["FromWarehouseCode", "FromWhsCod"]))
    location = clean(_pick(sources, ["LocationCode", "LocCode"]))
    sac_entry = _pick(sources, ["SACEntry", "SacEntry", "ServiceAccountingCodeEntry"], [["sac", "entry"]], udf_only=False)
    sac = lookups["sac"].get(str(as_int(sac_entry))) if as_int(sac_entry) is not None else None
    base_type = as_int(_pick(sources, ["BaseType", "BaseObjectType"]))
    warehouses = lookups["warehouses"]
    dimensions = []
    for n in range(1, 6):
        code = clean(_pick(sources, [f"CostingCode{n}" if n > 1 else "CostingCode", f"OcrCode{n}" if n > 1 else "OcrCode"]))
        dimensions.append({"code": code, "name": lookups["dimensions"].get(code, "") if code else ""})
    sub_account_keys = [
        "U_Sub_Account", "U_SubAccount", "U_SubAcct", "U_SubAcc", "U_Sub_Acc", "U_Sub_Accnt", "U_Sub_Acco",
    ]
    return {
        "line_num": as_int(_pick(sources, ["LineNum", "LineNumber"])),
        "item_code": clean(_pick(sources, ["ItemCode"])),
        "description": clean(_pick(sources, ["ItemDescription", "Description", "Dscription"])),
        "quantity": number(_pick(sources, ["Quantity"])),
        "uom": clean(_pick(sources, ["UoMCode", "MeasureUnit", "unitMsr"])),
        "unit_price": number(_pick(sources, ["UnitPrice", "Price"])),
        "line_total": number(_pick(sources, ["LineTotal"])),
        "tax_code": clean(_pick(sources, ["TaxCode", "VatGroup"])),
        "tax_percent": number(_pick(sources, ["TaxPercentagePerRow", "VatPrcnt"])),
        "tax_amount": number(_pick(sources, ["TaxTotal", "LineVat"])),
        "warehouse_code": warehouse,
        "warehouse_name": (warehouses.get(warehouse) or {}).get("name", "") if warehouse else "",
        "from_warehouse_code": from_warehouse,
        "from_warehouse_name": (warehouses.get(from_warehouse) or {}).get("name", "") if from_warehouse else "",
        "account_code": account,
        "account_name": lookups["accounts"].get(account, "") if account else "",
        # SACEntry is OSAC's internal key; SAP's own screen shows OSAC.ServCode.
        # Negative keys are real (imported SAC rows, 779594d).
        "sac_code": (sac or {}).get("code", ""),
        "sac_name": (sac or {}).get("name", ""),
        "location_code": location,
        # OLCT is the location master; OBPL only when OLCT has no such code.
        "location_name": (lookups["locations"].get(location) or lookups["branches"].get(location, ""))
        if location
        else "",
        "dimensions": dimensions,
        "project": clean(_pick(sources, ["ProjectCode", "Project"])),
        "base_type": str(base_type) if base_type and base_type > 0 else "",
        "base_label": OBJECT_LABELS.get(str(base_type), "") if base_type and base_type > 0 else "",
        "base_entry": _positive(_pick(sources, ["BaseEntry"])),
        "base_ref": clean(_pick(sources, ["BaseRef", "BaseDocNum"])),
        # Freight and service lines carry the quantities in UDFs; their standard
        # Quantity is 0 (7d6409e).
        "received_qty": number(
            _pick(sources, ["U_Recvd_Qty", "U_ReceivedQty", "U_Received_Qty"], [["recvd", "qty"], ["received", "qty"]])
        ),
        "dispatched_qty": number(
            _pick(sources, ["U_Disp_Qty", "U_DispatchQty", "U_Dispatch_Qty"], [["disp", "qty"], ["dispatch", "qty"]])
        ),
        "litres": number(
            _pick(sources, ["U_UNE_LTS", "U_Litres", "U_Litre", "U_LitreS"], [["u", "lts"], ["litre"], ["liter"]])
        ),
        "bilty_no": clean(
            _pick(sources, ["U_BilltyNumber", "U_BiltyNumber", "U_BilltyNo", "U_BiltyNo"], [["billty"], ["bilty"]])
        ),
        "ar_no": clean(_pick(sources, ["U_ARNO", "U_Arno"], [["arno"]])),
        "sub_account": clean(
            _pick(sources, sub_account_keys, [["sub", "account"], ["sub", "acct"], ["sub", "acnt"], ["sub", "acc"]])
        ),
        "udf_card_code": clean(_pick(sources, ["U_CardCode", "U_CustomerCode"], [["card", "code"], ["customer", "code"]])),
        "purpose": clean(_pick(sources, ["U_Purpose"], [["purpose"]])),
        "remarks": clean(_pick(sources, ["U_Remarks", "FreeText", "FreeTxt", "Remarks"])),
    }


def _match_rows(lines: list, rows: list) -> list:
    """Pair each Service Layer line with its HANA row: by LineNum, else by
    position (portal ``routes/sap.js:1507-1511``)."""
    by_line = {as_int(_value(row, "LineNum")): row for row in rows}
    pairs = []
    for index, line in enumerate(lines):
        line_num = as_int(line.get("LineNum", line.get("LineNumber", index)))
        pairs.append(by_line.get(line_num) or (rows[index] if index < len(rows) else None))
    return pairs


def _best_address(addresses: list, codes) -> dict | None:
    """The CRD1 row a document means: its own address code first (in the order
    given), then any address that carries a GSTIN. Ordering by "has a GSTIN"
    first showed an unrelated address's GSTIN and state — an MP number on a
    Delhi shipment (5b5f762)."""
    codes = [clean(code) for code in codes]

    def rank(address):
        name = clean(address.get("address"))
        position = next((i for i, code in enumerate(codes) if code and name == code), len(codes))
        return (position, 0 if clean(address.get("gstin")) else 1)

    ranked = sorted(addresses or [], key=rank)
    return ranked[0] if ranked else None


def _choose_partner(partners: list, card_code: str, expected_type: str) -> dict | None:
    """The partner by code; with no code (a draft SAP left without one), the
    exact-name match of the expected type, active first (portal 1850-1865)."""
    if card_code:
        return next((p for p in partners if p["card_code"] == card_code), None)
    ranked = sorted(
        partners, key=lambda p: (p["card_type"] != expected_type, p["valid_for"] != "Y")
    )
    return ranked[0] if ranked else None


def _is_vendor_counterparty(doc_type: DocumentType, doc: dict, partners: list) -> bool:
    """A purchase document, or anything whose partner is a vendor. The object
    type decides first: a draft with no CardCode once failed the vendor lookup
    and showed our own branch's GSTIN as the supplier's (portal 1873-1887)."""
    if doc_type.purchase:
        return True
    obj = clean(_value(doc, "DocObjectCode", "ObjType", "ObjectType"))
    if obj in PURCHASE_OBJECTS or OBJECT_CODE_TYPES.get(obj) in PURCHASE_OBJECTS:
        return True
    card_code = clean(doc.get("CardCode"))
    if not card_code:
        return False
    partner = next((p for p in partners if p["card_code"] == card_code), None)
    return bool(partner and partner["card_type"] == "S")


def _address_text(doc: dict) -> str:
    text = clean(_value(doc, "Address", "Address2"))
    text = re.sub(r"\r\n?|\n", ", ", text)
    text = re.sub(r"\s*,\s*,+", ", ", text)
    return re.sub(r"^,\s*|,\s*$", "", text)


def _partner_ship_from(doc: dict, partner: dict | None) -> dict | None:
    """The counterparty as ship-from: its address master row for the document's
    own ship-from / ship-to code, then pay-to (an A/P invoice often carries only
    PayToCode), then any address with a GSTIN (portal 1807-1849, 5b5f762)."""
    card_name = clean(doc.get("CardName"))
    card_code = clean(doc.get("CardCode")) or (partner or {}).get("card_code", "")
    if not card_code and not card_name:
        return None
    gstin = state = address = ""
    if partner:
        row = _best_address(partner["addresses"], [_value(doc, "ShipFrom", "ShipToCode"), doc.get("PayToCode")])
        if row:
            gstin, state = clean(row.get("gstin")), clean(row.get("state"))
            address = ", ".join(
                part for part in (clean(row.get(k)) for k in ("street", "block", "city", "state", "zip", "country")) if part
            )
    if not address:
        address = _address_text(doc)
    if not gstin and not address and not card_name:
        return None
    return {"code": card_name or card_code, "name": "", "gstin": gstin, "branch": "", "state": state, "address": address}


def _ship_from(doc_type, doc, lines, lookups, purchase) -> list | None:
    """Vendor for purchase documents, our warehouse otherwise (portal 1888-1937).

    A purchase document whose party cannot be resolved gets none: falling back
    to the warehouse would print our own branch GSTIN as the supplier's. A
    transfer ships from its *from* warehouse (the portal used the lines' target
    warehouse there)."""
    partners = lookups["partners"]
    card_code = clean(doc.get("CardCode"))
    if purchase:
        vendor = _partner_ship_from(doc, _choose_partner(partners, card_code, "S"))
        return [vendor] if vendor else None
    if doc_type.kind == TRANSFER:
        codes = list(dict.fromkeys(filter(None, [line["from_warehouse_code"] for line in lines]))) or [
            clean(doc.get("FromWarehouse"))
        ]
    else:
        codes = list(dict.fromkeys(filter(None, [line["warehouse_code"] for line in lines])))
    codes = [code for code in codes if code]
    if codes:
        blank = {"name": "", "gstin": "", "branch": "", "state": "", "address": ""}
        return [lookups["warehouses"].get(code) or {"code": code, **blank} for code in codes]
    fallback = _partner_ship_from(doc, _choose_partner(partners, card_code, "C"))
    return [fallback] if fallback else None


def _party(doc: dict, header_table: str | None, partners: list) -> dict:
    """The counterparty's own GSTIN, PAN and state. ``VATRegNum`` is OUR
    branch's GST number, so showing it beside the customer misreported who was
    billed (portal 1682-1720): the address the document uses (CRD1) first, then
    the partner master, then the e-way bill's billed party."""
    card_code = clean(doc.get("CardCode"))
    gstin = state = ""
    if card_code:
        partner = _choose_partner(partners, card_code, "")
        if partner:
            row = _best_address(partner["addresses"], [_value(doc, "ShipToCode", "PayToCode")])
            if row:
                gstin, state = clean(row.get("gstin")), clean(row.get("state"))
            if not gstin:
                gstin = partner["lic_trad_num"]
        if not gstin:
            ewb = doc.get("EWayBillDetails") or {}
            from_ewb = clean(ewb.get("BillFromGSTIN") if header_table in ("OPCH", "ORPC") else ewb.get("BillToGSTIN"))
            if from_ewb and from_ewb != clean(doc.get("VATRegNum")):
                gstin = from_ewb
    return {
        "party_gstin": gstin,
        "party_state": state,
        "party_pan": clean((doc.get("TaxExtension") or {}).get("TaxId0")),
        "branch_gstin": clean(doc.get("VATRegNum")),
    }


def _tds(rows: list) -> list:
    """Withholding tax per code. OWHT.OffclCode is SAP's statutory section (what
    Form 26Q shows); the tax name is parsed only when it is empty (portal 1615-1650)."""
    return [
        {
            "code": clean(row.get("code")),
            "name": clean(row.get("name")),
            "section": clean(row.get("section")) or extract_tds_section(row.get("name")),
            "rate": number(row.get("rate")),
            "amount": number(row.get("amount")),
            "taxable": number(row.get("taxable")),
        }
        for row in rows
    ]


def _journal_from_service_layer(doc: dict) -> dict:
    """A journal entry from its Service Layer shape, when HANA cannot be read."""
    lines = []
    for index, line in enumerate(doc.get("JournalEntryLines") or [], start=1):
        debit, credit = number(line.get("Debit")) or 0.0, number(line.get("Credit")) or 0.0
        lines.append(
            {
                "line_id": as_int(line.get("Line_ID")) if line.get("Line_ID") is not None else index,
                "account": clean(line.get("AccountCode")),
                "account_name": "",
                "short_name": clean(line.get("ShortName")),
                "debit": debit,
                "credit": credit,
                "contra_account": clean(line.get("ContraAccount")),
                "line_memo": clean(line.get("LineMemo")),
                "cost_centers": [
                    clean(line.get(f"CostingCode{n}" if n > 1 else "CostingCode")) for n in range(1, 6)
                ],
            }
        )
    return {
        "trans_id": as_int(doc.get("JdtNum")),
        "number": as_int(doc.get("Number")),
        "preview": False,
        "ref_date": iso_date(doc.get("ReferenceDate")),
        "due_date": iso_date(doc.get("DueDate")),
        "tax_date": iso_date(doc.get("TaxDate")),
        "memo": clean(doc.get("Memo")),
        "base_ref": clean(doc.get("Reference")),
        "trans_type": "30",
        "trans_type_label": "Journal Entry",
        "total_debit": round(sum(line["debit"] for line in lines), 2),
        "total_credit": round(sum(line["credit"] for line in lines), 2),
        "lines": lines,
    }


def _base(doc_type: DocumentType) -> dict:
    return {
        "type": doc_type.key,
        "label": doc_type.label,
        "kind": doc_type.kind,
        "header": {},
        "totals": {},
        "lines": [],
        "tds": [],
        "tds_section": "",
        "ship_from": None,
        "base_documents": [],
        "attachment_entry": None,
        "journal_entry": None,
        "in_transit_journal_entries": [],
        "journal_preview": None,
        "posted_as": None,
        "payment": None,
        "warnings": [],
    }


def shape_journal(doc_type: DocumentType, doc: dict, lookups: dict) -> dict:
    out = _base(doc_type)
    out["header"] = {
        "doc_entry": as_int(doc.get("JdtNum")),
        "doc_num": as_int(doc.get("Number")),
        "doc_date": iso_date(doc.get("ReferenceDate")),
        "due_date": iso_date(doc.get("DueDate")),
        "tax_date": iso_date(doc.get("TaxDate")),
        "memo": clean(doc.get("Memo")),
        "reference": clean(doc.get("Reference")),
        "reference2": clean(doc.get("Reference2")),
        "reference3": clean(doc.get("Reference3")),
        "reversal_date": iso_date(doc.get("StornoDate")),
        "transaction_code": clean(doc.get("TransactionCode")),
        "project": clean(doc.get("ProjectCode")),
        "currency": "INR",
    }
    out["journal_entry"] = lookups["journal_entry"] or _journal_from_service_layer(doc)
    return out


def shape_payment(doc_type: DocumentType, doc: dict, lookups: dict) -> dict:
    """An outgoing payment from its Service Layer shape (``VendorPayments``)."""
    out = _base(doc_type)
    checks = [
        {
            "check_number": clean(row.get("CheckNumber")),
            "bank_code": clean(row.get("BankCode")),
            "due_date": iso_date(row.get("DueDate")),
            "check_sum": number(row.get("CheckSum")),
        }
        for row in doc.get("PaymentChecks") or []
    ]
    names = lookups["accounts"]
    accounts = [
        {
            "account_code": clean(row.get("AccountCode")),
            "account_name": clean(row.get("AccountName")) or names.get(clean(row.get("AccountCode")), ""),
            "description": clean(row.get("Decription") or row.get("Description")),
            "sum_paid": number(row.get("SumPaid")),
            "gross_amount": number(row.get("GrossAmount")),
            "tax_code": clean(row.get("VatGroup")),
            "cost_centers": [
                clean(row.get("ProfitCenter" if n == 1 else f"ProfitCenter{n}")) for n in range(1, 6)
            ],
            "section": "",
            "project": clean(row.get("ProjectCode")),
        }
        for row in doc.get("PaymentAccounts") or []
    ]
    invoices = []
    for row in doc.get("PaymentInvoices") or []:
        inv_type = PAYMENT_INVOICE_TYPES.get(clean(row.get("InvoiceType")), "")
        resolved = lookups["payment_invoices"].get(f"{inv_type}-{as_int(row.get('DocEntry'))}") or {}
        invoices.append(
            {
                "doc_entry": as_int(row.get("DocEntry")),
                "doc_num": resolved.get("doc_num") or as_int(row.get("DocEntry")),
                "doc_date": resolved.get("doc_date"),
                "invoice_type": OBJECT_LABELS.get(inv_type) or clean(row.get("InvoiceType")).replace("it_", ""),
                "sum_applied": number(row.get("SumApplied")),
                "discount_percent": number(row.get("DiscountPercent")),
                "total_discount": number(row.get("TotalDiscount")),
                "doc_total": resolved.get("doc_total"),
            }
        )
    cash = number(doc.get("CashSum")) or 0.0
    transfer = number(doc.get("TransferSum")) or 0.0
    cheques = round(sum(row["check_sum"] or 0.0 for row in checks), 2)
    credit = round(sum(number(row.get("CreditSum")) or 0.0 for row in doc.get("PaymentCreditCards") or []), 2)
    currency = clean(doc.get("DocCurrency")) or "INR"
    out["header"] = {
        "doc_entry": as_int(doc.get("DocEntry")),
        "doc_num": as_int(doc.get("DocNum")),
        "doc_date": iso_date(doc.get("DocDate")),
        "due_date": iso_date(doc.get("DueDate")),
        "tax_date": iso_date(doc.get("TaxDate")),
        "card_code": clean(doc.get("CardCode")),
        "card_name": clean(doc.get("CardName")),
        "party_role": "vendor",
        "bill_to": clean(doc.get("Address")),
        "currency": currency,
        "comments": clean(doc.get("Remarks")),
        "journal_memo": clean(doc.get("JournalRemarks")),
        "branch_id": _positive(doc.get("BPLID")),
        "branch_name": clean(doc.get("BPLName")),
        "project": clean(doc.get("ProjectCode")),
        "reference": clean(doc.get("Reference1")),
        "status": "cancelled" if clean(doc.get("Cancelled")) == "tYES" else "",
        "approval_status": _approval_label(doc.get("AuthorizationStatus")),
    }
    out["payment"] = {
        "cash_account": clean(doc.get("CashAccount")),
        "cash_sum": cash,
        "check_account": clean(doc.get("CheckAccount")),
        "check_sum": cheques,
        "credit_sum": credit,
        "transfer_account": clean(doc.get("TransferAccount")),
        "transfer_sum": transfer,
        "transfer_date": iso_date(doc.get("TransferDate")),
        "transfer_reference": clean(doc.get("TransferReference")),
        "counter_reference": clean(doc.get("CounterReference")),
        "on_account_sum": number(doc.get("OpenBalance")),
        "wt_account": "",
        "wt_amount": number(doc.get("WTAmount")),
        "wt_rate": None,
        "payment_mode": clean(doc.get("U_Pymnt_Mode")),
        "accounts": accounts,
        "invoices": invoices,
        "checks": checks,
    }
    out["totals"] = {
        "currency": currency,
        "total": round(cash + transfer + cheques + credit, 2),
        "withholding": number(doc.get("WTAmount")),
    }
    out["attachment_entry"] = _positive(doc.get("AttachmentEntry"))
    out["journal_entry"] = lookups["journal_entry"]
    return out


def shape_document(doc_type: DocumentType, doc: dict, lookups: dict) -> dict:
    """A document as the screen shows it: header facts, lines with names,
    totals, partner and ship-from, base documents and the journal."""
    if doc_type.kind == JOURNAL:
        return shape_journal(doc_type, doc, lookups)
    if doc_type.kind == PAYMENT:
        return shape_payment(doc_type, doc, lookups)

    out = _base(doc_type)
    sl_lines = document_lines(doc)
    rows = lookups["line_rows"]
    lines = [shape_line(line, row, lookups) for line, row in zip(sl_lines, _match_rows(sl_lines, rows))]
    partners = lookups["partners"]
    purchase = _is_vendor_counterparty(doc_type, doc, partners)
    names = lookups["header_names"]
    branch_id = _positive(_value(doc, "BPL_IDAssignedToInvoice", "BPLId", "BPLID", "BranchID"))
    hana_header = lookups["header"] or {}
    draft = doc_type.kind == DRAFT
    object_code = clean(doc.get("DocObjectCode"))

    header = {
        "doc_entry": as_int(doc.get("DocEntry")),
        "doc_num": as_int(doc.get("DocNum")),
        "doc_date": iso_date(doc.get("DocDate")),
        "due_date": iso_date(doc.get("DocDueDate") or doc.get("DueDate")),
        "tax_date": iso_date(doc.get("TaxDate")),
        "num_at_card": clean(doc.get("NumAtCard")),
        "card_code": clean(doc.get("CardCode")),
        "card_name": clean(doc.get("CardName")),
        "party_role": "vendor" if purchase else "customer",
        **_party(doc, doc_type.header_table, partners),
        # SAP returns codes for these; -1 is "none" (portal 1659-1680).
        "sales_person": names.get("sales_person", ""),
        "payment_terms": names.get("payment_terms", ""),
        "shipping_type": names.get("shipping_type", ""),
        "branch_id": branch_id,
        "branch_name": clean(doc.get("BPLName") or doc.get("BranchName"))
        or (lookups["branches"].get(str(branch_id), "") if branch_id else ""),
        "control_account": clean(doc.get("ControlAccount")),
        "ship_to_code": clean(doc.get("ShipToCode")),
        "pay_to_code": clean(doc.get("PayToCode")),
        "reference": clean(doc.get("Reference1")),
        "reference2": clean(doc.get("Reference2")),
        "period": clean(doc.get("PeriodIndicator")),
        "created_on": iso_date(doc.get("CreationDate")),
        "created_time": clean(doc.get("DocTime")),
        "currency": clean(doc.get("DocCurrency")) or "INR",
        "status": _status(doc),
        "subtype": DOCUMENT_SUBTYPES.get(clean(doc.get("DocumentSubType")), ""),
        "draft_key": _positive(doc.get("DraftKey")),
        "object_code": object_code if draft else "",
        "object_label": object_label(object_code) if draft else "",
        "comments": clean(doc.get("Comments")),
        "journal_memo": clean(doc.get("JournalMemo")),
        "bill_to": clean(doc.get("Address")),
        "ship_to": clean(doc.get("Address2")),
        # SAP's own "Original Ref. No." (India localisation) — what the credit
        # note form and GST reports show; the manual UDFs only when it is empty
        # (4bc73ef).
        "original_ref_no": clean(_value(doc, "OriginalRefNo", "U_OgRefNo", "U_OGRefNo", "U_OgRef", "OriginalRef")),
        "original_ref_date": iso_date(_value(doc, "OriginalRefDate", "U_OgRefDate", "U_OGRefDate", "U_OgDate")),
        "from_warehouse": clean(doc.get("FromWarehouse")),
        "from_warehouse_name": (lookups["warehouses"].get(clean(doc.get("FromWarehouse"))) or {}).get("name", ""),
        "to_warehouse": clean(doc.get("ToWarehouse")),
        "to_warehouse_name": (lookups["warehouses"].get(clean(doc.get("ToWarehouse"))) or {}).get("name", ""),
    }

    total = number(doc.get("DocTotal"))
    vat = number(doc.get("VatSum")) or 0.0
    rounding = number(doc.get("RoundingDiffAmount")) or 0.0
    withholding = number(doc.get("WTAmount")) or 0.0
    # A draft is not posted: nothing can have been paid against it.
    paid = None if draft or not hana_header else number(hana_header.get("paid_to_date"))
    out["totals"] = {
        "currency": header["currency"],
        # DocTotal is net of the tax withheld (the draft journal's partner leg
        # is DocTotal and its TDS leg the rest), so the withholding goes back in.
        "net": round(total - vat - rounding + withholding, 2) if total is not None else None,
        "discount": number(doc.get("TotalDiscount")),
        "tax": number(doc.get("VatSum")),
        "withholding": number(doc.get("WTAmount")),
        "down_payment": number(doc.get("DownPaymentAmount")),
        "rounding": number(doc.get("RoundingDiffAmount")),
        "total": total,
        "paid_to_date": paid,
        "balance_due": round(total - paid, 2) if paid is not None and total is not None else None,
        "gross_profit": None if draft else number(hana_header.get("gross_profit")),
    }
    out["header"] = header
    out["lines"] = lines
    out["tds"] = _tds(lookups["tds"])
    out["tds_section"] = ", ".join(dict.fromkeys(row["section"] for row in out["tds"] if row["section"]))
    out["ship_from"] = _ship_from(doc_type, doc, lines, lookups, purchase)
    out["base_documents"] = [
        {key: value for key, value in base.items() if key != "trans_id"} for base in lookups["base_documents"]
    ]
    out["attachment_entry"] = _positive(doc.get("AttachmentEntry"))
    out["journal_entry"] = lookups["journal_entry"]
    out["in_transit_journal_entries"] = lookups["in_transit_journal_entries"]
    out["journal_preview"] = lookups["journal_preview"]
    out["posted_as"] = lookups["posted_as"]
    return out


def _warning_texts(labels) -> list[str]:
    return [f"Could not read the {label} from SAP." for label in labels]


def document_detail(company_code: str, doc_type: DocumentType, doc_entry: int) -> dict | None:
    """One document, shaped for the screen; None when SAP has no such document."""
    if doc_type.kind == PAYMENT_DRAFT:
        return payment_draft(company_code, doc_entry)
    client = SAPClient(company_code=company_code)
    doc = client.get_sap_document(doc_type.key, doc_entry)
    if doc is None:
        return None
    warnings = []
    try:
        lookups = client.sap_document_lookups(**lookup_request(doc_type, doc))
    except (SAPConnectionError, SAPDataError) as e:
        logger.warning("SAP document %s(%s) opened without HANA: %s", doc_type.key, doc_entry, e)
        lookups = _empty_lookups()
        warnings.append(HANA_UNAVAILABLE)
    shaped = shape_document(doc_type, doc, lookups)
    shaped["warnings"] = [*warnings, *_warning_texts(lookups.get("warnings") or [])]
    return shaped


def payment_draft(company_code: str, doc_entry: int) -> dict | None:
    """An outgoing-payment draft (OPDF) from HANA, in the detail shape."""
    found = SAPClient(company_code=company_code).sap_payment_draft(doc_entry)
    if found is None:
        return None
    out = _base(DOCUMENT_TYPES["PaymentDrafts"])
    out.update({key: value for key, value in found.items() if key != "warnings"})
    out["warnings"] = _warning_texts(found.get("warnings") or [])
    return out


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def attachment_lines(company_code: str, abs_entry: int) -> list[dict]:
    return SAPClient(company_code=company_code).sap_attachment_lines(abs_entry)


def inline_content_type(content_type: str, file_name: str) -> str | None:
    """The type to show a file under in a browser tab, or None to download it.

    PDFs, pictures and plain text open in a tab (by SAP's type, or by the
    name when the file service said only ``octet-stream``); anything else
    downloads. The portal also opened ``text/html`` and ``image/svg+xml``
    inline, which from this app's origin would run whatever script the file
    carried."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in INLINE_CONTENT_TYPES:
        return base
    if base in ("", "application/octet-stream"):
        guessed = guess_content_type(file_name)
        return guessed if guessed in INLINE_CONTENT_TYPES else None
    return None


def content_disposition(file_name: str, inline: bool) -> str:
    """A download header that is always latin-1 safe (RFC 6266 / 5987).

    An ASCII ``filename=`` for old clients, plus a UTF-8 ``filename*=`` for
    any name outside ASCII — scanner apps put U+202F, en dashes and ₹ in names,
    and 979 SAP attachments could not be opened through the file server's own
    header (``backend_v1/docs/file-server-unicode-filenames.md``)."""
    name = re.sub(r"[\x00-\x1f\x7f]", " ", file_name or "").replace("/", "_").replace("\\", "_").strip()
    name = name or "attachment"
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "'")
    header = f'{"inline" if inline else "attachment"}; filename="{ascii_name}"'
    if not name.isascii():
        header += f"; filename*=UTF-8''{quote(name, safe='')}"
    return header


def fetch_attachment(company, user, abs_entry: int, line: int) -> dict:
    """The file of one ATC1 line, fetched from the SAP file service by entry and
    line (the name lookup is only the fallback: short names like ``1825.pdf``
    collide across documents). Recorded as downloaded once SAP served it."""
    client = SAPClient(company_code=company.code)
    match = next((row for row in client.sap_attachment_lines(abs_entry) if row["line"] == line), None)
    if match is None:
        raise AttachmentNotFound(f"Attachment {abs_entry} has no file on line {line} in SAP.")
    served = client.download_attachment(abs_entry, line, match["file_name"])
    file_name = match["file_name"] or served.get("file_name") or f"attachment-{abs_entry}-{line}"
    data = served.get("data") or b""
    content_type = (served.get("content_type") or guess_content_type(file_name)).split(";", 1)[0].strip()
    SapAttachmentDownload.objects.create(
        company=company,
        abs_entry=abs_entry,
        line=line,
        file_name=file_name[:300],
        content_type=content_type[:100],
        size_bytes=len(data),
        created_by=user,
    )
    return {"data": data, "file_name": file_name, "content_type": content_type}
