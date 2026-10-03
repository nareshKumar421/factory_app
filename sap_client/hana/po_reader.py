import logging
from datetime import date
from typing import Dict, List, Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..dtos import PODTO, POAdditionalExpenseDTO, POItemDTO
from ..exceptions import SAPConnectionError, SAPDataError, SAPUnavailable

logger = logging.getLogger(__name__)

# POR3/PDN3 store the distribution rule as a single char; the Service Layer wants
# the enum name. Only 'Q' and 'N' actually occur across the three companies, but
# map the full set so an unusual PO does not silently lose its rule.
DISTRIBUTION_METHOD_BY_CODE = {
    "N": "aedm_None",
    "Q": "aedm_Quantity",
    "V": "aedm_Volume",
    "W": "aedm_Weight",
    "E": "aedm_Equally",
    "R": "aedm_Row",
}


# The open-line columns, in the order ``_transform_to_dtos`` reads them -- the
# same order every open-PO query below selects them in. The copy (sap_mirror)
# stores rows in exactly this shape, so a copied answer goes through the same
# transform as a live one.
OPEN_LINE_COLUMNS = """
                    T0."DocNum"        AS po_number,
                    T0."CardCode"      AS supplier_code,
                    T0."CardName"      AS supplier_name,
                    T1."ItemCode"      AS po_item_code,
                    T1."Dscription"    AS item_name,
                    T1."Quantity"      AS ordered_qty,
                    (T1."Quantity" - T1."OpenQty") AS received_qty,
                    T1."OpenQty"       AS remaining_qty,
                    T1."unitMsr"       AS uom,
                    T1."Price"         AS rate,
                    T0."DocEntry"      AS doc_entry,
                    T1."LineNum"       AS line_num,
                    IFNULL(T1."TaxCode", '')   AS tax_code,
                    IFNULL(T1."WhsCode", '')   AS warehouse_code,
                    IFNULL(T1."AcctCode", '')  AS account_code,
                    T0."BPLId"         AS branch_id,
                    IFNULL(T0."NumAtCard", '') AS vendor_ref,
                    T0."DocDate"       AS po_date,
                    IFNULL(T1."OcrCode", '') AS variety"""


class HanaPOReader:
    """Reads purchase orders from SAP HANA.

    When HANA cannot be reached, the gate's three reads -- a supplier's open
    POs, its finished-goods POs, and a PO by number -- are answered from the
    copy of open PO lines (``sap_mirror.purchase_orders``, at most 15 minutes
    old). A PO number the copy does not hold still fails as SAP being
    unavailable. ``use_copy=False`` is for the job that takes the copy.
    """

    def __init__(self, context, *, use_copy: bool = True):
        self.connection = HanaConnection(context.hana)
        company_code = getattr(context, "company_code", None)
        self._copy_company = company_code if use_copy and isinstance(company_code, str) else None

    def _from_copy(self, exc, **lookup):
        """Open PO rows from the copy for a failed read, or re-raise ``exc``."""
        from sap_mirror.services import hana_unreachable

        if not self._copy_company or not hana_unreachable(exc):
            raise exc
        try:
            from sap_mirror import purchase_orders as po_copy

            rows = po_copy.open_po_rows(self._copy_company, **lookup)
        except Exception:  # noqa: BLE001 -- a broken copy must not change what the caller sees
            logger.exception("SAP PO copy could not answer for %s", self._copy_company)
            raise exc
        if rows is None:
            raise exc
        logger.warning("HANA unreachable; open POs for %s answered from the copy", self._copy_company)
        return rows

    def get_open_pos(self, supplier_code: str) -> List[PODTO]:
        try:
            return self._open_pos_live(supplier_code)
        except (SAPConnectionError, SAPDataError) as exc:
            return self._transform_to_dtos(self._from_copy(exc, supplier_code=supplier_code))

    def get_open_finished_goods_pos(self, supplier_code: str) -> List[PODTO]:
        """Open PO lines for a supplier restricted to FINISHED-GOODS items; see
        ``_open_finished_goods_pos_live``."""
        try:
            return self._open_finished_goods_pos_live(supplier_code)
        except (SAPConnectionError, SAPDataError) as exc:
            return self._transform_to_dtos(
                self._from_copy(exc, supplier_code=supplier_code, fg_only=True)
            )

    def get_open_po_by_number(self, po_number: str) -> Optional[PODTO]:
        try:
            return self._open_po_by_number_live(po_number)
        except (SAPConnectionError, SAPDataError) as exc:
            po_list = self._transform_to_dtos(self._from_copy(exc, po_number=po_number))
            return po_list[0] if po_list else None

    # -- for the copy (sap_mirror.purchase_orders) ----------------------------

    def open_po_versions(self) -> Dict[int, str]:
        """``DocEntry -> version`` for every PO with an open line.

        The version moves when the PO is edited (``UpdateDate``/``UpdateTS``)
        and when it is received against (the open quantity and the number of
        open lines), so the copy re-reads a PO exactly when it changed.
        """
        schema = self.connection.schema
        has_ts = bool(self._fetch(
            'SELECT 1 FROM "SYS"."TABLE_COLUMNS" WHERE "SCHEMA_NAME" = ? '
            'AND "TABLE_NAME" = \'OPOR\' AND "COLUMN_NAME" = \'UpdateTS\'',
            [schema],
        ))
        update_ts = 'T0."UpdateTS"' if has_ts else "NULL"
        group_ts = ', T0."UpdateTS"' if has_ts else ""
        rows = self._fetch(
            f"""
            SELECT T0."DocEntry", T0."UpdateDate", {update_ts},
                   SUM(T1."OpenQty"), COUNT(T1."LineNum")
            FROM "{schema}"."OPOR" T0
            JOIN "{schema}"."POR1" T1 ON T0."DocEntry" = T1."DocEntry"
            WHERE T1."OpenQty" > 0
            GROUP BY T0."DocEntry", T0."UpdateDate"{group_ts}
            """,
            [],
        )
        return {
            int(row[0]): (
                f"{row[1].isoformat() if hasattr(row[1], 'isoformat') else row[1] or ''}"
                f"|{'' if row[2] is None else row[2]}|{row[3]}|{row[4]}"
            )[:80]
            for row in rows
        }

    def open_po_rows_for(self, doc_entries) -> List[list]:
        """The open lines of these POs, each an ``OPEN_LINE_COLUMNS`` row plus a
        last flag: 1 for a finished-goods line (``get_open_finished_goods_pos``'s
        test), 0 otherwise."""
        entries = [int(entry) for entry in doc_entries or []]
        if not entries:
            return []
        schema = self.connection.schema
        placeholders = ", ".join("?" for _ in entries)
        return [list(row) for row in self._fetch(
            f"""
            SELECT {OPEN_LINE_COLUMNS},
                   CASE WHEN T2."ItmsGrpCod" = 102 AND T1."ItemCode" LIKE 'FG%'
                        THEN 1 ELSE 0 END AS is_fg
            FROM "{schema}"."OPOR" T0
            JOIN "{schema}"."POR1" T1 ON T0."DocEntry" = T1."DocEntry"
            LEFT JOIN "{schema}"."OITM" T2 ON T1."ItemCode" = T2."ItemCode"
            WHERE T0."DocEntry" IN ({placeholders})
              AND T1."OpenQty" > 0
            ORDER BY T0."DocEntry", T1."LineNum"
            """,
            entries,
        )]

    def _fetch(self, query: str, params) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            raise SAPConnectionError("Unable to connect to SAP HANA. Please try again later.") from e
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error(f"SAP HANA PO copy read failed: {e}")
            raise SAPDataError("Failed to read open purchase orders from SAP.") from e
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

    def get_po_date_by_doc_entry(self, doc_entry: int) -> Optional[date]:
        """Fetch OPOR.DocDate for a given PO DocEntry. Returns None if not found."""
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
            cursor = conn.cursor()
            schema = self.connection.schema
            cursor.execute(
                f'SELECT T0."DocDate" FROM "{schema}"."OPOR" T0 WHERE T0."DocEntry" = ?',
                doc_entry,
            )
            row = cursor.fetchone()
            return row[0] if row else None
        except dbapi.Error as e:
            logger.warning(f"SAP HANA DocDate lookup failed for doc_entry={doc_entry}: {e}")
            return None
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

    def get_po_open_qtys(self, doc_entries) -> Dict[tuple, float]:
        """``{(doc_entry, line_num): OpenQty}`` for the given PO DocEntries.

        Deliberately *unfiltered* on ``OpenQty > 0``, unlike ``get_open_pos``: a line
        that has been fully received must come back as 0 so the caller refuses it,
        rather than going missing and being read as "no data, allow it".

        Raises ``SAPDataError`` on failure. Callers use this to re-check the
        over-receipt tolerance immediately before posting, so a silent empty result
        would defeat the check.
        """
        doc_entries = [int(entry) for entry in doc_entries if entry is not None]
        if not doc_entries:
            return {}

        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
            cursor = conn.cursor()
            schema = self.connection.schema
            placeholders = ",".join("?" for _ in doc_entries)
            cursor.execute(
                f"""
                    SELECT T1."DocEntry", T1."LineNum", T1."OpenQty"
                    FROM "{schema}"."POR1" T1
                    WHERE T1."DocEntry" IN ({placeholders})
                """,
                doc_entries,
            )
            return {
                (int(row[0]), int(row[1])): float(row[2] or 0)
                for row in cursor.fetchall()
            }
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA open-qty lookup failed for %s: %s", doc_entries, e)
            raise SAPDataError(f"Could not read open PO quantities from SAP: {e}")
        except dbapi.Error as e:
            # Not the query: HANA refused or dropped the connection. That is SAP
            # being down, which a caller may wait out, not bad data it must stop on.
            logger.error("SAP HANA unreachable reading open PO quantities: %s", e)
            raise SAPUnavailable(f"Could not reach SAP HANA to read open PO quantities: {e}")
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

    def get_po_additional_expenses(
        self, doc_entries: List[int]
    ) -> Dict[int, List[POAdditionalExpenseDTO]]:
        """PO additional-expense (freight) lines for the given PO DocEntries.

        Returns ``{po_doc_entry: [POAdditionalExpenseDTO, ...]}``. Freight is
        negotiated on the PO, so the GRPO screen reads it from here rather than
        asking the operator for an expense code they cannot know.

        ``posted_amount`` is summed from PDN3 by base linkage (BaseType 22 +
        BaseAbsEnt + BaseLnNum) because POR3."DrawnTotal" is not maintained in
        these company databases. Without it a second GRPO against the same PO
        would re-prefill the full charge and double-bill it.

        Fail-soft: a HANA problem returns ``{}`` so the GRPO preview still
        renders — the operator can then add the charge by hand as before.
        """
        if not doc_entries:
            return {}

        unique_entries = sorted({int(e) for e in doc_entries if e})
        if not unique_entries:
            return {}

        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
            cursor = conn.cursor()
            schema = self.connection.schema
            placeholders = ", ".join(["?"] * len(unique_entries))

            cursor.execute(
                f"""
                SELECT
                    T0."DocEntry"                  AS po_doc_entry,
                    T0."LineNum"                   AS line_num,
                    T0."ExpnsCode"                 AS expense_code,
                    IFNULL(T1."ExpnsName", '')     AS expense_name,
                    IFNULL(T0."LineTotal", 0)      AS amount,
                    IFNULL(T0."TaxCode", '')       AS tax_code,
                    IFNULL(T0."DistrbMthd", '')    AS distribution_method,
                    IFNULL(T0."Comments", '')      AS remarks,
                    IFNULL(T0."Status", '')        AS status,
                    IFNULL(T1."ExpnsAcct", '')     AS expense_account,
                    IFNULL(T1."SacCode", '')       AS sac_code,
                    IFNULL((
                        SELECT SUM(T2."LineTotal")
                        FROM "{schema}"."PDN3" T2
                        JOIN "{schema}"."OPDN" T3 ON T3."DocEntry" = T2."DocEntry"
                        WHERE T2."BaseType" = 22
                          AND T2."BaseAbsEnt" = T0."DocEntry"
                          AND T2."BaseLnNum" = T0."LineNum"
                          AND T3."CANCELED" = 'N'
                    ), 0)                          AS posted_amount
                FROM "{schema}"."POR3" T0
                LEFT JOIN "{schema}"."OEXD" T1
                       ON T1."ExpnsCode" = T0."ExpnsCode"
                WHERE T0."DocEntry" IN ({placeholders})
                  AND IFNULL(T0."LineTotal", 0) <> 0
                ORDER BY T0."DocEntry", T0."LineNum"
                """,
                unique_entries,
            )

            expenses: Dict[int, List[POAdditionalExpenseDTO]] = {}
            for row in cursor.fetchall():
                po_doc_entry = int(row[0])
                amount = float(row[4])
                posted_amount = float(row[11])
                expenses.setdefault(po_doc_entry, []).append(
                    POAdditionalExpenseDTO(
                        expense_code=int(row[2]),
                        # Fall back to the raw code when OEXD has no row, so the
                        # screen never shows a nameless charge.
                        expense_name=row[3] or f"Expense {int(row[2])}",
                        amount=amount,
                        line_num=int(row[1]),
                        tax_code=row[5] or "",
                        distribution_method=DISTRIBUTION_METHOD_BY_CODE.get(
                            row[6] or "", ""
                        ),
                        remarks=row[7] or "",
                        status=row[8] or "",
                        expense_account=row[9] or "",
                        sac_code=row[10] or "",
                        posted_amount=posted_amount,
                        remaining_amount=max(amount - posted_amount, 0.0),
                    )
                )
            return expenses

        except dbapi.Error as e:
            logger.warning(
                f"SAP HANA PO additional-expense lookup failed for "
                f"doc_entries={unique_entries}: {e}"
            )
            return {}
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

    def _open_pos_live(self, supplier_code: str) -> List[PODTO]:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            base_columns = f"""
                    T0."DocNum"        AS po_number,
                    T0."CardCode"      AS supplier_code,
                    T0."CardName"      AS supplier_name,
                    T1."ItemCode"      AS po_item_code,
                    T1."Dscription"    AS item_name,
                    T1."Quantity"      AS ordered_qty,
                    (T1."Quantity" - T1."OpenQty") AS received_qty,
                    T1."OpenQty"       AS remaining_qty,
                    T1."unitMsr"       AS uom,
                    T1."Price"         AS rate,
                    T0."DocEntry"      AS doc_entry,
                    T1."LineNum"       AS line_num,
                    IFNULL(T1."TaxCode", '')   AS tax_code,
                    IFNULL(T1."WhsCode", '')   AS warehouse_code,
                    IFNULL(T1."AcctCode", '')  AS account_code,
                    T0."BPLId"         AS branch_id,
                    IFNULL(T0."NumAtCard", '') AS vendor_ref,
                    T0."DocDate"       AS po_date"""

            from_clause = f"""
                FROM "{schema}"."OPOR" T0
                JOIN "{schema}"."POR1" T1 ON T0."DocEntry" = T1."DocEntry"
                WHERE T0."CardCode" = ?
                  AND T1."OpenQty" > 0"""

            query = f"SELECT {base_columns}, IFNULL(T1.\"OcrCode\", '') AS variety {from_clause}"
            cursor.execute(query, supplier_code)

            rows = cursor.fetchall()

            return self._transform_to_dtos(rows)

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error for supplier {supplier_code}: {e}")
            raise SAPDataError(
                "Failed to retrieve PO data from SAP. Invalid query or parameters."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for supplier {supplier_code}: {e}")
            raise SAPDataError(
                "Failed to retrieve PO data from SAP. Please try again later."
            ) from e
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

    def _open_finished_goods_pos_live(self, supplier_code: str) -> List[PODTO]:
        """Open PO lines for a supplier restricted to FINISHED-GOODS items.

        Traded/purchased finished goods sit in SAP item group 102 (FINISHED) with
        an ``FG`` code prefix (``FB*`` bundle SKUs share the group but are not real
        purchasable finished goods, so they are excluded). Same shape as
        ``get_open_pos`` but joined to OITM so only FG lines are returned.
        """
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            base_columns = f"""
                    T0."DocNum"        AS po_number,
                    T0."CardCode"      AS supplier_code,
                    T0."CardName"      AS supplier_name,
                    T1."ItemCode"      AS po_item_code,
                    T1."Dscription"    AS item_name,
                    T1."Quantity"      AS ordered_qty,
                    (T1."Quantity" - T1."OpenQty") AS received_qty,
                    T1."OpenQty"       AS remaining_qty,
                    T1."unitMsr"       AS uom,
                    T1."Price"         AS rate,
                    T0."DocEntry"      AS doc_entry,
                    T1."LineNum"       AS line_num,
                    IFNULL(T1."TaxCode", '')   AS tax_code,
                    IFNULL(T1."WhsCode", '')   AS warehouse_code,
                    IFNULL(T1."AcctCode", '')  AS account_code,
                    T0."BPLId"         AS branch_id,
                    IFNULL(T0."NumAtCard", '') AS vendor_ref,
                    T0."DocDate"       AS po_date"""

            from_clause = f"""
                FROM "{schema}"."OPOR" T0
                JOIN "{schema}"."POR1" T1 ON T0."DocEntry" = T1."DocEntry"
                JOIN "{schema}"."OITM" T2 ON T1."ItemCode" = T2."ItemCode"
                WHERE T0."CardCode" = ?
                  AND T1."OpenQty" > 0
                  AND T2."ItmsGrpCod" = 102
                  AND T1."ItemCode" LIKE 'FG%'"""

            query = f"SELECT {base_columns}, IFNULL(T1.\"OcrCode\", '') AS variety {from_clause}"
            cursor.execute(query, supplier_code)

            rows = cursor.fetchall()

            return self._transform_to_dtos(rows)

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error (FG) for supplier {supplier_code}: {e}")
            raise SAPDataError(
                "Failed to retrieve PO data from SAP. Invalid query or parameters."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error (FG) for supplier {supplier_code}: {e}")
            raise SAPDataError(
                "Failed to retrieve PO data from SAP. Please try again later."
            ) from e
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

    def _open_po_by_number_live(self, po_number: str) -> Optional[PODTO]:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            base_columns = f"""
                    T0."DocNum"        AS po_number,
                    T0."CardCode"      AS supplier_code,
                    T0."CardName"      AS supplier_name,
                    T1."ItemCode"      AS po_item_code,
                    T1."Dscription"    AS item_name,
                    T1."Quantity"      AS ordered_qty,
                    (T1."Quantity" - T1."OpenQty") AS received_qty,
                    T1."OpenQty"       AS remaining_qty,
                    T1."unitMsr"       AS uom,
                    T1."Price"         AS rate,
                    T0."DocEntry"      AS doc_entry,
                    T1."LineNum"       AS line_num,
                    IFNULL(T1."TaxCode", '')   AS tax_code,
                    IFNULL(T1."WhsCode", '')   AS warehouse_code,
                    IFNULL(T1."AcctCode", '')  AS account_code,
                    T0."BPLId"         AS branch_id,
                    IFNULL(T0."NumAtCard", '') AS vendor_ref,
                    T0."DocDate"       AS po_date"""

            query = f"""
                SELECT {base_columns}, IFNULL(T1."OcrCode", '') AS variety
                FROM "{schema}"."OPOR" T0
                JOIN "{schema}"."POR1" T1 ON T0."DocEntry" = T1."DocEntry"
                WHERE TO_NVARCHAR(T0."DocNum") = ?
                  AND T1."OpenQty" > 0
            """
            cursor.execute(query, po_number)

            rows = cursor.fetchall()
            po_list = self._transform_to_dtos(rows)
            return po_list[0] if po_list else None

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error for PO {po_number}: {e}")
            raise SAPDataError(
                "Failed to retrieve PO data from SAP. Invalid query or parameters."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for PO {po_number}: {e}")
            raise SAPDataError(
                "Failed to retrieve PO data from SAP. Please try again later."
            ) from e
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

    def _transform_to_dtos(self, rows) -> List[PODTO]:
        """Group rows by PO and create PODTO objects with nested POItemDTO objects"""
        po_dict = {}

        for row in rows:
            po_number = row[0]
            supplier_code = row[1]
            supplier_name = row[2]
            doc_entry = int(row[10])
            line_num = int(row[11])
            tax_code = row[12] or ""
            warehouse_code = row[13] or ""
            account_code = row[14] or ""
            branch_id = int(row[15]) if row[15] is not None else None
            vendor_ref = row[16] or ""
            doc_date = row[17].date() if hasattr(row[17], "date") else row[17]
            variety = row[18] or ""

            item = POItemDTO(
                po_item_code=row[3],
                item_name=row[4],
                ordered_qty=float(row[5]),
                received_qty=float(row[6]),
                remaining_qty=float(row[7]),
                uom=row[8],
                rate=float(row[9]),
                line_num=line_num,
                tax_code=tax_code,
                warehouse_code=warehouse_code,
                account_code=account_code,
                variety=variety,
            )

            if po_number not in po_dict:
                po_dict[po_number] = {
                    'supplier_code': supplier_code,
                    'supplier_name': supplier_name,
                    'doc_entry': doc_entry,
                    'branch_id': branch_id,
                    'vendor_ref': vendor_ref,
                    'doc_date': doc_date,
                    'items': []
                }

            po_dict[po_number]['items'].append(item)

        po_list = []
        for po_number, po_data in po_dict.items():
            po_list.append(PODTO(
                po_number=str(po_number),
                supplier_code=po_data['supplier_code'],
                supplier_name=po_data['supplier_name'],
                items=po_data['items'],
                doc_entry=po_data['doc_entry'],
                branch_id=po_data['branch_id'],
                vendor_ref=po_data['vendor_ref'],
                doc_date=po_data['doc_date'],
            ))

        return po_list
