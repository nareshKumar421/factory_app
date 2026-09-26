"""HANA reads behind the master-data pickers ported from SAP Portal.

SAP Portal (``backend_v1``) served these as ``/api/sap/lookup/*`` and as the
``/lookup/*`` routes of its customer and vendor registration routers. They are
the same tables read the same way, with three deliberate differences:

* Every read uses the caller's company. The portal's customer and vendor
  lookups always read the default company (Oil), so a Beverages or Mart
  business partner was approved against Oil's groups, terms and accounts.
* Every value is bound with ``?``. The portal formatted search text into the
  SQL with hand quote-escaping.
* A failed read raises. The portal fell back to the Service Layer and, for tax
  codes, to a hard-coded list — a picker that quietly shows another source's
  data is worse than one that says SAP is unavailable. The two user-defined
  tables (``@MAIN_GROUP``, ``@CHAIN``) are the exception: not every company
  has them, and a registration form must still open without them.
"""

import logging
import re

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

# Business-partner card types as OCRD stores them.
CARD_TYPE_CUSTOMER = "C"
CARD_TYPE_SUPPLIER = "S"
_CARD_TYPES = (CARD_TYPE_CUSTOMER, CARD_TYPE_SUPPLIER)

# OCRG.GroupType uses the same letters as OCRD.CardType.
_GROUP_TYPES = _CARD_TYPES

# User-defined tables the registration forms read, by the name the API uses.
# Whitelisted because a table name cannot be bound as a parameter.
USER_TABLES = {"main-group": "@MAIN_GROUP", "chain": "@CHAIN"}

# The portal's control-account filters, kept exactly: customers draw from the
# 1101000 title (Sundry Debtors), vendors from 2101000 plus the 211* range,
# excluding cash accounts.
_AR_ACCOUNT_FILTER = """"FatherNum" = '1101000'"""
_AP_ACCOUNT_FILTER = """("FatherNum" = '2101000' OR "AcctCode" LIKE '211%') AND "Finanse" = 'N'"""

_CARD_CODE_PREFIX = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _clean(value) -> str:
    return (value or "").strip() if isinstance(value, str) else ("" if value is None else str(value))


def _like(search: str | None) -> str:
    """Upper-cased ``%search%`` for a case-insensitive contains match."""
    return f"%{(search or '').strip().upper()}%"


def _limit(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


class HanaLookupReader:
    """Master-data lookups for pickers: items, codes, accounts, partners."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Items and codes
    # ------------------------------------------------------------------

    def search_items(self, search: str, limit: int = 20) -> list[dict]:
        """Items whose code or name contains ``search`` (two characters minimum)."""
        if len((search or "").strip()) < 2:
            return []
        rows = self._query(
            """
            SELECT TOP {limit} "ItemCode", "ItemName", "InvntryUom", "LastPurPrc"
            FROM "{schema}"."OITM"
            WHERE UPPER("ItemCode") LIKE ? OR UPPER("ItemName") LIKE ?
            ORDER BY "ItemCode"
            """,
            (_like(search), _like(search)),
            limit=_limit(limit, 20, 100),
        )
        return [
            {
                "item_code": _clean(code),
                "item_name": _clean(name),
                "uom": _clean(uom) or "PCS",
                "last_purchase_price": float(price) if price is not None else 0.0,
            }
            for code, name, uom, price in rows
        ]

    def sac_codes(self, search: str = "", limit: int = 50) -> list[dict]:
        """India GST service accounting codes (OSAC)."""
        rows = self._query(
            """
            SELECT TOP {limit} "AbsEntry", "ServCode", "ServName"
            FROM "{schema}"."OSAC"
            WHERE UPPER("ServCode") LIKE ? OR UPPER("ServName") LIKE ?
            ORDER BY "ServCode"
            """,
            (_like(search), _like(search)),
            limit=_limit(limit, 50, 200),
        )
        return [
            {"abs_entry": int(entry), "sac_code": _clean(code), "sac_name": _clean(name) or _clean(code)}
            for entry, code, name in rows
        ]

    def locations(self, search: str = "", limit: int = 40) -> list[dict]:
        """Active GST locations (OLCT)."""
        rows = self._query(
            """
            SELECT TOP {limit} "AbsEntry", "Name"
            FROM "{schema}"."OLCT"
            WHERE "Inactive" = 'N'
              AND (UPPER(CAST("AbsEntry" AS NVARCHAR(20))) LIKE ? OR UPPER("Name") LIKE ?)
            ORDER BY "AbsEntry"
            """,
            (_like(search), _like(search)),
            limit=_limit(limit, 40, 200),
        )
        return [{"code": str(entry), "name": _clean(name) or str(entry)} for entry, name in rows]

    def tax_codes(self) -> list[dict]:
        """Unlocked tax codes (OSTC) with their rate."""
        rows = self._query(
            """
            SELECT "Code", "Name", "Rate"
            FROM "{schema}"."OSTC"
            WHERE "Locked" = 'N'
            ORDER BY "Code"
            """,
            (),
        )
        return [
            {"code": _clean(code), "name": _clean(name) or _clean(code), "rate": float(rate or 0)}
            for code, name, rate in rows
        ]

    def costing_codes(self, dimension: int, search: str = "", limit: int = 100) -> list[dict]:
        """Unlocked distribution rules (OPRC) of one cost dimension (1–5)."""
        try:
            dimension = int(dimension)
        except (TypeError, ValueError):
            raise SAPValidationError("Cost dimension must be a number from 1 to 5.")
        if dimension not in range(1, 6):
            raise SAPValidationError("Cost dimension must be a number from 1 to 5.")
        rows = self._query(
            """
            SELECT TOP {limit} "PrcCode", "PrcName"
            FROM "{schema}"."OPRC"
            WHERE "DimCode" = ? AND "Locked" = 'N'
              AND (UPPER("PrcCode") LIKE ? OR UPPER("PrcName") LIKE ?)
            ORDER BY "PrcCode"
            """,
            (dimension, _like(search), _like(search)),
            limit=_limit(limit, 100, 500),
        )
        return [{"code": _clean(code), "name": _clean(name) or _clean(code)} for code, name in rows]

    def branches(self) -> list[dict]:
        """Enabled branches (OBPL) with their GSTIN."""
        rows = self._query(
            """
            SELECT "BPLId", "BPLName", "TaxIdNum"
            FROM "{schema}"."OBPL"
            WHERE "Disabled" = 'N'
            ORDER BY "BPLName"
            """,
            (),
        )
        return [
            {"branch_id": int(bpl_id), "branch_name": _clean(name) or str(bpl_id), "tax_id": _clean(tax_id)}
            for bpl_id, name, tax_id in rows
        ]

    def resources(self, search: str = "", limit: int = 50) -> list[dict]:
        """Production resources (ORSC) — machines and labour on a BOM or order."""
        rows = self._query(
            """
            SELECT TOP {limit} "ResCode", "ResName"
            FROM "{schema}"."ORSC"
            WHERE UPPER("ResCode") LIKE ? OR UPPER("ResName") LIKE ?
            ORDER BY "ResCode"
            """,
            (_like(search), _like(search)),
            limit=_limit(limit, 50, 200),
        )
        return [{"code": _clean(code), "name": _clean(name) or _clean(code)} for code, name in rows]

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def gl_accounts(self, search: str = "", limit: int = 30) -> list[dict]:
        """Postable G/L accounts (OACT) whose code or name contains ``search``."""
        rows = self._query(
            """
            SELECT TOP {limit} "AcctCode", "AcctName"
            FROM "{schema}"."OACT"
            WHERE "Postable" = 'Y' AND (UPPER("AcctCode") LIKE ? OR UPPER("AcctName") LIKE ?)
            ORDER BY "AcctCode"
            """,
            (_like(search), _like(search)),
            limit=_limit(limit, 30, 200),
        )
        return [{"code": _clean(code), "name": _clean(name)} for code, name in rows]

    def ar_accounts(self) -> list[dict]:
        """Customer control accounts (children of 1101000, Sundry Debtors)."""
        return self._accounts(_AR_ACCOUNT_FILTER)

    def ap_accounts(self) -> list[dict]:
        """Vendor control accounts (children of 2101000 and the 211* range, not cash)."""
        return self._accounts(_AP_ACCOUNT_FILTER)

    def _accounts(self, condition: str) -> list[dict]:
        rows = self._query(
            f"""
            SELECT "AcctCode", "AcctName"
            FROM "{{schema}}"."OACT"
            WHERE {condition}
            ORDER BY "AcctCode"
            """,
            (),
        )
        return [{"code": _clean(code), "name": _clean(name)} for code, name in rows]

    # ------------------------------------------------------------------
    # Business partners
    # ------------------------------------------------------------------

    def search_business_partners(
        self, search: str = "", card_type: str | None = None, limit: int = 30
    ) -> list[dict]:
        """Active partners (OCRD) by code or name; ``card_type`` 'C' or 'S' narrows."""
        clauses = ['"validFor" = \'Y\'', '(UPPER("CardCode") LIKE ? OR UPPER("CardName") LIKE ?)']
        params: list = [_like(search), _like(search)]
        if card_type:
            if card_type not in _CARD_TYPES:
                raise SAPValidationError("Partner type must be 'C' (customer) or 'S' (vendor).")
            clauses.append('"CardType" = ?')
            params.append(card_type)
        rows = self._query(
            f"""
            SELECT TOP {{limit}} "CardCode", "CardName", "CardType"
            FROM "{{schema}}"."OCRD"
            WHERE {" AND ".join(clauses)}
            ORDER BY "CardName"
            """,
            tuple(params),
            limit=_limit(limit, 30, 200),
        )
        return [
            {"card_code": _clean(code), "card_name": _clean(name), "card_type": _clean(kind)}
            for code, name, kind in rows
        ]

    def business_partner(self, card_code: str) -> dict | None:
        """One partner by exact code, whatever its state — for duplicate checks."""
        rows = self._query(
            """
            SELECT "CardCode", "CardName", "CardType", "validFor", "frozenFor"
            FROM "{schema}"."OCRD"
            WHERE "CardCode" = ?
            """,
            (card_code,),
        )
        if not rows:
            return None
        code, name, kind, valid_for, frozen_for = rows[0]
        return {
            "card_code": _clean(code),
            "card_name": _clean(name),
            "card_type": _clean(kind),
            "active": _clean(valid_for) == "Y" and _clean(frozen_for) != "Y",
        }

    def partners_with_tax_ids(
        self, card_type: str, gstin: str = "", pan: str = ""
    ) -> list[dict]:
        """Existing partners of one type already registered under this GSTIN or PAN.

        Registration asks this before creating a partner: SAP accepts a second
        business partner with the same GSTIN, and the portal created such
        duplicates whenever an approval was clicked twice. GSTIN is read where
        the invoice prints read it (``CRD1.GSTRegnNo``, per address); PAN where
        the portal wrote it (``CRD7.TaxId0``, through ``BPFiscalTaxIDCollection``).
        """
        if card_type not in _CARD_TYPES:
            raise SAPValidationError("Partner type must be 'C' (customer) or 'S' (vendor).")
        found: dict[str, dict] = {}
        checks = (
            ((gstin or "").strip().upper(), "CRD1", '"GSTRegnNo"', "GSTIN"),
            ((pan or "").strip().upper(), "CRD7", '"TaxId0"', "PAN"),
        )
        for value, table, column, label in checks:
            if not value:
                continue
            rows = self._query(
                f"""
                SELECT DISTINCT C."CardCode", C."CardName"
                FROM "{{schema}}"."OCRD" C
                JOIN "{{schema}}"."{table}" T ON T."CardCode" = C."CardCode"
                WHERE C."CardType" = ? AND UPPER(T.{column}) = ?
                ORDER BY C."CardCode"
                """,
                (card_type, value),
            )
            for code, name in rows:
                entry = found.setdefault(
                    _clean(code), {"card_code": _clean(code), "card_name": _clean(name), "matched_on": []}
                )
                entry["matched_on"].append(label)
        return list(found.values())

    def next_card_code(self, prefix: str, card_type: str) -> str:
        """The next free card code under ``prefix`` — highest existing plus one.

        Same rule as the portal (``VENDA000123`` → ``VENDA000124``, width kept,
        six digits when the prefix is unused), but it raises when SAP can't be
        read instead of inventing a code from the clock. The caller still
        re-checks the code right before creating the partner.
        """
        prefix = (prefix or "").strip().upper()
        if not _CARD_CODE_PREFIX.match(prefix):
            raise SAPValidationError("A card-code prefix is 1–10 letters or digits.")
        if card_type not in _CARD_TYPES:
            raise SAPValidationError("Partner type must be 'C' (customer) or 'S' (vendor).")
        rows = self._query(
            """
            SELECT MAX("CardCode")
            FROM "{schema}"."OCRD"
            WHERE "CardCode" LIKE ? AND "CardType" = ?
            """,
            (f"{prefix}%", card_type),
        )
        last = _clean(rows[0][0]) if rows and rows[0][0] else ""
        digits = re.sub(r"\D", "", last[len(prefix):]) if last else ""
        width = len(digits) or 6
        return f"{prefix}{str((int(digits) if digits else 0) + 1).zfill(width)}"

    def bp_groups(self, card_type: str) -> list[dict]:
        """Business-partner groups (OCRG) of one side."""
        if card_type not in _GROUP_TYPES:
            raise SAPValidationError("Partner type must be 'C' (customer) or 'S' (vendor).")
        rows = self._query(
            """
            SELECT "GroupCode", "GroupName"
            FROM "{schema}"."OCRG"
            WHERE "GroupType" = ?
            ORDER BY "GroupName"
            """,
            (card_type,),
        )
        return [{"code": int(code), "name": _clean(name) or str(code)} for code, name in rows]

    def sales_employees(self) -> list[dict]:
        """Unlocked sales employees (OSLP), without the "-No Sales Employee-" row."""
        rows = self._query(
            """
            SELECT "SlpCode", "SlpName"
            FROM "{schema}"."OSLP"
            WHERE "SlpCode" > 0 AND "Locked" = 'N'
            ORDER BY "SlpName"
            """,
            (),
        )
        return [{"code": int(code), "name": _clean(name)} for code, name in rows]

    def payment_terms(self) -> list[dict]:
        """Payment terms (OCTG)."""
        rows = self._query(
            """
            SELECT "GroupNum", "PymntGroup"
            FROM "{schema}"."OCTG"
            ORDER BY "PymntGroup"
            """,
            (),
        )
        return [{"code": int(code), "name": _clean(name)} for code, name in rows]

    def states(self, country: str = "IN") -> list[dict]:
        """States of one country (OCST), for partner addresses."""
        rows = self._query(
            """
            SELECT "Code", "Name"
            FROM "{schema}"."OCST"
            WHERE "Country" = ?
            ORDER BY "Name"
            """,
            ((country or "IN").strip().upper(),),
        )
        return [{"code": _clean(code), "name": _clean(name) or _clean(code)} for code, name in rows]

    def user_table_values(self, key: str) -> tuple[list[dict], str | None]:
        """Rows of a whitelisted user-defined table, plus a warning if it is missing.

        Returns ``([], warning)`` rather than raising when the company has no
        such table: these feed optional classification fields on the
        registration forms, which must still open without them.
        """
        table = USER_TABLES.get(key)
        if not table:
            raise SAPValidationError(f"Unknown lookup table: {key}")
        try:
            rows = self._query(
                f"""
                SELECT "Code", "Name"
                FROM "{{schema}}"."{table}"
                ORDER BY "Code"
                """,
                (),
            )
        except SAPDataError:
            return [], f"This company has no {table} table in SAP."
        return [{"code": _clean(code), "name": _clean(name) or _clean(code)} for code, name in rows], None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple, limit: int | None = None) -> list:
        """Run one read; ``{schema}`` and ``{limit}`` are the only interpolations."""
        statement = sql.replace("{schema}", self.connection.schema)
        if limit is not None:
            statement = statement.replace("{limit}", str(int(limit)))
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading a lookup: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e
        try:
            cursor = conn.cursor()
            cursor.execute(statement, params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA lookup query failed: %s", e)
            raise SAPDataError("Failed to read SAP master data.") from e
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
