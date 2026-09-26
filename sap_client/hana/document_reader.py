"""HANA reads behind the SAP document browser ported from SAP Portal.

SAP Portal (``backend_v1/routes/sap.js`` ~1427–2665) read a document through the
Service Layer and then filled in what the Service Layer leaves out with a dozen
HANA helpers, each opening its own connection and several of them querying once
per line or per tax code. This reader does the same reads on **one connection
per screen**, each lookup batched across every line of the document (``IN``
lists), so the number of statements does not grow with the number of lines:

* ``document_lookups`` — the portal's ``enrichDocumentLineFields``,
  ``enrichDocumentGlNames``, ``enrichDocumentSacCodes``, ``enrichDocumentTds``,
  ``enrichBaseDocumentAttachments``, ``enrichDocumentHeaderNames``,
  ``enrichDocumentBranchName``, ``enrichWarehouseNames``,
  ``enrichLocationNames``, ``enrichDimensionNames``, the linked journal entry and,
  for a draft, ``buildDraftJournalEntryFromHana``.
* ``attachment_lines`` — the ``ATC1`` rows of one attachment entry.
* ``payment_draft`` — ``fetchPaymentDraft``: an outgoing-payment draft (OPDF)
  assembled from HANA, which the Service Layer does not expose as a payment.

Only the schema and table names from the fixed sets below are interpolated;
every value is bound with ``?``. A connect failure raises ``SAPConnectionError``.
The document lookups are decoration — the Service Layer already returned the
document — so a statement that fails there is logged, named in ``warnings`` and
skipped, the way the portal's helpers each caught their own error. The ATC1 and
payment-draft reads are the screen itself and raise ``SAPDataError``.

Linked journal entries are found by key, not by the portal's text search:
``fetchJournalEntryByReference`` matched any journal whose memo or references
merely *contained* the document's number (``LIKE '%123%'``), so it could show an
unrelated entry. Here a posted document's journal is its header ``TransId``; a
transfer or payment, which has no marketing-document header table, uses
``OJDT.TransType`` + ``OJDT.CreatedBy`` (the source document's DocEntry); the
"in transit" journal of an invoice is the journal of the GRPO / delivery its lines
were copied from; and a draft that has been added is found by ``draftKey`` on the
document it became.
"""

import logging
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from hdbcli import dbapi

from ..exceptions import SAPConnectionError, SAPDataError
from .connection import HanaConnection
from .finance_reader import TRANS_TYPE_LABELS

logger = logging.getLogger(__name__)

# Tables a caller may name. Nothing outside these sets is ever interpolated.
LINE_TABLES = frozenset({"DRF1", "POR1", "PDN1", "PCH1", "RPC1", "RPD1", "INV1", "RIN1", "RDN1", "WTR1", "WTQ1"})
HEADER_TABLES = frozenset({"ODRF", "OPOR", "OPDN", "OPCH", "ORPC", "ORPD", "OINV", "ORIN", "ORDN"})
TDS_TABLES = frozenset({"INV5", "RIN5", "PCH5", "RPC5", "DRF5"})

# SAP object type → the header table of that document (the portal's BASE_DOC_TABLE).
OBJECT_TABLES = {
    "13": "OINV", "14": "ORIN", "15": "ODLN", "16": "ORDN", "17": "ORDR", "23": "OQUT",
    "18": "OPCH", "19": "ORPC", "20": "OPDN", "21": "ORPD", "22": "OPOR", "540000006": "OPRQ",
}
OBJECT_LABELS = {
    "13": "AR Invoice", "14": "AR Credit Memo", "15": "Delivery", "16": "Return", "17": "Sales Order",
    "23": "Sales Quotation", "18": "AP Invoice", "19": "AP Credit Note", "20": "Goods Receipt PO",
    "21": "Goods Return", "22": "Purchase Order", "540000006": "Purchase Request",
    "24": "Incoming Payment", "46": "Outgoing Payment", "30": "Journal Entry",
    "59": "Goods Receipt", "60": "Goods Issue", "67": "Inventory Transfer",
    "1250000001": "Inventory Transfer Request", "202": "Production Order", "112": "Draft",
}

# Documents a payment can settle, by PDF2/VPM2 InvType (the portal's PAYMENT_INV_TABLE).
PAYMENT_INVOICE_TABLES = {"13": "OINV", "14": "ORIN", "18": "OPCH", "19": "ORPC", "24": "ORCT", "46": "OVPM"}

# The draft journal preview is built only for invoices and credit notes: the
# documents whose journal SAP Portal's rules were verified against (5e434d4,
# ca4a0ad — A/P invoices with RCM and TDS, A/P and A/R credit notes). The portal
# also ran them for GRPO and PO drafts, where they credited the vendor's control
# account; SAP posts a GRPO against goods-received-not-invoiced and a PO not at all.
PREVIEW_OBJECT_TYPES = frozenset({"13", "14", "18", "19"})
AP_OBJECT_TYPES = frozenset({"18", "19"})
CREDIT_NOTE_OBJECT_TYPES = frozenset({"14", "19"})

PREVIEW_MEMO = "Reconstructed journal preview — SAP posts the final entry on approval"

_FOUR = Decimal("0.0001")
_TWO = Decimal("0.01")


# ---------------------------------------------------------------------------
# small value helpers
# ---------------------------------------------------------------------------


def clean(value) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def to_decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def r4(value) -> Decimal:
    """The portal's ``Math.round(v * 10000) / 10000``."""
    return to_decimal(value).quantize(_FOUR, rounding=ROUND_HALF_UP)


def money(value) -> float:
    return float(to_decimal(value).quantize(_TWO, rounding=ROUND_HALF_UP))


def number(value):
    """A float, or None when SAP holds nothing."""
    if value is None or value == "":
        return None
    return float(to_decimal(value))


def as_int(value):
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def iso_date(value):
    """``YYYY-MM-DD``; None for nothing and for SAP's 1899/1900 placeholders."""
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        text = value.strftime("%Y-%m-%d")
    else:
        text = str(value)[:10]
    return None if text.startswith(("1899-12-30", "1900-01-01")) else text


def rate_text(rate) -> str:
    """``18.000000`` → ``18``, ``2.500000`` → ``2.5`` (JavaScript's ``String(Number(x))``)."""
    text = format(to_decimal(rate).normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def extract_tds_section(name) -> str:
    """The statutory section inside a withholding-tax name (``194C``, ``206C1H``…)."""
    match = re.search(r"\b(19[0-9][A-Z]{0,2}|20[0-9][A-Z]{0,2}|206C[A-Z]?)\b", str(name or ""))
    return match.group(1).upper() if match else ""


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


def _ints(values) -> list[int]:
    out = []
    for value in values:
        number_ = as_int(value)
        if number_ is not None and number_ not in out:
            out.append(number_)
    return out


def _codes(values) -> list[str]:
    out = []
    for value in values:
        code = clean(value)
        if code and code not in out:
            out.append(code)
    return out


# ---------------------------------------------------------------------------
# Draft journal preview (the portal's buildDraftJournalEntryFromHana)
# ---------------------------------------------------------------------------


def gst_account_kind(sta_code) -> str | None:
    """Which GST ledger a tax-code component posts to, from its STC1 type code."""
    code = str(sta_code or "").upper()
    if "IGST" in code or code.startswith("IG"):
        return "IGST"
    if "CGST" in code or code.startswith("CG") or code.startswith("RCG"):
        return "CGST"
    if "SGST" in code or code.startswith("SG") or code.startswith("RSG"):
        return "SGST"
    if "CESS" in code:
        return "CESS"
    return None


def is_rcm_tax_name(name) -> bool:
    """A reverse-charge tax code, as the portal read it off ``OSTC.Name``."""
    return bool(re.search(r"RCM|REVERSE\s*CHARGE", str(name or ""), re.IGNORECASE))


def resolve_gst_account(accounts: dict, direction: str, kind: str, rate, rcm: bool):
    """The GST G/L account for (INPUT/OUTPUT, IGST/CGST/SGST/CESS, rate, RCM?).

    OSTA/OSTT carry no account here, so the chart of accounts is the
    determination source, by its naming convention ("INPUT IGST @ 18 %").
    Reverse-charge GST posts to the "... RCM" accounts and forward GST never
    does. Shortest matching name wins, as the portal's ``ORDER BY LENGTH``.
    """
    wanted = (direction.upper(), kind.upper(), rate_text(rate))
    hits = []
    for code, name in accounts.items():
        upper = (name or "").upper()
        if not all(part in upper for part in wanted):
            continue
        if ("RCM" in upper) != bool(rcm):
            continue
        hits.append((len(name or ""), code, name))
    if not hits:
        return None
    _, code, name = sorted(hits)[0]
    return code, name


def _first_account(accounts: dict, pattern: str, exclude: str = ""):
    for code in sorted(accounts):
        upper = (accounts[code] or "").upper()
        if re.search(pattern, upper) and not (exclude and exclude in upper):
            return code, accounts[code]
    return None


def _journal_line(account, name, amount: Decimal, side: str, *, short_name="", memo="", cost_centers=None):
    return {
        "line_id": None,
        "account": clean(account),
        "account_name": clean(name),
        "short_name": clean(short_name),
        "debit": float(amount) if side == "D" else 0.0,
        "credit": float(amount) if side == "C" else 0.0,
        "contra_account": "",
        "line_memo": memo,
        "cost_centers": list(cost_centers or ["", "", "", "", ""]),
    }


def build_draft_journal(
    header: dict,
    lines: list[dict],
    expenses: list[dict],
    withholding: list[dict],
    tax_codes: dict,
    accounts: dict,
    rounding_account=None,
) -> dict | None:
    """Reconstruct the journal SAP will post when a draft is added.

    A draft has no OJDT yet. Each draft line already carries its determined G/L
    account (``DRF1.AcctCode``); the partner leg goes to the partner's control
    account (``OCRD.DebPayAcct``); GST is split per tax-code component (STC1)
    and matched to the chart of accounts; additional expenses come from DRF3;
    and any residual lands on the rounding (Short & Excess) account so the
    preview always balances — SAP Portal ``routes/sap.js:2135-2273``.

    * Reverse charge (5e434d4): the input leg uses the INPUT … RCM account and a
      matching OUTPUT … RCM liability posts on the opposite side, detected from
      ``OSTC.Name``; without it the amount fell into Short & Excess.
    * TDS (5e434d4): the tax withheld (DRF5) posts to ``OWHT.ApTdsAcc`` (A/R:
      ``ArTdsAcc``) on the partner's side; ``DocTotal`` already excludes it.
    * Credit notes (ca4a0ad): an A/R or A/P credit note reverses its invoice, so
      the expense, tax, partner, RCM and TDS legs all flip. The GST family stays
      INPUT for purchases and OUTPUT for sales; only the side reverses.

    ``header``: obj_type, card_code, card_name, doc_total, doc_date, due_date,
    tax_date, num_at_card, journal_memo, control_account. ``lines`` /
    ``expenses``: account, line_total, line_vat, tax_code (+ stock on expenses).
    ``withholding``: amount, ap_account, ar_account, name. ``tax_codes``:
    {code: {"name", "components": [(sta_code, rate), …]}}. ``accounts``:
    {code: name} — every account named here plus the GST / expense-clearing /
    short-and-excess candidates. ``rounding_account``: (code, name) of
    ``OACP.LinkAct_24`` or None.
    """
    obj_type = clean(header.get("obj_type"))
    if obj_type not in PREVIEW_OBJECT_TYPES:
        return None
    is_ap = obj_type in AP_OBJECT_TYPES
    direction = "INPUT" if is_ap else "OUTPUT"

    def flip(side):
        return "C" if side == "D" else "D"

    expense_side = "D" if is_ap else "C"  # expense / GRNI: A/P debit, A/R (revenue) credit
    tax_side = "D" if is_ap else "C"  # input tax debit, output tax credit
    bp_side = "C" if is_ap else "D"  # vendor credit, customer debit
    if obj_type in CREDIT_NOTE_OBJECT_TYPES:
        expense_side, tax_side, bp_side = flip(expense_side), flip(tax_side), flip(bp_side)

    out: list[dict] = []

    def add(account, name, amount, side, **extra):
        value = r4(amount)
        if not value:
            return
        out.append(_journal_line(account, name, value, side, **extra))

    def add_tax(tax_code, line_vat):
        if not to_decimal(line_vat):
            return
        code = clean(tax_code)
        info = tax_codes.get(code) or {}
        rcm = is_rcm_tax_name(info.get("name"))
        components = info.get("components") or []
        total_rate = sum((to_decimal(rate) for _, rate in components), Decimal("0")) or Decimal("1")
        for sta_code, rate in components:
            kind = gst_account_kind(sta_code)
            if not kind:
                continue
            amount = r4(to_decimal(line_vat) * to_decimal(rate) / total_rate)
            label_rate = rate_text(rate)
            found = resolve_gst_account(accounts, direction, kind, rate, rcm)
            add(
                found[0] if found else f"{direction} {kind}",
                found[1] if found else f"{direction} {kind} @ {label_rate}%{' RCM' if rcm else ''}",
                amount,
                tax_side,
            )
            if rcm:
                liability = resolve_gst_account(accounts, "OUTPUT", kind, rate, True)
                add(
                    liability[0] if liability else f"OUTPUT {kind} RCM",
                    liability[1] if liability else f"OUTPUT {kind} @ {label_rate}% RCM",
                    amount,
                    flip(tax_side),
                )

    for line in lines:
        code = clean(line.get("account"))
        add(code, accounts.get(code, ""), line.get("line_total"), expense_side)
    for line in lines:
        add_tax(line.get("tax_code"), line.get("line_vat"))
    for expense in expenses:
        if clean(expense.get("stock")) == "Y":
            # Freight on stock clears through the Expense Clearing account.
            found = _first_account(accounts, r"EXPENSE CLEARING")
            code, name = found if found else ("", "")
        else:
            code = clean(expense.get("account"))
            name = accounts.get(code, "")
        add(code, name, expense.get("line_total"), expense_side)
        add_tax(expense.get("tax_code"), expense.get("line_vat"))

    card_code = clean(header.get("card_code"))
    control = clean(header.get("control_account"))
    add(control or card_code, clean(header.get("card_name")), header.get("doc_total"), bp_side, short_name=card_code)

    for row in withholding:
        if not to_decimal(row.get("amount")):
            continue
        account = clean(row.get("ap_account") if is_ap else row.get("ar_account"))
        name = (accounts.get(account, "") if account else "") or clean(row.get("name")) or "TDS Payable"
        add(account or "TDS Payable", name, row.get("amount"), bp_side)

    debit = sum((to_decimal(line["debit"]) for line in out), Decimal("0"))
    credit = sum((to_decimal(line["credit"]) for line in out), Decimal("0"))
    difference = r4(credit - debit)
    if abs(difference) >= _FOUR:
        # OACP's rounding account when it names a real account, else the
        # chart's Short & Excess account (never a stock one).
        found = rounding_account if rounding_account and all(rounding_account) else None
        if not found:
            found = _first_account(accounts, r"SHORT.*EXCESS", exclude="STOCK")
        add(
            found[0] if found else "Rounding",
            (found[1] if found else "") or "Rounding / Short & Excess",
            abs(difference),
            "D" if difference > 0 else "C",
        )

    if not out:
        return None
    for index, line in enumerate(out, start=1):
        line["line_id"] = index
    return {
        "trans_id": None,
        "number": None,
        "preview": True,
        "ref_date": iso_date(header.get("doc_date")),
        "due_date": iso_date(header.get("due_date")),
        "tax_date": iso_date(header.get("tax_date")),
        "memo": clean(header.get("journal_memo")) or PREVIEW_MEMO,
        "base_ref": clean(header.get("num_at_card")),
        "trans_type": obj_type,
        "trans_type_label": OBJECT_LABELS.get(obj_type, obj_type),
        "total_debit": float(r4(sum((to_decimal(x["debit"]) for x in out), Decimal("0")))),
        "total_credit": float(r4(sum((to_decimal(x["credit"]) for x in out), Decimal("0")))),
        "lines": out,
    }


# ---------------------------------------------------------------------------
# Outgoing-payment journal preview (the portal's buildPaymentDraftJournalEntry)
# ---------------------------------------------------------------------------

PAYMENT_PREVIEW_MEMO = (
    "Outgoing payment journal preview — the final SAP journal entry is generated after approval."
)


def build_payment_journal(payment: dict, accounts: dict) -> dict | None:
    """Dr G/L lines + Dr partner (the rest) = Cr bank / cash / cheque + Cr TDS.

    SAP Portal ``routes/sap.js:2536-2579``. ``payment``: doc_num, doc_date,
    due_date, tax_date, card_code, card_name, invoices (count matters),
    accounts [{account_code, account_name, description, sum_paid,
    gross_amount, cost_centers}], cash_sum/cash_account, transfer_sum/
    transfer_account, check_sum/check_account, credit_sum, wt_amount/
    wt_account. ``accounts``: {code: name} to fill names SAP left blank.
    """
    out: list[dict] = []
    gl_total = Decimal("0")
    gl_lines = 0
    for row in payment.get("accounts") or []:
        amount = to_decimal(row.get("sum_paid")) or to_decimal(row.get("gross_amount"))
        if not amount:
            continue
        gl_total += amount
        gl_lines += 1
        out.append(
            _journal_line(
                row.get("account_code"),
                row.get("account_name"),
                amount,
                "D",
                memo=clean(row.get("description")) or "G/L payment",
                cost_centers=row.get("cost_centers"),
            )
        )
    means_total = Decimal("0")
    for account_key, sum_key, memo in (
        ("transfer_account", "transfer_sum", "Bank transfer"),
        ("cash_account", "cash_sum", "Cash"),
        ("check_account", "check_sum", "Cheque"),
        ("credit_account", "credit_sum", "Credit card"),
    ):
        amount = to_decimal(payment.get(sum_key))
        if amount > 0:
            means_total += amount
            out.append(_journal_line(clean(payment.get(account_key)) or memo, "", amount, "C", memo=memo))
    withheld = to_decimal(payment.get("wt_amount"))
    if withheld > 0:
        account = clean(payment.get("wt_account"))
        out.append(
            _journal_line(
                account or "TDS Payable",
                "" if account else "TDS / Withholding Tax Payable",
                withheld,
                "C",
                memo="TDS / withholding tax withheld",
            )
        )
    partner_debit = (means_total + withheld - gl_total).quantize(_TWO, rounding=ROUND_HALF_UP)
    if partner_debit > Decimal("0.005"):
        settled = len(payment.get("invoices") or [])
        card_code = clean(payment.get("card_code"))
        out.insert(
            gl_lines if gl_total > 0 else 0,
            _journal_line(
                card_code or "BP",
                clean(payment.get("card_name")) or "Business Partner / Control",
                partner_debit,
                "D",
                short_name=card_code,
                memo=f"Settlement of {settled} document(s)" if settled else "Payment on account",
            ),
        )
    if not out:
        return None
    for index, line in enumerate(out, start=1):
        line["line_id"] = index
        if not line["account_name"] and line["account"] in accounts:
            line["account_name"] = accounts[line["account"]]
    return {
        "trans_id": None,
        "number": None,
        "preview": True,
        "ref_date": payment.get("doc_date"),
        "due_date": payment.get("due_date"),
        "tax_date": payment.get("tax_date"),
        "memo": PAYMENT_PREVIEW_MEMO,
        "base_ref": str(payment.get("doc_num") or ""),
        "trans_type": "46",
        "trans_type_label": TRANS_TYPE_LABELS.get("46", "46"),
        "total_debit": money(sum((to_decimal(x["debit"]) for x in out), Decimal("0"))),
        "total_credit": money(sum((to_decimal(x["credit"]) for x in out), Decimal("0"))),
        "lines": out,
    }


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


class _Session:
    """Statements on one open connection. ``soft`` names a decoration read that
    may fail without failing the screen; its label goes into ``warnings``."""

    def __init__(self, conn, schema: str, warnings: list):
        self.conn = conn
        self.schema = schema
        self.warnings = warnings

    def rows(self, sql: str, params=(), *, soft: str | None = None, quiet: bool = False) -> list[dict]:
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(sql.replace("{schema}", self.schema), tuple(params))
            names = [column[0] for column in (cursor.description or ())]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except dbapi.Error as e:
            if soft or quiet:
                logger.warning("SAP document lookup skipped (%s): %s", soft or "quiet", e)
                if soft and soft not in self.warnings:
                    self.warnings.append(soft)
                return []
            logger.error("SAP HANA document query failed: %s", e)
            raise SAPDataError("Failed to read the document from SAP.") from e
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass


def _column(row: dict, *names):
    """First non-empty value among ``names``, matched without regard to case."""
    if not row:
        return None
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and value != "":
            return value
    return None


def sac_entry_of(row: dict):
    """The line's GST SAC key (OSAC.AbsEntry) whatever the column is called."""
    explicit = _column(row, "SACEntry", "SacEntry")
    if explicit is not None:
        return explicit
    for key, value in (row or {}).items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if "sac" in normalized and "entry" in normalized and value not in (None, ""):
            return value
    return None


class HanaDocumentReader:
    """Batched HANA reads for one company's SAP documents."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    @contextmanager
    def _session(self, warnings: list):
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading a document: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e
        try:
            yield _Session(conn, self.connection.schema, warnings)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Document detail
    # ------------------------------------------------------------------

    def document_lookups(
        self,
        *,
        doc_entry: int,
        line_table: str | None = None,
        header_table: str | None = None,
        tds_table: str | None = None,
        account_codes=(),
        sac_entries=(),
        warehouse_codes=(),
        dimension_codes=(),
        location_codes=(),
        branch_ids=(),
        sales_person=None,
        payment_group=None,
        transport=None,
        card_code: str = "",
        card_name: str = "",
        base_refs=(),
        payment_invoices=(),
        journal_trans_ids=(),
        journal_created_by=None,
        in_transit_base_type: str | None = None,
        draft: bool = False,
    ) -> dict:
        """Everything the document screen resolves from HANA, on one connection.

        The codes passed in come from the Service Layer document; the line
        table's own rows add theirs (the Service Layer leaves UDFs and some
        standard fields out of draft reads). Every lookup is one ``IN``
        statement over all the lines. See the module docstring for how the
        journal entries are found.
        """
        for table, allowed in ((line_table, LINE_TABLES), (header_table, HEADER_TABLES), (tds_table, TDS_TABLES)):
            if table and table not in allowed:
                raise ValueError(f"{table} is not a document table this reader knows.")
        warnings: list = []
        result = {
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
            "warnings": warnings,
        }
        entry = as_int(doc_entry)
        account_codes = _codes(account_codes)
        sac_entries = _ints(sac_entries)
        warehouse_codes = _codes(warehouse_codes)
        dimension_codes = _codes(dimension_codes)
        location_codes = _ints(c for c in location_codes if re.fullmatch(r"\d+", clean(c)))
        branch_ids = _ints(branch_ids)
        base_refs = list(base_refs)

        with self._session(warnings) as s:
            if line_table and entry:
                result["line_rows"] = s.rows(
                    f'SELECT * FROM "{{schema}}"."{line_table}" WHERE "DocEntry" = ? ORDER BY "LineNum"',
                    (entry,),
                    soft="document lines",
                )
                for row in result["line_rows"]:
                    account_codes = _codes([*account_codes, _column(row, "AcctCode")])
                    warehouse_codes = _codes([*warehouse_codes, _column(row, "WhsCode"), _column(row, "FromWhsCod")])
                    dimension_codes = _codes(
                        [*dimension_codes, *(_column(row, f"OcrCode{n}" if n > 1 else "OcrCode") for n in range(1, 6))]
                    )
                    location = clean(_column(row, "LocCode"))
                    if re.fullmatch(r"\d+", location):
                        location_codes = _ints([*location_codes, location])
                    sac = sac_entry_of(row)
                    if sac is not None:
                        sac_entries = _ints([*sac_entries, sac])
                    base_type, base_entry = as_int(_column(row, "BaseType")), as_int(_column(row, "BaseEntry"))
                    if base_type and base_type > 0 and base_entry and base_entry > 0:
                        base_refs.append((str(base_type), base_entry, clean(_column(row, "BaseRef"))))

            if header_table and entry:
                rows = s.rows(
                    f"""
                    SELECT H."PaidToDate" AS "paid_to_date", H."GrosProfit" AS "gross_profit",
                           H."TransId" AS "trans_id", H."ObjType" AS "obj_type",
                           H."CardCode" AS "card_code", H."CardName" AS "card_name",
                           H."DocTotal" AS "doc_total", H."DocDate" AS "doc_date",
                           H."DocDueDate" AS "due_date", H."TaxDate" AS "tax_date",
                           H."NumAtCard" AS "num_at_card", H."JrnlMemo" AS "journal_memo",
                           C."DebPayAcct" AS "control_account"
                    FROM "{{schema}}"."{header_table}" H
                    LEFT JOIN "{{schema}}"."OCRD" C ON C."CardCode" = H."CardCode"
                    WHERE H."DocEntry" = ?
                    """,
                    (entry,),
                    soft="settlement and journal",
                )
                result["header"] = rows[0] if rows else None

            if tds_table and entry:
                result["tds"] = s.rows(
                    f"""
                    SELECT X."WTCode" AS "code", X."Rate" AS "rate", X."WTAmnt" AS "amount",
                           X."TaxbleAmnt" AS "taxable", W."WTName" AS "name", W."OffclCode" AS "section",
                           W."ApTdsAcc" AS "ap_account", W."ArTdsAcc" AS "ar_account"
                    FROM "{{schema}}"."{tds_table}" X
                    LEFT JOIN "{{schema}}"."OWHT" W ON W."WTCode" = X."WTCode"
                    WHERE X."AbsEntry" = ? AND X."WTCode" IS NOT NULL AND X."WTCode" <> ''
                    """,
                    (entry,),
                    soft="withholding tax",
                )

            result["accounts"] = self._account_names(s, account_codes, soft="G/L account names")
            if sac_entries:
                for row in s.rows(
                    f'SELECT "AbsEntry" AS "entry", "ServCode" AS "code", "ServName" AS "name" '
                    f'FROM "{{schema}}"."OSAC" WHERE "AbsEntry" IN ({_placeholders(sac_entries)})',
                    sac_entries,
                    soft="SAC codes",
                ):
                    result["sac"][str(as_int(row["entry"]))] = {"code": clean(row["code"]), "name": clean(row["name"])}
            if dimension_codes:
                for row in s.rows(
                    f'SELECT "PrcCode" AS "code", "PrcName" AS "name" '
                    f'FROM "{{schema}}"."OPRC" WHERE "PrcCode" IN ({_placeholders(dimension_codes)})',
                    dimension_codes,
                    soft="cost dimension names",
                ):
                    result["dimensions"][clean(row["code"])] = clean(row["name"])
            if location_codes:
                # OLCT is the location master; OBPL shares numeric ids with
                # different names, so it is only a fallback (portal 1953-1997).
                for row in s.rows(
                    f'SELECT "Code" AS "code", "Location" AS "name" '
                    f'FROM "{{schema}}"."OLCT" WHERE "Code" IN ({_placeholders(location_codes)})',
                    location_codes,
                    soft="location names",
                ):
                    result["locations"][str(as_int(row["code"]))] = clean(row["name"])
            branch_lookup = _ints([*branch_ids, *location_codes])
            if branch_lookup:
                for row in s.rows(
                    f'SELECT "BPLId" AS "id", "BPLName" AS "name" '
                    f'FROM "{{schema}}"."OBPL" WHERE "BPLId" IN ({_placeholders(branch_lookup)})',
                    branch_lookup,
                    soft="branch names",
                ):
                    result["branches"][str(as_int(row["id"]))] = clean(row["name"])

            result["header_names"] = self._header_names(s, sales_person, payment_group, transport)

            if warehouse_codes:
                for row in s.rows(
                    f"""
                    SELECT W."WhsCode" AS "code", W."WhsName" AS "name", W."Street" AS "street",
                           W."StreetNo" AS "street_no", W."Block" AS "block", W."City" AS "city",
                           W."State" AS "state", W."ZipCode" AS "zip", W."Country" AS "country",
                           B."BPLName" AS "branch", B."TaxIdNum" AS "gstin", B."State" AS "branch_state"
                    FROM "{{schema}}"."OWHS" W
                    LEFT JOIN "{{schema}}"."OBPL" B ON B."BPLId" = W."BPLid"
                    WHERE W."WhsCode" IN ({_placeholders(warehouse_codes)})
                    """,
                    warehouse_codes,
                    soft="warehouse names",
                ):
                    code = clean(row["code"])
                    address = ", ".join(
                        part
                        for part in (
                            clean(row.get(key))
                            for key in ("street_no", "street", "block", "city", "state", "zip", "country")
                        )
                        if part
                    )
                    result["warehouses"][code] = {
                        "code": code,
                        "name": clean(row["name"]),
                        "gstin": clean(row.get("gstin")),
                        "branch": clean(row.get("branch")),
                        "state": clean(row.get("state")) or clean(row.get("branch_state")),
                        "address": address,
                    }

            code, name = clean(card_code), clean(card_name)
            if code or name:
                result["partners"] = self._partners(s, code, name)

            result["base_documents"] = self._base_documents(s, base_refs)
            result["payment_invoices"] = self._payment_invoice_docs(s, payment_invoices)

            # ---- journal entries -------------------------------------------------
            header = result["header"] or {}
            trans_ids = _ints(journal_trans_ids)
            if draft:
                posted = self._posted_from_draft(s, clean(header.get("obj_type")), entry)
                if posted:
                    result["posted_as"] = posted
                    trans_ids = _ints([*trans_ids, posted.get("trans_id")])
            elif header_table and as_int(header.get("trans_id")):
                trans_ids = _ints([*trans_ids, header.get("trans_id")])
            if not trans_ids and journal_created_by:
                trans_type, created_by = journal_created_by
                rows = s.rows(
                    """
                    SELECT TOP 1 "TransId" AS "trans_id" FROM "{schema}"."OJDT"
                    WHERE CAST("TransType" AS NVARCHAR(20)) = ? AND "CreatedBy" = ?
                    ORDER BY "TransId"
                    """,
                    (str(trans_type), int(created_by)),
                    soft="journal entry",
                )
                trans_ids = _ints(row["trans_id"] for row in rows)
            in_transit_ids = []
            if in_transit_base_type:
                in_transit_ids = _ints(
                    doc["trans_id"] for doc in result["base_documents"] if doc["base_type"] == in_transit_base_type
                )
            journals = self._journal_entries(s, [*trans_ids, *in_transit_ids])
            if trans_ids:
                result["journal_entry"] = journals.get(trans_ids[0])
            result["in_transit_journal_entries"] = [journals[i] for i in in_transit_ids if i in journals]

            # A draft that has been added has a real journal (or none, if its
            # load failed); only a draft still waiting gets the reconstruction.
            if draft and not result["posted_as"] and clean(header.get("obj_type")) in PREVIEW_OBJECT_TYPES:
                result["journal_preview"] = self._draft_preview(s, entry, header, result["line_rows"], result["tds"])
        return result

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def attachment_lines(self, abs_entry: int) -> list[dict]:
        """The files of one attachment entry (``ATC1``), in SAP's line order.

        ``Line`` is what the file service keys on (``/files/by-entry/{AbsEntry}/{Line}``,
        ``backend_v1/docs/attachment-entry-lookup.md``). ``SELECT *`` because
        the column set differs by SAP version; only the known ones are returned
        (never the share path).
        """
        entry = as_int(abs_entry)
        if not entry or entry <= 0:
            return []
        with self._session([]) as s:
            rows = s.rows('SELECT * FROM "{schema}"."ATC1" WHERE "AbsEntry" = ? ORDER BY "Line"', (entry,))
        lines = []
        for row in rows:
            stem = clean(_column(row, "FileName"))
            extension = clean(_column(row, "FileExt")).lstrip(".")
            lines.append(
                {
                    "line": as_int(_column(row, "Line")),
                    "file_name": attachment_file_name(stem, extension),
                    "stem": stem,
                    "extension": extension,
                    "attached_on": iso_date(_column(row, "Date")),
                }
            )
        return lines

    # ------------------------------------------------------------------
    # Outgoing-payment drafts
    # ------------------------------------------------------------------

    def payment_draft(self, doc_entry: int) -> dict | None:
        """One outgoing-payment draft assembled from OPDF, PDF4 (G/L lines),
        PDF2 (documents settled), PDF1 (cheques) and PDF6 (withholding).

        SAP Portal ``routes/sap.js:2382-2522``. ``SELECT *`` on OPDF and PDF4
        because their user-defined fields (``U_Pymnt_Mode``, ``U_Remarks``)
        are not present in every company.
        """
        entry = as_int(doc_entry)
        if not entry or entry <= 0:
            return None
        warnings: list = []
        with self._session(warnings) as s:
            headers = s.rows('SELECT * FROM "{schema}"."OPDF" WHERE "DocEntry" = ?', (entry,))
            if not headers:
                return None
            h = headers[0]
            account_rows = s.rows('SELECT * FROM "{schema}"."PDF4" WHERE "DocNum" = ? ORDER BY "LineId"', (entry,))
            invoice_rows = s.rows(
                """
                SELECT "InvoiceId" AS "invoice_id", "DocEntry" AS "doc_entry", "InvType" AS "inv_type",
                       "SumApplied" AS "sum_applied", "Dcount" AS "discount_percent", "DcntSum" AS "total_discount"
                FROM "{schema}"."PDF2" WHERE "DocNum" = ? ORDER BY "InvoiceId"
                """,
                (entry,),
            )
            check_rows = s.rows(
                """
                SELECT "LineID" AS "line", "CheckNum" AS "check_number", "BankCode" AS "bank_code",
                       "DueDate" AS "due_date", "CheckSum" AS "check_sum"
                FROM "{schema}"."PDF1" WHERE "DocNum" = ? ORDER BY "LineID"
                """,
                (entry,),
                soft="cheques",
            )
            wt_rows = s.rows(
                """
                SELECT X."WTCode" AS "code", X."Rate" AS "rate", X."TaxbleAmnt" AS "taxable",
                       X."WTSum" AS "amount", X."TdsAmnt" AS "tds_amount", W."WTName" AS "name"
                FROM "{schema}"."PDF6" X LEFT JOIN "{schema}"."OWHT" W ON W."WTCode" = X."WTCode"
                WHERE X."DocNum" = ? ORDER BY X."Line"
                """,
                (entry,),
                soft="withholding tax",
            )
            invoice_docs = self._payment_invoice_docs(
                s, [(clean(row["inv_type"]), row["doc_entry"]) for row in invoice_rows]
            )
            trans_id = as_int(_column(h, "TransId"))
            journals = self._journal_entries(s, [trans_id] if trans_id else [])
            means_accounts = [_column(h, key) for key in ("TrsfrAcct", "CashAcct", "CheckAcct", "WtAccount")]
            names = self._account_names(
                s, [*means_accounts, *(_column(row, "AcctCode") for row in account_rows)], soft="G/L account names"
            )
        return shape_payment_draft(
            h, account_rows, invoice_rows, check_rows, wt_rows, invoice_docs, journals.get(trans_id), names, warnings
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _account_names(self, s: _Session, codes, *, soft: str) -> dict:
        codes = _codes(codes)
        if not codes:
            return {}
        return {
            clean(row["code"]): clean(row["name"])
            for row in s.rows(
                f'SELECT "AcctCode" AS "code", "AcctName" AS "name" '
                f'FROM "{{schema}}"."OACT" WHERE "AcctCode" IN ({_placeholders(codes)})',
                codes,
                soft=soft,
            )
        }

    def _header_names(self, s: _Session, sales_person, payment_group, transport) -> dict:
        """Sales employee, payment terms and shipping type names, in one statement.
        SAP's -1 means "none" and is not looked up (portal 1659-1680)."""
        parts, params = [], []
        for kind, value, table, key, name in (
            ("sales_person", sales_person, "OSLP", "SlpCode", "SlpName"),
            ("payment_terms", payment_group, "OCTG", "GroupNum", "PymntGroup"),
            ("shipping_type", transport, "OSHP", "TrnspCode", "TrnspName"),
        ):
            code = as_int(value)
            if code is None or code < 0:
                continue
            parts.append(
                f"SELECT '{kind}' AS \"kind\", CAST(\"{name}\" AS NVARCHAR(254)) AS \"name\" "
                f'FROM "{{schema}}"."{table}" WHERE "{key}" = ?'
            )
            params.append(code)
        if not parts:
            return {}
        return {
            row["kind"]: clean(row["name"])
            for row in s.rows(" UNION ALL ".join(parts), params, soft="sales employee and terms")
            if clean(row["name"])
        }

    def _partners(self, s: _Session, card_code: str, card_name: str) -> list[dict]:
        """The partner (by code, or every partner of that exact name when SAP left
        the draft's code empty) with all of its CRD1 addresses."""
        where, value = ('C."CardCode" = ?', card_code) if card_code else ('C."CardName" = ?', card_name)
        rows = s.rows(
            f"""
            SELECT C."CardCode" AS "card_code", C."CardName" AS "card_name", C."CardType" AS "card_type",
                   C."validFor" AS "valid_for", C."LicTradNum" AS "lic_trad_num",
                   A."Address" AS "address", A."GSTRegnNo" AS "gstin", A."State" AS "state",
                   A."Street" AS "street", A."Block" AS "block", A."City" AS "city",
                   A."ZipCode" AS "zip", A."Country" AS "country"
            FROM "{{schema}}"."OCRD" C
            LEFT JOIN "{{schema}}"."CRD1" A ON A."CardCode" = C."CardCode"
            WHERE {where}
            """,
            (value,),
            soft="business partner",
        )
        partners: dict[str, dict] = {}
        for row in rows:
            code = clean(row["card_code"])
            partner = partners.setdefault(
                code,
                {
                    "card_code": code,
                    "card_name": clean(row["card_name"]),
                    "card_type": clean(row["card_type"]),
                    "valid_for": clean(row["valid_for"]),
                    "lic_trad_num": clean(row["lic_trad_num"]),
                    "addresses": [],
                },
            )
            if row.get("address") is not None:
                partner["addresses"].append(
                    {key: clean(row.get(key)) for key in ("address", "gstin", "state", "street", "block", "city", "zip", "country")}
                )
        return list(partners.values())

    def _base_documents(self, s: _Session, refs) -> list[dict]:
        """The documents the lines were copied from, with their own attachment
        entry — the scan is often filed there (portal 1769-1805)."""
        # {type: {entry: BaseRef}} — the Service Layer line has no BaseRef and
        # the HANA row does, so a later non-empty one fills an earlier blank.
        by_type: dict[str, dict] = {}
        for ref in refs:
            base_type, base_entry = str(ref[0]), as_int(ref[1])
            base_ref = clean(ref[2]) if len(ref) > 2 else ""
            if base_type not in OBJECT_TABLES or not base_entry or base_entry <= 0:
                continue
            items = by_type.setdefault(base_type, {})
            if not items.get(base_entry):
                items[base_entry] = base_ref
        out = []
        for base_type, entries in by_type.items():
            table = OBJECT_TABLES[base_type]
            items = list(entries.items())
            ids = [item[0] for item in items]
            rows = s.rows(
                f"""
                SELECT "DocEntry" AS "doc_entry", "DocNum" AS "doc_num", "AtcEntry" AS "atc_entry",
                       "DocDate" AS "doc_date", "TransId" AS "trans_id"
                FROM "{{schema}}"."{table}" WHERE "DocEntry" IN ({_placeholders(ids)})
                """,
                ids,
                soft="base documents",
            )
            by_entry = {as_int(row["doc_entry"]): row for row in rows}
            for base_entry, base_ref in items:
                row = by_entry.get(base_entry)
                if not row:
                    continue
                atc = as_int(row.get("atc_entry"))
                out.append(
                    {
                        "base_type": base_type,
                        "base_entry": base_entry,
                        "type_label": OBJECT_LABELS.get(base_type, f"Object {base_type}"),
                        "doc_num": as_int(row.get("doc_num")),
                        "doc_date": iso_date(row.get("doc_date")),
                        "base_ref": base_ref or None,
                        "attachment_entry": atc if atc and atc > 0 else None,
                        "trans_id": as_int(row.get("trans_id")),
                    }
                )
        return out

    def _payment_invoice_docs(self, s: _Session, refs) -> dict:
        """{"<type>-<entry>": {doc_num, doc_date, doc_total}} for the documents a
        payment settles, one statement per document type (portal 2356-2380)."""
        by_type: dict[str, list] = {}
        for inv_type, doc_entry in refs:
            entry = as_int(doc_entry)
            if clean(inv_type) in PAYMENT_INVOICE_TABLES and entry:
                ids = by_type.setdefault(clean(inv_type), [])
                if entry not in ids:
                    ids.append(entry)
        found = {}
        for inv_type, ids in by_type.items():
            for row in s.rows(
                f"""
                SELECT "DocEntry" AS "doc_entry", "DocNum" AS "doc_num", "DocDate" AS "doc_date",
                       "DocTotal" AS "doc_total"
                FROM "{{schema}}"."{PAYMENT_INVOICE_TABLES[inv_type]}" WHERE "DocEntry" IN ({_placeholders(ids)})
                """,
                ids,
                soft="settled document numbers",
            ):
                found[f"{inv_type}-{as_int(row['doc_entry'])}"] = {
                    "doc_num": as_int(row.get("doc_num")),
                    "doc_date": iso_date(row.get("doc_date")),
                    "doc_total": number(row.get("doc_total")),
                }
        return found

    def _posted_from_draft(self, s: _Session, obj_type: str, draft_entry) -> dict | None:
        """The document an added draft became (``draftKey`` on its header)."""
        table = OBJECT_TABLES.get(obj_type)
        if not table or not draft_entry:
            return None
        rows = s.rows(
            f'SELECT TOP 1 "DocEntry" AS "doc_entry", "DocNum" AS "doc_num", "TransId" AS "trans_id" '
            f'FROM "{{schema}}"."{table}" WHERE "draftKey" = ? ORDER BY "DocEntry"',
            (int(draft_entry),),
            soft="document added from this draft",
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "object_type": obj_type,
            "type_label": OBJECT_LABELS.get(obj_type, obj_type),
            "doc_entry": as_int(row.get("doc_entry")),
            "doc_num": as_int(row.get("doc_num")),
            "trans_id": as_int(row.get("trans_id")),
        }

    def _journal_entries(self, s: _Session, trans_ids) -> dict:
        """{TransId: entry} — headers and lines of several journals in one statement."""
        ids = _ints(trans_ids)
        if not ids:
            return {}
        rows = s.rows(
            f"""
            SELECT H."TransId" AS "trans_id", H."Number" AS "number", H."RefDate" AS "ref_date",
                   H."DueDate" AS "due_date", H."TaxDate" AS "tax_date", H."Memo" AS "memo",
                   H."BaseRef" AS "base_ref", H."TransType" AS "trans_type",
                   L."Line_ID" AS "line_id", L."Account" AS "account", A."AcctName" AS "account_name",
                   L."ShortName" AS "short_name", L."Debit" AS "debit", L."Credit" AS "credit",
                   L."ContraAct" AS "contra_account", L."LineMemo" AS "line_memo",
                   L."ProfitCode" AS "cc1", L."OcrCode2" AS "cc2", L."OcrCode3" AS "cc3",
                   L."OcrCode4" AS "cc4", L."OcrCode5" AS "cc5"
            FROM "{{schema}}"."OJDT" H
            LEFT JOIN "{{schema}}"."JDT1" L ON L."TransId" = H."TransId"
            LEFT JOIN "{{schema}}"."OACT" A ON A."AcctCode" = L."Account"
            WHERE H."TransId" IN ({_placeholders(ids)})
            ORDER BY H."TransId", L."Line_ID"
            """,
            ids,
            soft="journal entry",
        )
        entries: dict[int, dict] = {}
        for row in rows:
            trans_id = as_int(row["trans_id"])
            kind = clean(row.get("trans_type"))
            entry = entries.setdefault(
                trans_id,
                {
                    "trans_id": trans_id,
                    "number": as_int(row.get("number")),
                    "preview": False,
                    "ref_date": iso_date(row.get("ref_date")),
                    "due_date": iso_date(row.get("due_date")),
                    "tax_date": iso_date(row.get("tax_date")),
                    "memo": clean(row.get("memo")),
                    "base_ref": clean(row.get("base_ref")),
                    "trans_type": kind,
                    "trans_type_label": TRANS_TYPE_LABELS.get(kind, OBJECT_LABELS.get(kind, kind)),
                    "total_debit": 0.0,
                    "total_credit": 0.0,
                    "lines": [],
                },
            )
            if row.get("line_id") is None:
                continue
            debit, credit = money(row.get("debit")), money(row.get("credit"))
            entry["lines"].append(
                {
                    "line_id": as_int(row["line_id"]),
                    "account": clean(row.get("account")),
                    "account_name": clean(row.get("account_name")),
                    "short_name": clean(row.get("short_name")),
                    "debit": debit,
                    "credit": credit,
                    "contra_account": clean(row.get("contra_account")),
                    "line_memo": clean(row.get("line_memo")),
                    "cost_centers": [clean(row.get(f"cc{n}")) for n in range(1, 6)],
                }
            )
            entry["total_debit"] = round(entry["total_debit"] + debit, 2)
            entry["total_credit"] = round(entry["total_credit"] + credit, 2)
        return entries

    def _draft_preview(self, s: _Session, entry: int, header: dict, line_rows: list, tds_rows: list):
        """Rows for :func:`build_draft_journal`. The draft's lines and its DRF5
        rows were already read for the screen; this adds DRF3, the tax-code
        components, the accounts and the rounding account."""
        lines = [
            {
                "account": _column(row, "AcctCode"),
                "line_total": _column(row, "LineTotal"),
                "line_vat": _column(row, "LineVat"),
                "tax_code": _column(row, "TaxCode"),
            }
            for row in line_rows
        ]
        expenses = [
            {
                "account": row.get("expense_account"),
                "line_total": row.get("line_total"),
                "line_vat": row.get("line_vat"),
                "tax_code": row.get("tax_code"),
                "stock": row.get("stock"),
            }
            for row in s.rows(
                """
                SELECT X."ExpnsCode" AS "code", X."LineTotal" AS "line_total", X."LineVat" AS "line_vat",
                       X."TaxCode" AS "tax_code", X."Stock" AS "stock", E."ExpnsAcct" AS "expense_account"
                FROM "{schema}"."DRF3" X
                LEFT JOIN "{schema}"."OEXD" E ON E."ExpnsCode" = X."ExpnsCode"
                WHERE X."DocEntry" = ?
                """,
                (entry,),
                soft="journal preview (freight)",
            )
        ]
        tax_list = _codes([*(line["tax_code"] for line in lines), *(e["tax_code"] for e in expenses)])
        tax_codes: dict = {}
        if tax_list:
            for row in s.rows(
                f"""
                SELECT C."Code" AS "code", C."Name" AS "name", S."STACode" AS "sta_code", S."EfctivRate" AS "rate"
                FROM "{{schema}}"."OSTC" C
                LEFT JOIN "{{schema}}"."STC1" S ON S."STCCode" = C."Code"
                WHERE C."Code" IN ({_placeholders(tax_list)})
                ORDER BY C."Code", S."Line_ID"
                """,
                tax_list,
                soft="journal preview (tax codes)",
            ):
                info = tax_codes.setdefault(clean(row["code"]), {"name": clean(row.get("name")), "components": []})
                if row.get("sta_code") is not None:
                    info["components"].append((clean(row["sta_code"]), row.get("rate")))
        withholding = [
            {
                "amount": row.get("amount"),
                "ap_account": row.get("ap_account"),
                "ar_account": row.get("ar_account"),
                "name": row.get("name"),
            }
            for row in tds_rows
        ]
        explicit = _codes(
            [
                *(line["account"] for line in lines),
                *(e["account"] for e in expenses),
                *(w["ap_account"] for w in withholding),
                *(w["ar_account"] for w in withholding),
                header.get("control_account"),
            ]
        )
        # The GST, expense-clearing and short-and-excess candidates are chosen
        # by name, as the portal did; these patterns are constants, not input.
        account_filter = """
            UPPER("AcctName") LIKE '%INPUT%' OR UPPER("AcctName") LIKE '%OUTPUT%'
            OR UPPER("AcctName") LIKE '%EXPENSE CLEARING%' OR UPPER("AcctName") LIKE '%SHORT%EXCESS%'
        """
        if explicit:
            account_filter = f'"AcctCode" IN ({_placeholders(explicit)}) OR {account_filter}'
        accounts = {
            clean(row["code"]): clean(row["name"])
            for row in s.rows(
                f'SELECT "AcctCode" AS "code", "AcctName" AS "name" FROM "{{schema}}"."OACT" WHERE {account_filter}',
                explicit,
                soft="journal preview (accounts)",
            )
        }
        rounding_rows = s.rows(
            """
            SELECT TOP 1 P."LinkAct_24" AS "code", A."AcctName" AS "name"
            FROM "{schema}"."OACP" P
            LEFT JOIN "{schema}"."OACT" A ON A."AcctCode" = P."LinkAct_24"
            WHERE P."LinkAct_24" IS NOT NULL AND P."LinkAct_24" <> ''
            """,
            (),
            quiet=True,
        )
        rounding = (clean(rounding_rows[0]["code"]), clean(rounding_rows[0]["name"])) if rounding_rows else None
        return build_draft_journal(
            {
                "obj_type": header.get("obj_type"),
                "card_code": header.get("card_code"),
                "card_name": header.get("card_name"),
                "doc_total": header.get("doc_total"),
                "doc_date": header.get("doc_date"),
                "due_date": header.get("due_date"),
                "tax_date": header.get("tax_date"),
                "num_at_card": header.get("num_at_card"),
                "journal_memo": header.get("journal_memo"),
                "control_account": header.get("control_account"),
            },
            lines,
            expenses,
            withholding,
            tax_codes,
            accounts,
            rounding,
        )


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def attachment_file_name(stem: str, extension: str) -> str:
    """``1825`` + ``pdf`` → ``1825.pdf``; a stem that already ends ``.pdf`` stays."""
    stem, extension = clean(stem), clean(extension).lstrip(".")
    if not stem:
        return ""
    if not extension or stem.lower().endswith(f".{extension.lower()}"):
        return stem
    return f"{stem}.{extension}"


# OPDF.WddStatus — the approval state of a payment draft.
PAYMENT_DRAFT_APPROVAL = {
    "W": "Pending approval",
    "Y": "Approved",
    "N": "Rejected",
    "P": "Generated",
    "A": "Generated by authorizer",
    "C": "Cancelled",
    "-": "No approval",
}


def shape_payment_draft(h, account_rows, invoice_rows, check_rows, wt_rows, invoice_docs, journal, names, warnings):
    """The portal's ``fetchPaymentDraft`` result, in this app's field names."""
    currency = clean(_column(h, "DocCurr")) or "INR"
    accounts = []
    for row in account_rows:
        code = clean(_column(row, "AcctCode"))
        accounts.append(
            {
                "account_code": code,
                "account_name": clean(_column(row, "AcctName")) or names.get(code, ""),
                "description": clean(_column(row, "Descrip")) or clean(_column(row, "U_Remarks")),
                "sum_paid": number(_column(row, "SumApplied")),
                "gross_amount": number(_column(row, "GrossAmnt")),
                "tax_code": clean(_column(row, "VatGroup")),
                "cost_centers": [
                    clean(_column(row, "OcrCode" if n == 1 else f"OcrCode{n}")) for n in range(1, 6)
                ],
                "section": clean(_column(row, "Section")),
                "project": clean(_column(row, "Project")),
            }
        )
    invoices = []
    for row in invoice_rows:
        inv_type = clean(row.get("inv_type"))
        doc_entry = as_int(row.get("doc_entry"))
        resolved = invoice_docs.get(f"{inv_type}-{doc_entry}") or {}
        invoices.append(
            {
                "doc_entry": doc_entry,
                "doc_num": resolved.get("doc_num") or doc_entry,
                "doc_date": resolved.get("doc_date"),
                "invoice_type": OBJECT_LABELS.get(inv_type, f"Type {inv_type}"),
                "sum_applied": number(row.get("sum_applied")),
                "discount_percent": number(row.get("discount_percent")),
                "total_discount": number(row.get("total_discount")),
                "doc_total": resolved.get("doc_total"),
            }
        )
    checks = [
        {
            "check_number": clean(row.get("check_number")),
            "bank_code": clean(row.get("bank_code")),
            "due_date": iso_date(row.get("due_date")),
            "check_sum": number(row.get("check_sum")),
        }
        for row in check_rows
    ]
    tds = [
        {
            "code": clean(row.get("code")),
            "name": clean(row.get("name")),
            "section": extract_tds_section(row.get("name")),
            "rate": number(row.get("rate")),
            "amount": number(row.get("amount")) or number(row.get("tds_amount")),
            "taxable": number(row.get("taxable")),
        }
        for row in wt_rows
    ]
    tds = [row for row in tds if row["code"] or row["amount"]]
    wt_amount = sum((to_decimal(row.get("amount")) for row in wt_rows), Decimal("0")) or to_decimal(
        _column(h, "WtSum")
    )
    attachment = as_int(_column(h, "AtcEntry"))
    wdd = clean(_column(h, "WddStatus"))
    payment = {
        "cash_account": clean(_column(h, "CashAcct")),
        "cash_sum": number(_column(h, "CashSum")),
        "check_account": clean(_column(h, "CheckAcct")),
        "check_sum": number(_column(h, "CheckSum")),
        "credit_sum": number(_column(h, "CreditSum")),
        "transfer_account": clean(_column(h, "TrsfrAcct")),
        "transfer_sum": number(_column(h, "TrsfrSum")),
        "transfer_date": iso_date(_column(h, "TrsfrDate")),
        "transfer_reference": clean(_column(h, "TrsfrRef")),
        "counter_reference": clean(_column(h, "CounterRef")),
        "on_account_sum": number(_column(h, "NoDocSum")),
        "wt_account": clean(_column(h, "WtAccount")),
        "wt_amount": float(wt_amount) if wt_amount else None,
        "wt_rate": number(wt_rows[0].get("rate")) if wt_rows else None,
        "payment_mode": clean(_column(h, "U_Pymnt_Mode")),
        "accounts": accounts,
        "invoices": invoices,
        "checks": checks,
    }
    header = {
        "doc_entry": as_int(_column(h, "DocEntry")),
        "doc_num": as_int(_column(h, "DocNum")),
        "doc_date": iso_date(_column(h, "DocDate")),
        "due_date": iso_date(_column(h, "DocDueDate")),
        "tax_date": iso_date(_column(h, "TaxDate")),
        "card_code": clean(_column(h, "CardCode")),
        "card_name": clean(_column(h, "CardName")),
        "party_role": "vendor",
        "bill_to": clean(_column(h, "Address")),
        "currency": currency,
        "comments": clean(_column(h, "Comments")),
        "journal_memo": clean(_column(h, "JrnlMemo")),
        "branch_id": as_int(_column(h, "BPLId")),
        "branch_name": clean(_column(h, "BPLName")),
        "project": clean(_column(h, "PrjCode")),
        "pay_to_code": clean(_column(h, "PayToCode")),
        "series": as_int(_column(h, "Series")),
        "reference": clean(_column(h, "Ref1")),
        "status": "closed" if clean(_column(h, "Status")) == "Y" else "open",
        "approval_status": PAYMENT_DRAFT_APPROVAL.get(wdd, wdd),
    }
    preview = None
    if not journal:
        preview = build_payment_journal(
            {
                **payment,
                "doc_num": header["doc_num"],
                "doc_date": header["doc_date"],
                "due_date": header["due_date"],
                "tax_date": header["tax_date"],
                "card_code": header["card_code"],
                "card_name": header["card_name"],
            },
            names,
        )
    return {
        "header": header,
        "payment": payment,
        "totals": {"currency": currency, "total": number(_column(h, "DocTotal")), "withholding": payment["wt_amount"]},
        "tds": tds,
        "tds_section": ", ".join(dict.fromkeys(row["section"] for row in tds if row["section"])),
        "attachment_entry": attachment if attachment and attachment > 0 else None,
        "journal_entry": journal,
        "journal_preview": preview,
        "warnings": warnings,
    }
