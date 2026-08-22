import logging
from typing import Any, Dict, List, Sequence, Set

from hdbcli import dbapi

from gate_core.services.box_packing import CSD_SQL_PREDICATE
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

# Safety ceiling for a single date-bounded bill query. A caller that doesn't ask
# for a specific row count gets the whole window up to this many rows; it exists
# only so a pathologically wide date range can't run away on the shared SAP box.
# ~3.5 years of the busiest company's invoices, i.e. effectively "everything" for
# any real dispatch window.
MAX_BILL_ROWS = 20000


class HanaDispatchBillReader:
    """Reads SAP B1 A/R invoices that act as dispatch bills."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)
        self._columns_cache: Dict[str, Set[str]] = {}

    def list_bills(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        query, params = self._build_bills_query(filters)
        rows = self._execute(query, params)
        return [self._map_bill_row(row) for row in rows]

    def get_bill_by_number(self, invoice_number: str) -> Dict[str, Any] | None:
        rows = self.list_bills({"invoice_doc_num": invoice_number, "limit": 1})
        if not rows:
            return None

        bill = rows[0]
        bill["items"] = self.list_bill_lines(bill["doc_entry"])
        return bill

    def list_bills_by_doc_entries(self, doc_entries: List[int]) -> List[Dict[str, Any]]:
        doc_entries = [int(doc_entry) for doc_entry in dict.fromkeys(doc_entries or [])]
        if not doc_entries:
            return []
        return self.list_bills(
            {
                "doc_entries": doc_entries,
                "limit": len(doc_entries),
            }
        )

    def list_bill_lines(self, doc_entry: int) -> List[Dict[str, Any]]:
        schema = self.connection.schema
        line_columns = self._table_columns("INV1")
        item_columns = self._table_columns("OITM")

        item_code = self._optional_line_string(line_columns, "ItemCode", "item_code")
        item_name = self._optional_line_string(line_columns, "Dscription", "item_name")
        quantity = self._optional_line_number(line_columns, "Quantity", "quantity")
        uom = self._optional_line_uom(line_columns)
        rate = self._optional_line_number(line_columns, "Price", "rate")
        line_total = self._optional_line_number(line_columns, "LineTotal", "line_total")
        gross_total = self._optional_line_number(line_columns, "GTotal", "gross_total")
        warehouse_code = self._optional_line_string(line_columns, "WhsCode", "warehouse_code")
        base_ref = self._optional_line_string(line_columns, "BaseRef", "base_ref")
        base_entry = self._optional_line_raw(line_columns, "BaseEntry", "base_entry")
        base_type = self._optional_line_raw(line_columns, "BaseType", "base_type")
        tax_code = self._optional_line_string(line_columns, "TaxCode", "tax_code")
        weight1_expr = self._line_number_expr(line_columns, "Weight1")
        weight2_expr = self._line_number_expr(line_columns, "Weight2")
        total_litres_expr = self._line_total_litres_expr(item_columns)
        box_split_expr = self._box_count_expr(item_columns)
        loose_split_expr = self._loose_quantity_expr(item_columns)
        gross_weight_expr = self._optional_item_number(item_columns, "U_Gross_Weight")
        pack_size_expr = self._sales_pack_size_expr(item_columns)
        sal_factor2_expr = self._optional_item_number(item_columns, "SalFactor2")

        query = f"""
            SELECT
                L."LineNum" AS line_num,
                {item_code},
                {item_name},
                {quantity},
                {uom},
                {rate},
                {line_total},
                {gross_total},
                {warehouse_code},
                {base_ref},
                {base_entry},
                {base_type},
                {tax_code},
                {total_litres_expr} AS total_litres,
                {box_split_expr} AS total_boxes,
                {loose_split_expr} AS total_loose,
                CASE
                    WHEN {weight1_expr} > 0 THEN {weight1_expr}
                    WHEN {weight2_expr} > 0 THEN {weight2_expr}
                    WHEN {gross_weight_expr} > 0
                        THEN IFNULL(L."Quantity", 0) * {gross_weight_expr} / {pack_size_expr}
                    ELSE 0
                END AS total_weight,
                {sal_factor2_expr} AS sal_factor2
            FROM "{schema}"."INV1" L
            LEFT JOIN "{schema}"."OITM" I
                ON I."ItemCode" = L."ItemCode"
            WHERE L."DocEntry" = ?
            ORDER BY L."LineNum"
        """
        rows = self._execute(query, [doc_entry])
        return [self._map_bill_line_row(row) for row in rows]

    def _build_bills_query(self, filters: Dict[str, Any]):
        schema = self.connection.schema
        header_columns = self._table_columns("OINV")
        tax_columns = self._table_columns("INV12")
        line_columns = self._table_columns("INV1")
        item_columns = self._table_columns("OITM")

        dispatch_date = self._optional_raw(
            header_columns, "U_Dipatch_Date", "sap_dispatch_date", "NULL"
        )
        bilty_date = self._optional_raw(
            header_columns, "U_BiltyDate", "sap_bilty_date", "NULL"
        )
        bilty_no = self._optional_string(
            header_columns, "U_BilltyNumber", "sap_bilty_no"
        )
        transporter_name = self._optional_string(
            header_columns, "U_TransporterName", "sap_transporter_name"
        )
        vehicle_no = self._optional_string(
            header_columns, "U_VehicleNoM", "sap_vehicle_no"
        )
        transporter_invoice = self._optional_string(
            header_columns, "U_TransporterInvoice", "sap_transporter_invoice"
        )
        lr_number = self._optional_string(
            header_columns, "U_LRNUmber", "sap_lr_number"
        )
        eway_bill = self._optional_table_string(
            tax_columns,
            [
                "EWayBillNo",
                "EWayBillNum",
                "EwbNo",
                "EWBNo",
                "U_EWAYBILL",
                "U_EWayBillNo",
                "U_EWBNo",
                "U_EwayBillNo",
            ],
            "A",
            "sap_eway_bill",
        )

        total_litres_expr = self._line_total_litres_expr(item_columns)
        box_split_expr = self._box_count_expr(item_columns)
        loose_split_expr = self._loose_quantity_expr(item_columns)
        gross_weight_expr = self._optional_item_number(item_columns, "U_Gross_Weight")
        pack_size_expr = self._sales_pack_size_expr(item_columns)

        where_clauses = ['H."CANCELED" = \'N\'']
        params: List[Any] = []

        doc_entries = [int(value) for value in filters.get("doc_entries") or []]
        invoice_doc_num = (filters.get("invoice_doc_num") or "").strip()
        if doc_entries:
            placeholders = ", ".join("?" for _ in doc_entries)
            where_clauses.append(f'H."DocEntry" IN ({placeholders})')
            params.extend(doc_entries)
        elif invoice_doc_num:
            where_clauses.append('TO_NVARCHAR(H."DocNum") = ?')
            params.append(invoice_doc_num)
        else:
            where_clauses.append('H."CreateDate" >= ?')
            params.append(filters["date_from"])
            where_clauses.append('H."CreateDate" <= ?')
            params.append(filters["date_to"])

        branch = (filters.get("branch") or "").strip()
        if branch:
            where_clauses.append(
                '(LOWER(IFNULL(H."BPLName", \'\')) = ? OR CAST(H."BPLId" AS NVARCHAR(30)) = ?)'
            )
            params.extend([branch.lower(), branch])

        # A caller may cap the result (e.g. the vehicle picker asks for 500). With
        # no explicit cap, return the whole date-bounded window so the dispatch
        # dashboard shows everything for the range, bounded only by the safety
        # ceiling above.
        raw_limit = filters.get("limit")
        limit = int(raw_limit) if raw_limit else MAX_BILL_ROWS
        limit = min(max(limit, 1), MAX_BILL_ROWS)

        # Restrict the line aggregation to the same headers the outer query
        # selects. Without this the CTE aggregates every line in INV1 (the whole
        # sales ledger) and only filters afterwards, turning a single-bill lookup
        # into a full-table scan. The header filter is reused verbatim, so its
        # bound params must be supplied twice (CTE subquery first, outer second).
        header_filter = " AND ".join(where_clauses)

        query = f"""
            WITH line_agg AS (
                SELECT
                    L."DocEntry" AS doc_entry,
                    COUNT(L."LineNum") AS line_count,
                    SUM(IFNULL(L."Quantity", 0)) AS total_quantity,
                    SUM({total_litres_expr}) AS total_litres,
                    SUM({box_split_expr}) AS total_boxes,
                    SUM({loose_split_expr}) AS total_loose,
                    SUM(
                        CASE
                            WHEN IFNULL(L."Weight1", 0) > 0 THEN IFNULL(L."Weight1", 0)
                            WHEN IFNULL(L."Weight2", 0) > 0 THEN IFNULL(L."Weight2", 0)
                            WHEN {gross_weight_expr} > 0
                                THEN IFNULL(L."Quantity", 0) * {gross_weight_expr} / {pack_size_expr}
                            ELSE 0
                        END
                    ) AS total_weight,
                    SUM(IFNULL(L."LineTotal", 0)) AS total_line_amount,
                    SUM(IFNULL(L."GTotal", 0)) AS total_gross_amount,
                    STRING_AGG(IFNULL(L."WhsCode", ''), ', ') AS warehouses,
                    STRING_AGG(
                        IFNULL(L."ItemCode", '') || ' - ' || IFNULL(L."Dscription", ''),
                        ', '
                    ) AS item_summary,
                    STRING_AGG(IFNULL(TO_NVARCHAR(L."BaseRef"), ''), ', ') AS base_refs
                FROM "{schema}"."INV1" L
                LEFT JOIN "{schema}"."OITM" I
                    ON I."ItemCode" = L."ItemCode"
                WHERE L."DocEntry" IN (
                    SELECT H."DocEntry"
                    FROM "{schema}"."OINV" H
                    WHERE {header_filter}
                )
                GROUP BY L."DocEntry"
            )
            SELECT
                H."DocEntry" AS doc_entry,
                TO_NVARCHAR(H."DocNum") AS doc_num,
                H."DocDate" AS doc_date,
                H."CreateDate" AS create_date,
                H."DocTime" AS doc_time,
                IFNULL(H."CardCode", '') AS card_code,
                IFNULL(H."CardName", '') AS card_name,
                IFNULL(H."DocTotal", 0) AS doc_total,
                H."BPLId" AS branch_id,
                IFNULL(H."BPLName", '') AS branch_name,
                IFNULL(H."ShipToCode", '') AS ship_to_code,
                IFNULL(H."Address2", '') AS ship_to_address,
                IFNULL(A."StateS", '') AS state,
                IFNULL(A."CityS", '') AS city,
                IFNULL(A."BpGSTN", '') AS bp_gstin,
                {dispatch_date},
                {bilty_no},
                {bilty_date},
                {transporter_name},
                {vehicle_no},
                {transporter_invoice},
                {lr_number},
                {eway_bill},
                IFNULL(A."Vehicle", '') AS gst_vehicle_no,
                A."TransprtDT" AS gst_transport_date,
                IFNULL(A."TransprtRS", '') AS gst_transport_reason,
                IFNULL(LA.line_count, 0) AS line_count,
                IFNULL(LA.total_quantity, 0) AS total_quantity,
                IFNULL(LA.total_litres, 0) AS total_litres,
                IFNULL(LA.total_boxes, 0) AS total_boxes,
                IFNULL(LA.total_loose, 0) AS total_loose,
                IFNULL(LA.total_weight, 0) AS total_weight,
                IFNULL(LA.total_line_amount, 0) AS total_line_amount,
                IFNULL(LA.total_gross_amount, 0) AS total_gross_amount,
                IFNULL(LA.warehouses, '') AS warehouses,
                IFNULL(LA.item_summary, '') AS item_summary,
                IFNULL(LA.base_refs, '') AS base_refs
            FROM "{schema}"."OINV" H
            LEFT JOIN "{schema}"."INV12" A
                ON A."DocEntry" = H."DocEntry"
            LEFT JOIN line_agg LA
                ON LA.doc_entry = H."DocEntry"
            WHERE {header_filter}
            ORDER BY H."CreateDate" DESC, H."DocTime" DESC, H."DocNum" DESC
            LIMIT {limit}
        """
        # header_filter is bound once inside the CTE and once in the outer WHERE.
        return query, params + params

    def _table_columns(self, table_name: str) -> Set[str]:
        key = table_name.upper()
        if key in self._columns_cache:
            return self._columns_cache[key]

        rows = self._execute(
            """
                SELECT "COLUMN_NAME"
                FROM "SYS"."TABLE_COLUMNS"
                WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = ?
            """,
            [self.connection.schema, key],
        )
        columns = {row[0] for row in rows}
        self._columns_cache[key] = columns
        return columns

    @staticmethod
    def _optional_string(columns: Set[str], column: str, alias: str) -> str:
        if column not in columns:
            return f"'' AS {alias}"
        return f'IFNULL(TO_NVARCHAR(H."{column}"), \'\') AS {alias}'

    @staticmethod
    def _optional_raw(
        columns: Set[str], column: str, alias: str, fallback: str = "NULL"
    ) -> str:
        if column not in columns:
            return f"{fallback} AS {alias}"
        return f'H."{column}" AS {alias}'

    @staticmethod
    def _optional_table_string(
        columns: Set[str],
        candidates: List[str],
        table_alias: str,
        alias: str,
    ) -> str:
        for column in candidates:
            if column in columns:
                return f'IFNULL(TO_NVARCHAR({table_alias}."{column}"), \'\') AS {alias}'
        return f"'' AS {alias}"

    @staticmethod
    def _optional_item_number(columns: Set[str], column: str) -> str:
        if column not in columns:
            return "0"
        return f'IFNULL(I."{column}", 0)'

    @staticmethod
    def _sales_pack_size_expr(item_columns: Set[str]) -> str:
        # OITM.SalFactor2 holds the number of base units (pieces) per sales
        # case/box. U_Gross_Weight is the gross weight of one such case while
        # the line quantity is in pieces, so the case count is qty / SalFactor2.
        # Default to 1 when the factor is missing or zero, which avoids a
        # divide-by-zero and falls back to treating the weight as per-piece.
        if "SalFactor2" not in item_columns:
            return "1"
        return 'CASE WHEN IFNULL(I."SalFactor2", 0) > 0 THEN IFNULL(I."SalFactor2", 0) ELSE 1 END'

    @staticmethod
    def _box_pieces_expr(item_columns: Set[str]) -> str:
        """Pieces per countable box, or 0 when the item is not boxed at all.

        SAP's own bill layout (procedure ``CRYSTAL_AR_INVOICE_ITEMS``) treats
        ``SalFactor2 = 1`` as "not transacted in boxes" and prints 0 boxes with the
        whole line loose -- which is why FG0000381 (500 pcs of a 10ML bottle) bills as
        "0 Box  500.00 PCS". CSD stock is the exception: it also carries SalFactor2 = 1,
        but there one box IS the billed piece, so it stays box-counted. Mirrors
        ``gate_core.services.box_packing.pieces_per_box``.
        """
        if "SalFactor2" not in item_columns:
            return "0"
        # The line's own description is what the bill prints; the item master name is
        # the fallback for a line SAP left blank. Either carrying the CSD token is
        # enough to treat the SKU as boxed.
        name = 'IFNULL(L."Dscription", \'\')'
        if "ItemName" in item_columns:
            name = 'IFNULL(L."Dscription", IFNULL(I."ItemName", \'\'))'
        csd = CSD_SQL_PREDICATE.format(name=name)
        return (
            'CASE WHEN IFNULL(I."SalFactor2", 0) > 1 THEN IFNULL(I."SalFactor2", 0) '
            f"WHEN {csd} THEN 1 ELSE 0 END"
        )

    @classmethod
    def _box_count_expr(cls, item_columns: Set[str]) -> str:
        """Full boxes on the line: FLOOR(qty / pieces-per-box), 0 for a loose item."""
        pieces = cls._box_pieces_expr(item_columns)
        return (
            f"CASE WHEN ({pieces}) > 0 "
            f'THEN FLOOR(IFNULL(L."Quantity", 0) / ({pieces})) ELSE 0 END'
        )

    @classmethod
    def _loose_quantity_expr(cls, item_columns: Set[str]) -> str:
        """Pieces not in a full box: the whole line when loose, else the remainder."""
        pieces = cls._box_pieces_expr(item_columns)
        return (
            f"CASE WHEN ({pieces}) > 0 "
            f'THEN IFNULL(L."Quantity", 0) - FLOOR(IFNULL(L."Quantity", 0) / ({pieces})) * ({pieces}) '
            f'ELSE IFNULL(L."Quantity", 0) END'
        )

    @staticmethod
    def _optional_item_string(columns: Set[str], column: str, fallback: str = "") -> str:
        if column not in columns:
            return f"'{fallback}'"
        return f'IFNULL(TO_NVARCHAR(I."{column}"), \'{fallback}\')'

    @classmethod
    def _litres_per_unit_expr(cls, item_columns: Set[str]) -> str:
        """Litres in ONE billed unit, straight from ``OITM.SalPackUn``.

        SalPackUn ("Items per Sales Unit") is where SAP records the volume of the
        thing a line is billed in, and it is populated for the whole item master:
        a 5 LTR tin reads 5, a 250 ML bottle 0.25, a "1 LTR + 1 LTR COMBO" set 2,
        a CSD carton the litres of the whole carton (16 x 1 L -> 16). It is the
        same field the monthly sales-litre reports run on.

        Never parse the item name for this. Names state the piece volume and the
        carton size separately and lie about both (combos read "1 LTR" but hold
        two, CSD cartons read "1 LTR 16 PCS" but bill as one 16 L unit), which is
        what made the old name/BOM/weight cascade under- and over-count.

        ``U_IsLitre`` is the gate: SalPackUn carries a number for every item,
        including cartons, preforms and labels, so without the flag a packaging
        line of 100,000 preforms would report 100,000 litres.
        """
        is_litre_expr = cls._optional_item_string(item_columns, "U_IsLitre", "N")
        if "SalPackUn" not in item_columns:
            return "0"
        return f"""
            CASE
                WHEN UPPER({is_litre_expr}) = 'Y' THEN IFNULL(I."SalPackUn", 0)
                ELSE 0
            END
        """

    @classmethod
    def _line_total_litres_expr(cls, item_columns: Set[str]) -> str:
        """Litres on the line: billed quantity x litres per billed unit."""
        return f'IFNULL(L."Quantity", 0) * ({cls._litres_per_unit_expr(item_columns)})'

    @staticmethod
    def _optional_line_string(columns: Set[str], column: str, alias: str) -> str:
        if column not in columns:
            return f"'' AS {alias}"
        return f'IFNULL(TO_NVARCHAR(L."{column}"), \'\') AS {alias}'

    @staticmethod
    def _optional_line_number(columns: Set[str], column: str, alias: str) -> str:
        if column not in columns:
            return f"0 AS {alias}"
        return f'IFNULL(L."{column}", 0) AS {alias}'

    @staticmethod
    def _optional_line_raw(columns: Set[str], column: str, alias: str) -> str:
        if column not in columns:
            return f"NULL AS {alias}"
        return f'L."{column}" AS {alias}'

    @staticmethod
    def _line_number_expr(columns: Set[str], column: str) -> str:
        if column not in columns:
            return "0"
        return f'IFNULL(L."{column}", 0)'

    @staticmethod
    def _optional_line_uom(columns: Set[str]) -> str:
        if "unitMsr" in columns:
            return 'IFNULL(TO_NVARCHAR(L."unitMsr"), \'\') AS uom'
        if "UomCode" in columns:
            return 'IFNULL(TO_NVARCHAR(L."UomCode"), \'\') AS uom'
        return "'' AS uom"

    def _map_bill_row(self, row: Sequence[Any]) -> Dict[str, Any]:
        return {
            "doc_entry": int(row[0]),
            "doc_num": row[1] or "",
            "doc_date": self._format_date(row[2]),
            "create_date": self._format_date(row[3]),
            "create_time": self._format_time(row[4]),
            "card_code": row[5] or "",
            "card_name": row[6] or "",
            "doc_total": float(row[7] or 0),
            "branch_id": int(row[8]) if row[8] is not None else None,
            "branch_name": row[9] or "",
            "ship_to_code": row[10] or "",
            "ship_to_address": row[11] or "",
            "state": row[12] or "",
            "city": row[13] or "",
            "bp_gstin": row[14] or "",
            "sap_dispatch_date": self._format_date(row[15]),
            "sap_bilty_no": row[16] or "",
            "sap_bilty_date": self._format_date(row[17]),
            "sap_transporter_name": row[18] or "",
            "sap_vehicle_no": row[19] or "",
            "sap_transporter_invoice": row[20] or "",
            "sap_lr_number": row[21] or "",
            "sap_eway_bill": row[22] or "",
            "gst_vehicle_no": row[23] or "",
            "gst_transport_date": self._format_date(row[24]),
            "gst_transport_reason": row[25] or "",
            "line_count": int(row[26] or 0),
            "total_quantity": float(row[27] or 0),
            "total_litres": float(row[28] or 0),
            "total_boxes": float(row[29] or 0),
            "total_loose": float(row[30] or 0),
            "total_weight": float(row[31] or 0),
            "total_line_amount": float(row[32] or 0),
            "total_gross_amount": float(row[33] or 0),
            "warehouses": self._dedupe_csv(row[34] or ""),
            "item_summary": row[35] or "",
            "base_refs": self._dedupe_csv(row[36] or ""),
        }

    @staticmethod
    def _map_bill_line_row(row: Sequence[Any]) -> Dict[str, Any]:
        return {
            "line_num": int(row[0] or 0),
            "item_code": row[1] or "",
            "item_name": row[2] or "",
            "quantity": float(row[3] or 0),
            "uom": row[4] or "",
            "rate": float(row[5] or 0),
            "line_total": float(row[6] or 0),
            "gross_total": float(row[7] or 0),
            "warehouse_code": row[8] or "",
            "base_ref": row[9] or "",
            "base_entry": int(row[10]) if row[10] is not None else None,
            "base_type": int(row[11]) if row[11] is not None else None,
            "tax_code": row[12] or "",
            "total_litres": float(row[13] or 0),
            "total_boxes": float(row[14] or 0),
            "total_loose": float(row[15] or 0),
            "total_weight": float(row[16] or 0),
            "sal_factor2": float(row[17] or 0),
        }

    def _execute(self, query: str, params: List[Any]) -> List:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed for dispatch plans: %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA dispatch plans query error: %s", e)
            raise SAPDataError(
                "Failed to retrieve dispatch bills from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error("SAP HANA dispatch plans data error: %s", e)
            raise SAPDataError(
                "Failed to retrieve dispatch bills from SAP. Please try again."
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

    @staticmethod
    def _dedupe_csv(value: str, separator: str = ", ") -> str:
        """De-duplicate a separator-joined string, preserving first-seen order.

        HANA's STRING_AGG has no DISTINCT, so line-level aggregates (e.g. base_refs,
        warehouses) repeat a value once per line. Collapse them here instead.
        """
        if not value:
            return ""
        seen = []
        for part in str(value).split(separator.strip()):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
        return separator.join(seen)

    @staticmethod
    def _format_date(value):
        if not value:
            return None
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        text = str(value).strip()
        return text or None

    @staticmethod
    def _format_time(value) -> str:
        if value in (None, ""):
            return ""
        try:
            value_int = int(value)
        except (TypeError, ValueError):
            return str(value)

        hours = value_int // 100
        minutes = value_int % 100
        return f"{hours:02d}:{minutes:02d}"
