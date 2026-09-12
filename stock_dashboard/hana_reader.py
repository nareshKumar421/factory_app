"""
stock_dashboard/hana_reader.py

Executes SAP HANA SQL queries for the Stock Dashboard.
Reads from SAP B1 HANA tables: OITW (Item Warehouses), OITM (Item Master).
"""

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

SLOW_MOVING_DAYS = 30


class HanaStockDashboardReader:
    """
    Reads stock level data directly from SAP HANA.

    Returns items with current OnHand quantities, warehouse code, and
    inventory UOM. Pagination is handled via LIMIT/OFFSET.
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)
        # Column names per SAP table, filled on first probe. See
        # `_table_columns` -- the weight field this reader needs is a UDF that
        # does not exist in every company's schema.
        self._columns_cache: Dict[str, Set[str]] = {}

    # Transaction types that represent real outbound usage/demand, not stock transfers.
    _CONSUMPTION_TRANS_TYPES = (15, 60, 202)  # Delivery, Goods Issue, Production Order

    # ------------------------------------------------------------------
    # Public Methods
    # ------------------------------------------------------------------

    def get_stock_levels(self, filters: Dict[str, Any], page: int = 1, page_size: int = 50) -> List[Dict]:
        """Returns one page of item-warehouse rows, ordered by warehouse then item code."""
        query, params = self._build_query(filters)
        offset = (page - 1) * page_size
        paginated_query = f"{query} LIMIT ? OFFSET ?"
        rows = self._execute(paginated_query, params + [page_size, offset])
        return [self._map_row(r) for r in rows]

    def get_warehouses(self) -> List[str]:
        """Returns sorted list of distinct warehouse codes present in OITW."""
        schema = self.connection.schema
        query = f"""
            SELECT DISTINCT w."WhsCode"
            FROM "{schema}"."OITW" w
            ORDER BY w."WhsCode" ASC
        """
        rows = self._execute(query, [])
        return [r[0] for r in rows if r[0]]

    def get_stock_stats(self, filters: Dict[str, Any]) -> Dict:
        """Returns total, healthy, low, and critical counts across the full filtered dataset."""
        query, params = self._build_stats_query(filters)
        rows = self._execute(query, params)
        row = rows[0] if rows else (0, 0, 0, 0)
        return {
            "total_items": int(row[0] or 0),
            "healthy_count": int(row[1] or 0),
            "low_count": int(row[2] or 0),
            "critical_count": int(row[3] or 0),
            # Tonnage is a grouped-query answer only. A single-warehouse read
            # reports it as unknown rather than as zero, so a caller cannot
            # mistake "not computed here" for "nothing under benchmark".
            "below_benchmark_tonnes": None,
            "unweighed_below_benchmark": 0,
        }

    def get_as_of_stock_levels(
        self,
        filters: Dict[str, Any],
        as_of_date,
        page: int = 1,
        page_size: int = 50,
    ) -> List[Dict]:
        """
        Reconstructs one page of item-warehouse rows as of a prior SAP posting date.

        This uses current OITW.OnHand minus OINM net movements after the selected
        date. Benchmark and item master fields remain current SAP master data.
        """
        query, params = self._build_as_of_query(filters, as_of_date)
        offset = (page - 1) * page_size
        paginated_query = f"{query} LIMIT ? OFFSET ?"
        rows = self._execute(paginated_query, params + [page_size, offset])
        return [self._map_row(r) for r in rows]

    def get_as_of_stock_stats(self, filters: Dict[str, Any], as_of_date) -> Dict:
        """Returns stats for the SAP movement reconstruction query."""
        query, params = self._build_as_of_stats_query(filters, as_of_date)
        rows = self._execute(query, params)
        row = rows[0] if rows else (0, 0, 0, 0)
        return {
            "total_items": int(row[0] or 0),
            "healthy_count": int(row[1] or 0),
            "low_count": int(row[2] or 0),
            "critical_count": int(row[3] or 0),
        }

    def get_warehouse_occupancy(
        self, warehouse: str, item_groups: Optional[List[int]] = None
    ) -> List[Dict]:
        """One row per SKU holding stock in `warehouse`, with the two pack fields.

        The Production Control board has to turn SAP's piece count into pallets,
        and SAP has no pallet unit anywhere to lean on: of the item master's four
        sales factors, ``SalFactor1`` is never set, ``SalFactor2`` is
        pieces-per-box, ``SalFactor3`` duplicates it on CSD items, and
        ``SalFactor4`` has been used to hold MRP -- so their product is
        meaningless and only ``SalFactor2`` may be read. The three UoM groups
        that exist are mass-to-volume density conversions assigned to bulk oils,
        with no pallet, case or box unit defined.

        So the conversion happens in the caller, from two fields returned here
        alongside the stock:

          - ``pieces_per_box`` -- OITM.SalFactor2. **A value of 1 means the SKU
            is not transacted in boxes at all**, so the caller must not divide
            those by a boxes-per-pallet figure; at BH-PF that would read 4,028
            jars of ghee as 100 pallets.
          - ``litres_per_piece`` -- OITM.SalPackUn, which is how the caller tells
            a 200-litre drum from a 15-litre can from a jar. Never parse the SKU
            name for volume: a "1 LTR + 1 LTR COMBO" piece holds two litres and
            a "13 KGS" pack states no volume at all.
          - ``gross_weight_per_case`` -- OITM.U_Gross_Weight, the gross weight in
            kg of one sales case, so a board can report a warehouse in tonnes.
            **Gross, not net**: it includes the packaging. Weight per piece is
            this over ``pieces_per_box``, matching the invoice reader's proven
            expression (``dispatch_plans/hana_reader.py``, cross-checked there
            against the Crystal subreport). Two rules for the caller: apply it
            only where ``uom`` is a piece unit -- on a row stocked in KG or LTR
            the on-hand figure is already a mass or a volume and dividing it by
            a pack factor means nothing -- and where it is ``None``, disclose the
            row instead of counting it as weightless.

        Rows with no stock are dropped. A negative on-hand is returned as-is
        rather than clamped, so a board can show that SAP is carrying a negative
        instead of silently reading it as an empty shelf.
        """
        schema = self.connection.schema
        gross_weight_expr = self._gross_weight_expr(self._table_columns("OITM"))

        # Group codes are validated as integers upstream, so they are inlined
        # rather than bound: HANA will not take a parameter list for IN, and
        # binding one placeholder per code would make the statement cache churn
        # on every distinct filter length.
        group_clause = ""
        if item_groups:
            codes = ", ".join(str(int(code)) for code in item_groups)
            group_clause = f'AND i."ItmsGrpCod" IN ({codes})'

        query = f"""
            SELECT
                w."ItemCode",
                i."ItemName",
                COALESCE(w."OnHand", 0)       AS "OnHand",
                COALESCE(i."SalFactor2", 0)   AS "PiecesPerBox",
                COALESCE(i."SalPackUn", 0)    AS "LitresPerPiece",
                COALESCE(w."StockValue", 0)   AS "StockValue",
                COALESCE(i."U_Sub_Group", '') AS "SubGroup",
                COALESCE(i."InvntryUom", '')  AS "Uom",
                {gross_weight_expr}           AS "GrossWeightPerCase"
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" i ON i."ItemCode" = w."ItemCode"
            WHERE w."WhsCode" = ?
              AND COALESCE(w."OnHand", 0) <> 0
              {group_clause}
            ORDER BY COALESCE(w."OnHand", 0) DESC
        """
        rows = self._execute(query, [warehouse])
        return [self._map_occupancy_row(r) for r in rows]

    def _table_columns(self, table_name: str) -> Set[str]:
        """The column names SAP actually has for `table_name`, cached per reader.

        Mirrors ``dispatch_plans.hana_reader.HanaDispatchReader._table_columns``.
        Needed because ``U_Gross_Weight`` is a user-defined field: naming it in a
        SELECT against a schema that does not carry it fails the whole query, so
        it has to be probed rather than assumed.
        """
        key = table_name.upper()
        # Tolerated rather than required: a query BUILDER must stay callable
        # without a live connection, and this probe is now reached from one.
        cache = getattr(self, "_columns_cache", None)
        if cache is None:
            cache = {}
            self._columns_cache = cache
        if key in cache:
            return cache[key]

        try:
            rows = self._execute(
                """
                    SELECT "COLUMN_NAME"
                    FROM "SYS"."TABLE_COLUMNS"
                    WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = ?
                """,
                [self.connection.schema, key],
            )
        except Exception:  # noqa: BLE001
            # Unknown reads as "this company has no user-defined columns",
            # which drops the optional weight to NULL. The callers already
            # disclose how many rows they could not weigh, so the figure
            # degrades into its own footnote rather than into a wrong total.
            logger.warning("Could not probe columns for %s; assuming none", key)
            cache[key] = set()
            return cache[key]

        columns = {row[0] for row in rows}
        cache[key] = columns
        return columns

    @staticmethod
    def _gross_weight_expr(item_columns: Set[str], alias: str = "i") -> str:
        """Gross weight of one sales case, in kg, or NULL where SAP has no field.

        NULL rather than 0 on purpose. The invoice-side reader answers the
        literal ``0`` for an absent UDF
        (``dispatch_plans.hana_reader._optional_item_number``), which is right
        there -- a weight of zero contributes nothing to a SUM. Here the caller
        has to tell "this item weighs nothing" apart from "this company does not
        record weights at all", because the second case must disclose itself
        rather than render a confident 0 t for the whole warehouse.
        """
        if "U_Gross_Weight" not in item_columns:
            return "CAST(NULL AS DECIMAL)"
        return f'NULLIF(COALESCE({alias}."U_Gross_Weight", 0), 0)'

    @staticmethod
    def _map_occupancy_row(row) -> Dict:
        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "on_hand": float(row[2] or 0),
            # None rather than 0 where SAP holds nothing, so the caller decides
            # what an unconfigured item means instead of dividing by a zero that
            # looks like a deliberate answer.
            "pieces_per_box": float(row[3] or 0) or None,
            "litres_per_piece": float(row[4] or 0) or None,
            "stock_value": float(row[5] or 0),
            "sub_group": row[6] or "",
            "uom": row[7] or "",
            # None where SAP records no case weight for the item, or where the
            # company has no U_Gross_Weight field at all -- see
            # `_gross_weight_expr`. The caller must disclose those rows rather
            # than treat them as weightless.
            "gross_weight_per_case": float(row[8]) if row[8] is not None else None,
        }

    def get_item_batches(self, item_code: str, warehouse: str) -> List[Dict]:
        """Every batch of one item standing in one warehouse, oldest make first.

        Answers "how old is this stock and when does it expire" for a single SKU,
        which is the question that follows any non-moving or occupancy row.

        Batch quantities are per warehouse (``OBTQ``) while the dates live on the
        batch master (``OBTN``), so both are needed -- ``OBTN.Quantity`` is the
        batch's whole life across every warehouse and would overstate a single
        floor.

        Three date fields matter and they are NOT interchangeable:

          - ``MnfDate`` -- when it was made. What the caller actually wants, and
            populated on about three quarters of BH-PF's batches.
          - ``InDate`` -- when it entered SAP. Always present, so it is the
            fallback, but it is a receipt date and can trail the make by days.
          - ``ExpDate`` -- expiry. Present exactly where ``MnfDate`` is.

        The gap is real: the batches missing a make date are the ones whose batch
        number was typed as a stray figure ("583.6796", "5165654"), so they have
        no shelf-life data at all. `mfg_date_source` says which date each row is
        reporting rather than letting a receipt date pass as a make date.
        """
        schema = self.connection.schema
        query = f"""
            SELECT
                n."DistNumber",
                q."Quantity",
                n."MnfDate",
                n."InDate",
                n."ExpDate",
                COALESCE(n."Notes", '')     AS "Notes",
                q."CommitQty",
                DAYS_BETWEEN(COALESCE(n."MnfDate", n."InDate"), CURRENT_DATE) AS "AgeDays",
                DAYS_BETWEEN(CURRENT_DATE, n."ExpDate")                       AS "DaysToExpiry"
            FROM "{schema}"."OBTQ" q
            JOIN "{schema}"."OBTN" n ON n."AbsEntry" = q."MdAbsEntry"
            WHERE q."ItemCode" = ?
              AND q."WhsCode" = ?
              AND COALESCE(q."Quantity", 0) <> 0
            ORDER BY COALESCE(n."MnfDate", n."InDate") ASC
        """
        rows = self._execute(query, [item_code, warehouse])
        return [self._map_batch_row(r) for r in rows]

    @staticmethod
    def _map_batch_row(row) -> Dict:
        def day(value):
            return value.strftime("%Y-%m-%d") if value else None

        mfg = day(row[2])
        return {
            "batch": row[0] or "",
            "quantity": float(row[1] or 0),
            # Null, never the receipt date dressed up as a make date.
            "mfg_date": mfg,
            "in_date": day(row[3]),
            "exp_date": day(row[4]),
            # Which date `age_days` was measured from, so the UI can say so.
            "mfg_date_source": "manufactured" if mfg else ("received" if row[3] else "unknown"),
            "notes": row[5] or "",
            "committed": float(row[6] or 0),
            "age_days": int(row[7]) if row[7] is not None else None,
            "days_to_expiry": int(row[8]) if row[8] is not None else None,
        }

    # What each OINM TransType means, for the ones that actually occur on a
    # finished-goods floor. Verified against BH-PF's own ledger.
    _TRANS_TYPE_LABELS = {
        13: "Sold on invoice",
        14: "Returned by customer",
        15: "Delivered",
        16: "Returned",
        20: "Received on PO",
        21: "Returned to supplier",
        59: "Received from production",
        60: "Issued to production",
        67: "Transferred",
        162: "Revalued",
        202: "Production order",
        10000071: "Stock posting",
    }

    def get_item_movements(self, item_code: str, warehouse: str, limit: int = 25) -> List[Dict]:
        """One item's recent movements through one warehouse, newest first.

        Direction is taken from the QUANTITY, never from the transaction type: a
        transfer (67) goes both ways, and on a production-finished floor it is
        just as often stock arriving as leaving. Rows that move no quantity at
        all -- revaluations, production-order postings -- are returned with
        direction ``NONE`` so a reader can see they happened without mistaking
        them for stock moving.
        """
        schema = self.connection.schema
        query = f"""
            SELECT TOP {int(limit)}
                "DocDate",
                "TransType",
                COALESCE("InQty", 0)  AS "InQty",
                COALESCE("OutQty", 0) AS "OutQty",
                COALESCE("BASE_REF", '') AS "BaseRef"
            FROM "{schema}"."OINM"
            WHERE "ItemCode" = ? AND "Warehouse" = ?
            ORDER BY "DocDate" DESC, "TransNum" DESC
        """
        rows = self._execute(query, [item_code, warehouse])
        return [self._map_movement_row(r) for r in rows]

    @classmethod
    def _map_movement_row(cls, row) -> Dict:
        trans_type = int(row[1] or 0)
        in_qty = float(row[2] or 0)
        out_qty = float(row[3] or 0)
        return {
            "date": row[0].strftime("%Y-%m-%d") if row[0] else None,
            "trans_type": trans_type,
            "label": cls._TRANS_TYPE_LABELS.get(trans_type, f"Type {trans_type}"),
            "in_qty": in_qty,
            "out_qty": out_qty,
            "direction": "IN" if in_qty > 0 else ("OUT" if out_qty > 0 else "NONE"),
            # OINM carries no DocNum -- BASE_REF is the source document's number.
            "doc_ref": row[4] or "",
        }

    def get_item_movement_ages(self, item_code: str, warehouse: str) -> Dict:
        """How long since this item last moved through the warehouse, both ways.

        Two ages, because they answer different questions and can differ wildly:

          - ``days_since_any`` -- since ANY movement, in or out. This is what the
            non-moving report ages on.
          - ``days_since_out`` -- since stock last LEFT. On a floor that goods
            are produced INTO, an inbound receipt is stock arriving, not stock
            moving, so an item can look freshly moved while nothing has shipped
            for months. BH-PF's 200-litre groundnut drum reads 69 days on the
            first measure and 152 on the second.

        A board that shows only the first understates how long stock has stood.
        """
        schema = self.connection.schema
        query = f"""
            SELECT
                MAX("DocDate")                                        AS "LastAny",
                MAX(CASE WHEN COALESCE("OutQty", 0) > 0
                         THEN "DocDate" END)                          AS "LastOut",
                MAX(CASE WHEN COALESCE("InQty", 0) > 0
                         THEN "DocDate" END)                          AS "LastIn"
            FROM "{schema}"."OINM"
            WHERE "ItemCode" = ? AND "Warehouse" = ?
        """
        rows = self._execute(query, [item_code, warehouse])
        row = rows[0] if rows else (None, None, None)

        def day(value):
            return value.strftime("%Y-%m-%d") if value else None

        def age(value):
            if not value:
                return None
            return (date.today() - value.date()).days

        return {
            "last_any_date": day(row[0]),
            "last_out_date": day(row[1]),
            "last_in_date": day(row[2]),
            "days_since_any": age(row[0]),
            "days_since_out": age(row[1]),
            "days_since_in": age(row[2]),
        }

    # ------------------------------------------------------------------
    # Query Builders
    # ------------------------------------------------------------------

    # Maps each status to its SQL condition based on the health thresholds.
    # Slow-moving rows are excluded from all stock health statuses.
    _UNGROUPED_REQUIRED_SQL = 'w."MinStock"'
    _UNGROUPED_DAYS_SQL = 'DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE)'
    _UNGROUPED_SLOW_SQL = (
        f'mov."LastConsumptionDate" IS NULL OR {_UNGROUPED_DAYS_SQL} > {SLOW_MOVING_DAYS}'
    )
    _UNGROUPED_NOT_SLOW_SQL = f'NOT ({_UNGROUPED_SLOW_SQL})'
    _UNGROUPED_SLOW_OPERATIONAL_SQL = f'{_UNGROUPED_REQUIRED_SQL} > 0 AND ({_UNGROUPED_SLOW_SQL})'
    _STATUS_SQL = {
        "unset":    f'{_UNGROUPED_REQUIRED_SQL} = 0 AND {_UNGROUPED_NOT_SLOW_SQL}',
        "healthy":  f'{_UNGROUPED_REQUIRED_SQL} > 0 AND w."OnHand" >= {_UNGROUPED_REQUIRED_SQL} AND {_UNGROUPED_NOT_SLOW_SQL}',
        "low":      f'{_UNGROUPED_REQUIRED_SQL} > 0 AND w."OnHand" < {_UNGROUPED_REQUIRED_SQL} AND w."OnHand" >= {_UNGROUPED_REQUIRED_SQL} * 0.6 AND {_UNGROUPED_NOT_SLOW_SQL}',
        "critical": f'{_UNGROUPED_REQUIRED_SQL} > 0 AND w."OnHand" < {_UNGROUPED_REQUIRED_SQL} * 0.6 AND {_UNGROUPED_NOT_SLOW_SQL}',
    }

    # Status conditions for aggregated (grouped) queries - uses SUM aliases.
    _GROUPED_REQUIRED_SQL = "min_stock"
    _GROUPED_DAYS_SQL = "days_since_last_consumption"
    _GROUPED_SLOW_SQL = (
        f"{_GROUPED_DAYS_SQL} IS NULL OR {_GROUPED_DAYS_SQL} > {SLOW_MOVING_DAYS}"
    )
    _GROUPED_NOT_SLOW_SQL = f"NOT ({_GROUPED_SLOW_SQL})"
    _GROUPED_SLOW_OPERATIONAL_SQL = f"{_GROUPED_REQUIRED_SQL} > 0 AND ({_GROUPED_SLOW_SQL})"

    _GROUPED_STATUS_SQL = {
        "unset":    f"{_GROUPED_REQUIRED_SQL} = 0 AND {_GROUPED_NOT_SLOW_SQL}",
        "healthy":  f"{_GROUPED_REQUIRED_SQL} > 0 AND on_hand >= {_GROUPED_REQUIRED_SQL} AND {_GROUPED_NOT_SLOW_SQL}",
        "low":      f"{_GROUPED_REQUIRED_SQL} > 0 AND on_hand < {_GROUPED_REQUIRED_SQL} AND on_hand >= {_GROUPED_REQUIRED_SQL} * 0.6 AND {_GROUPED_NOT_SLOW_SQL}",
        "critical": f"{_GROUPED_REQUIRED_SQL} > 0 AND on_hand < {_GROUPED_REQUIRED_SQL} * 0.6 AND {_GROUPED_NOT_SLOW_SQL}",
    }
    _AS_OF_REQUIRED_SQL = "min_stock"
    _AS_OF_DAYS_SQL = "days_since_last_consumption"
    _AS_OF_SLOW_SQL = (
        f"{_AS_OF_DAYS_SQL} IS NULL OR {_AS_OF_DAYS_SQL} > {SLOW_MOVING_DAYS}"
    )
    _AS_OF_NOT_SLOW_SQL = f"NOT ({_AS_OF_SLOW_SQL})"
    _AS_OF_SLOW_OPERATIONAL_SQL = f"{_AS_OF_REQUIRED_SQL} > 0 AND ({_AS_OF_SLOW_SQL})"
    _AS_OF_STATUS_SQL = {
        "unset":    f"{_AS_OF_REQUIRED_SQL} = 0 AND {_AS_OF_NOT_SLOW_SQL}",
        "healthy":  f"{_AS_OF_REQUIRED_SQL} > 0 AND on_hand >= {_AS_OF_REQUIRED_SQL} AND {_AS_OF_NOT_SLOW_SQL}",
        "low":      f"{_AS_OF_REQUIRED_SQL} > 0 AND on_hand < {_AS_OF_REQUIRED_SQL} AND on_hand >= {_AS_OF_REQUIRED_SQL} * 0.6 AND {_AS_OF_NOT_SLOW_SQL}",
        "critical": f"{_AS_OF_REQUIRED_SQL} > 0 AND on_hand < {_AS_OF_REQUIRED_SQL} * 0.6 AND {_AS_OF_NOT_SLOW_SQL}",
    }
    _DEFAULT_OPERATIONAL_STATUSES = {"healthy", "low", "critical"}

    # Maps frontend sort column names to SQL expressions
    _SORT_COL_SQL = {
        "item_code":    'w."ItemCode"',
        "item_name":    'm."ItemName"',
        "warehouse":    'w."WhsCode"',
        "on_hand":      'w."OnHand"',
        "min_stock":    'w."MinStock"',
        # health_ratio is computed, so we use the ratio expression directly
        "health_ratio": f'CASE WHEN {_UNGROUPED_REQUIRED_SQL} > 0 THEN w."OnHand" / {_UNGROUPED_REQUIRED_SQL} ELSE 0 END',
    }

    # For grouped queries the aliases are different
    _SORT_COL_GROUPED = {
        "item_code":    "item_code",
        "item_name":    "item_name",
        "warehouse":    "warehouse_count",
        "on_hand":      "on_hand",
        "min_stock":    "min_stock",
        "health_ratio": f"CASE WHEN {_GROUPED_REQUIRED_SQL} > 0 THEN on_hand / {_GROUPED_REQUIRED_SQL} ELSE 0 END",
    }

    _SORT_COL_AS_OF = {
        "item_code":    "item_code",
        "item_name":    "item_name",
        "warehouse":    "warehouse",
        "on_hand":      "on_hand",
        "min_stock":    "min_stock",
        "health_ratio": f"CASE WHEN {_AS_OF_REQUIRED_SQL} > 0 THEN on_hand / {_AS_OF_REQUIRED_SQL} ELSE 0 END",
    }

    def _build_order_by(self, filters: Dict[str, Any], grouped: bool = False) -> str:
        col = filters.get("sort_by", "health_ratio")
        direction = filters.get("sort_dir", "asc").upper()
        col_map = self._SORT_COL_GROUPED if grouped else self._SORT_COL_SQL
        sql_col = col_map.get(col, col_map["health_ratio"])
        return f"ORDER BY {sql_col} {direction}"

    def _build_as_of_order_by(self, filters: Dict[str, Any]) -> str:
        col = filters.get("sort_by", "health_ratio")
        direction = filters.get("sort_dir", "asc").upper()
        sql_col = self._SORT_COL_AS_OF.get(col, self._SORT_COL_AS_OF["health_ratio"])
        return f"ORDER BY {sql_col} {direction}"

    def _movement_joins(self, schema: str) -> str:
        consumption_types = ", ".join(str(t) for t in self._CONSUMPTION_TRANS_TYPES)
        return f"""
            LEFT JOIN (
                SELECT
                    n."ItemCode",
                    MAX(n."DocDate") AS "LastConsumptionDate"
                FROM "{schema}"."OINM" n
                WHERE n."OutQty" > 0
                  AND n."TransType" IN ({consumption_types})
                GROUP BY n."ItemCode"
            ) mov
                ON mov."ItemCode" = w."ItemCode"
        """

    def _as_of_movement_joins(self, schema: str) -> str:
        consumption_types = ", ".join(str(t) for t in self._CONSUMPTION_TRANS_TYPES)
        return f"""
            LEFT JOIN (
                SELECT
                    n."ItemCode",
                    n."Warehouse",
                    SUM(IFNULL(n."InQty", 0) - IFNULL(n."OutQty", 0)) AS "FutureNetQty"
                FROM "{schema}"."OINM" n
                WHERE n."DocDate" > ?
                GROUP BY n."ItemCode", n."Warehouse"
            ) future_mov
                ON future_mov."ItemCode" = w."ItemCode"
               AND future_mov."Warehouse" = w."WhsCode"
            LEFT JOIN (
                SELECT
                    n."ItemCode",
                    MAX(n."DocDate") AS "LastConsumptionDate"
                FROM "{schema}"."OINM" n
                WHERE n."OutQty" > 0
                  AND n."TransType" IN ({consumption_types})
                  AND n."DocDate" <= ?
                GROUP BY n."ItemCode"
            ) mov
                ON mov."ItemCode" = w."ItemCode"
        """

    def _build_base_where(self, filters: Dict[str, Any]) -> Tuple[List[str], List]:
        """Base WHERE clauses for warehouse, item group, and search (no status)."""
        clauses = []
        params = []

        warehouse_list = filters.get("warehouse", [])
        if warehouse_list:
            placeholders = ", ".join("?" for _ in warehouse_list)
            clauses.append(f'w."WhsCode" IN ({placeholders})')
            params.extend(warehouse_list)

        item_group = (filters.get("item_group") or "").strip()
        if item_group:
            clauses.append('UPPER(IFNULL(grp."ItmsGrpNam", \'\')) = UPPER(?)')
            params.append(item_group)

        if filters.get("search"):
            search_term = f"%{filters['search']}%"
            clauses.append(
                '(w."ItemCode" LIKE ? OR m."ItemName" LIKE ? OR w."WhsCode" LIKE ?)'
            )
            params.extend([search_term, search_term, search_term])

        return clauses, params

    def _build_where(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        """Full WHERE clause including stock and movement filters."""
        clauses, params = self._build_base_where(filters)

        status_list = filters.get("status", [])
        if status_list:
            conditions = [f'({self._STATUS_SQL[s]})' for s in status_list if s in self._STATUS_SQL]
            if self._includes_default_operational_statuses(status_list):
                conditions.append(f'({self._UNGROUPED_SLOW_OPERATIONAL_SQL})')
            if conditions:
                clauses.append(f'({" OR ".join(conditions)})')

        movement_clause = self._movement_where_clause(filters, grouped=False)
        if movement_clause:
            clauses.append(movement_clause)

        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        return where, params

    def _post_group_where_clause(self, filters: Dict[str, Any]) -> str:
        """Builds filters that apply after grouped aggregation."""
        clauses = []
        status_list = filters.get("status", [])
        if status_list:
            conditions = [
                f"({self._GROUPED_STATUS_SQL[s]})"
                for s in status_list
                if s in self._GROUPED_STATUS_SQL
            ]
            if self._includes_default_operational_statuses(status_list):
                conditions.append(f"({self._GROUPED_SLOW_OPERATIONAL_SQL})")
            if conditions:
                clauses.append(f'({" OR ".join(conditions)})')

        movement_clause = self._movement_where_clause(filters, grouped=True)
        if movement_clause:
            clauses.append(movement_clause)

        return f'WHERE {" AND ".join(clauses)}' if clauses else ""

    def _post_as_of_where_clause(self, filters: Dict[str, Any]) -> str:
        """Builds filters that apply after as-of stock reconstruction."""
        clauses = []
        status_list = filters.get("status", [])
        if status_list:
            conditions = [
                f"({self._AS_OF_STATUS_SQL[s]})"
                for s in status_list
                if s in self._AS_OF_STATUS_SQL
            ]
            if self._includes_default_operational_statuses(status_list):
                conditions.append(f"({self._AS_OF_SLOW_OPERATIONAL_SQL})")
            if conditions:
                clauses.append(f'({" OR ".join(conditions)})')

        movement_clause = self._movement_where_clause(filters, grouped=True)
        if movement_clause:
            clauses.append(movement_clause)

        return f'WHERE {" AND ".join(clauses)}' if clauses else ""

    def _movement_where_clause(self, filters: Dict[str, Any], grouped: bool) -> str:
        """Builds a movement-status filter matching the service display labels."""
        movement_statuses = filters.get("movement_status", [])
        if not movement_statuses:
            return ""

        if grouped:
            days = "days_since_last_consumption"
        else:
            days = 'DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE)'

        sql_map = {
            "recent": (
                f'{days} IS NOT NULL '
                f'AND {days} <= {SLOW_MOVING_DAYS}'
            ),
            "slow": (
                f'( {days} IS NULL OR {days} > {SLOW_MOVING_DAYS} )'
            ),
        }
        conditions = [f"({sql_map[s]})" for s in movement_statuses if s in sql_map]
        return f'({" OR ".join(conditions)})' if conditions else ""

    @classmethod
    def _includes_default_operational_statuses(cls, status_list: List[str]) -> bool:
        return set(status_list) == cls._DEFAULT_OPERATIONAL_STATUSES

    def _build_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        schema = self.connection.schema
        where, params = self._build_where(filters)
        order_by = self._build_order_by(filters)

        query = f"""
            SELECT
                w."ItemCode",
                m."ItemName",
                w."WhsCode",
                w."OnHand",
                w."MinStock",
                IFNULL(m."InvntryUom", '')  AS uom,
                mov."LastConsumptionDate",
                CASE
                    WHEN mov."LastConsumptionDate" IS NULL THEN NULL
                    ELSE DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE)
                END AS "DaysSinceLastConsumption"
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m
                ON w."ItemCode" = m."ItemCode"
            LEFT JOIN "{schema}"."OITB" grp
                ON m."ItmsGrpCod" = grp."ItmsGrpCod"
            {self._movement_joins(schema)}
            {where}
            {order_by}
        """
        return query, params

    def _build_stats_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        """
        Counts items using the same thresholds as the service layer:
          healthy:  on_hand >= benchmark
          low:      on_hand < required quantity and on_hand >= required * 0.6
          critical: on_hand < required * 0.6
        """
        schema = self.connection.schema
        where, params = self._build_where(filters)

        query = f"""
            SELECT
                COUNT(*) AS total_items,
                SUM(CASE
                    WHEN {self._UNGROUPED_REQUIRED_SQL} > 0
                         AND w."OnHand" >= {self._UNGROUPED_REQUIRED_SQL}
                         AND {self._UNGROUPED_NOT_SLOW_SQL}
                    THEN 1 ELSE 0
                END) AS healthy_count,
                SUM(CASE
                    WHEN {self._UNGROUPED_REQUIRED_SQL} > 0
                         AND w."OnHand" < {self._UNGROUPED_REQUIRED_SQL}
                         AND w."OnHand" >= {self._UNGROUPED_REQUIRED_SQL} * 0.6
                         AND {self._UNGROUPED_NOT_SLOW_SQL}
                    THEN 1 ELSE 0
                END) AS low_count,
                SUM(CASE
                    WHEN {self._UNGROUPED_REQUIRED_SQL} > 0
                         AND w."OnHand" < {self._UNGROUPED_REQUIRED_SQL} * 0.6
                         AND {self._UNGROUPED_NOT_SLOW_SQL}
                    THEN 1 ELSE 0
                END) AS critical_count
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m
                ON w."ItemCode" = m."ItemCode"
            LEFT JOIN "{schema}"."OITB" grp
                ON m."ItmsGrpCod" = grp."ItmsGrpCod"
            {self._movement_joins(schema)}
            {where}
        """
        return query, params

    def _build_as_of_base_query(self, filters: Dict[str, Any], as_of_date) -> Tuple[str, List]:
        """
        Builds the unfiltered reconstructed row query used by as-of data and stats.

        On-hand is reconstructed as:
          current OITW.OnHand - net OINM movements posted after as_of_date.
        """
        schema = self.connection.schema
        base_clauses, base_params = self._build_base_where(filters)
        base_where = f'WHERE {" AND ".join(base_clauses)}' if base_clauses else ""

        query = f"""
            SELECT
                w."ItemCode" AS item_code,
                m."ItemName" AS item_name,
                w."WhsCode" AS warehouse,
                (
                    IFNULL(w."OnHand", 0)
                    - IFNULL(future_mov."FutureNetQty", 0)
                ) AS on_hand,
                IFNULL(w."MinStock", 0) AS min_stock,
                IFNULL(m."InvntryUom", '') AS uom,
                mov."LastConsumptionDate" AS last_consumption_date,
                CASE
                    WHEN mov."LastConsumptionDate" IS NULL THEN NULL
                    ELSE DAYS_BETWEEN(mov."LastConsumptionDate", ?)
                END AS days_since_last_consumption
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m
                ON w."ItemCode" = m."ItemCode"
            LEFT JOIN "{schema}"."OITB" grp
                ON m."ItmsGrpCod" = grp."ItmsGrpCod"
            {self._as_of_movement_joins(schema)}
            {base_where}
        """
        return query, [as_of_date, as_of_date, as_of_date] + base_params

    def _build_as_of_query(self, filters: Dict[str, Any], as_of_date) -> Tuple[str, List]:
        base_query, params = self._build_as_of_base_query(filters, as_of_date)
        post_where = self._post_as_of_where_clause(filters)
        order_by = self._build_as_of_order_by(filters)

        query = f"""
            SELECT
                item_code,
                item_name,
                warehouse,
                on_hand,
                min_stock,
                uom,
                last_consumption_date,
                days_since_last_consumption
            FROM (
                {base_query}
            ) s
            {post_where}
            {order_by}
        """
        return query, params

    def _build_as_of_stats_query(self, filters: Dict[str, Any], as_of_date) -> Tuple[str, List]:
        base_query, params = self._build_as_of_base_query(filters, as_of_date)
        post_where = self._post_as_of_where_clause(filters)

        query = f"""
            SELECT
                COUNT(*) AS total_items,
                SUM(CASE
                    WHEN {self._AS_OF_REQUIRED_SQL} > 0
                         AND on_hand >= {self._AS_OF_REQUIRED_SQL}
                         AND {self._AS_OF_NOT_SLOW_SQL}
                    THEN 1 ELSE 0
                END) AS healthy_count,
                SUM(CASE
                    WHEN {self._AS_OF_REQUIRED_SQL} > 0
                         AND on_hand < {self._AS_OF_REQUIRED_SQL}
                         AND on_hand >= {self._AS_OF_REQUIRED_SQL} * 0.6
                         AND {self._AS_OF_NOT_SLOW_SQL}
                    THEN 1 ELSE 0
                END) AS low_count,
                SUM(CASE
                    WHEN {self._AS_OF_REQUIRED_SQL} > 0
                         AND on_hand < {self._AS_OF_REQUIRED_SQL} * 0.6
                         AND {self._AS_OF_NOT_SLOW_SQL}
                    THEN 1 ELSE 0
                END) AS critical_count
            FROM (
                {base_query}
            ) s
            {post_where}
        """
        return query, params

    # ------------------------------------------------------------------
    # Grouped Queries (multi-warehouse)
    # ------------------------------------------------------------------

    def get_grouped_stock_levels(
        self, filters: Dict[str, Any], page: int = 1, page_size: int = 50
    ) -> List[Dict]:
        """Returns one page of item rows grouped across warehouses."""
        query, params = self._build_grouped_query(filters)
        offset = (page - 1) * page_size
        paginated_query = f"{query} LIMIT ? OFFSET ?"
        rows = self._execute(paginated_query, params + [page_size, offset])
        return [self._map_grouped_row(r) for r in rows]

    def get_grouped_stock_stats(self, filters: Dict[str, Any]) -> Dict:
        """Stats for grouped items (multi-warehouse)."""
        query, params = self._build_grouped_stats_query(filters)
        rows = self._execute(query, params)
        row = rows[0] if rows else (0, 0, 0, 0, None, 0)
        return {
            "total_items": int(row[0] or 0),
            "healthy_count": int(row[1] or 0),
            "low_count": int(row[2] or 0),
            "critical_count": int(row[3] or 0),
            # None, not 0, where nothing under benchmark carries a case weight
            # — "no weight recorded" and "weighs nothing" are different answers.
            "below_benchmark_tonnes": float(row[4]) if row[4] is not None else None,
            "unweighed_below_benchmark": int(row[5] or 0),
        }

    def get_item_warehouses(
        self, item_code: str, warehouses: List[str]
    ) -> List[Dict]:
        """Returns per-warehouse rows for a single item (expand detail)."""
        schema = self.connection.schema
        placeholders = ", ".join("?" for _ in warehouses)
        query = f"""
            SELECT
                w."ItemCode",
                m."ItemName",
                w."WhsCode",
                w."OnHand",
                w."MinStock",
                IFNULL(m."InvntryUom", '') AS uom,
                mov."LastConsumptionDate",
                CASE
                    WHEN mov."LastConsumptionDate" IS NULL THEN NULL
                    ELSE DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE)
                END AS "DaysSinceLastConsumption"
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m
                ON w."ItemCode" = m."ItemCode"
            {self._movement_joins(schema)}
            WHERE w."ItemCode" = ? AND w."WhsCode" IN ({placeholders})
            ORDER BY w."WhsCode" ASC
        """
        rows = self._execute(query, [item_code] + warehouses)
        return [self._map_row(r) for r in rows]

    def _build_grouped_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        schema = self.connection.schema
        base_clauses, params = self._build_base_where(filters)
        base_where = f'WHERE {" AND ".join(base_clauses)}' if base_clauses else ""
        post_group_where = self._post_group_where_clause(filters)
        order_by = self._build_order_by(filters, grouped=True)

        query = f"""
            SELECT * FROM (
                SELECT
                    w."ItemCode"    AS item_code,
                    m."ItemName"    AS item_name,
                    SUM(w."OnHand")    AS on_hand,
                    SUM(w."MinStock")  AS min_stock,
                    IFNULL(m."InvntryUom", '') AS uom,
                    COUNT(*)           AS warehouse_count,
                    SUM(CASE WHEN {self._UNGROUPED_REQUIRED_SQL} > 0
                              AND w."OnHand" < {self._UNGROUPED_REQUIRED_SQL} * 0.6
                              AND {self._UNGROUPED_NOT_SLOW_SQL}
                         THEN 1 ELSE 0 END) AS critical_wh,
                    SUM(CASE WHEN {self._UNGROUPED_REQUIRED_SQL} > 0
                              AND w."OnHand" < {self._UNGROUPED_REQUIRED_SQL}
                              AND w."OnHand" >= {self._UNGROUPED_REQUIRED_SQL} * 0.6
                              AND {self._UNGROUPED_NOT_SLOW_SQL}
                         THEN 1 ELSE 0 END) AS low_wh,
                    MAX(mov."LastConsumptionDate") AS last_consumption_date,
                    MIN(CASE
                        WHEN mov."LastConsumptionDate" IS NULL THEN NULL
                        ELSE DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE)
                    END) AS days_since_last_consumption
                FROM "{schema}"."OITW" w
                JOIN "{schema}"."OITM" m ON w."ItemCode" = m."ItemCode"
                LEFT JOIN "{schema}"."OITB" grp
                    ON m."ItmsGrpCod" = grp."ItmsGrpCod"
                {self._movement_joins(schema)}
                {base_where}
                GROUP BY w."ItemCode", m."ItemName", m."InvntryUom"
            ) g
            {post_group_where}
            {order_by}
        """
        return query, params

    def _build_grouped_stats_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        schema = self.connection.schema
        base_clauses, params = self._build_base_where(filters)
        base_where = f'WHERE {" AND ".join(base_clauses)}' if base_clauses else ""
        post_group_where = self._post_group_where_clause(filters)
        # OITM is `m` in this query, not the `i` the occupancy reader uses.
        gross_weight = self._gross_weight_expr(self._table_columns("OITM"), alias="m")
        # Low and critical in one condition: both mean "under its benchmark",
        # and the tile that reads this asks for the pair rather than the split.
        under_benchmark = (
            f"{self._GROUPED_REQUIRED_SQL} > 0"
            f" AND on_hand < {self._GROUPED_REQUIRED_SQL}"
            f" AND {self._GROUPED_NOT_SLOW_SQL}"
        )

        query = f"""
            SELECT
                COUNT(*) AS total_items,
                SUM(CASE WHEN {self._GROUPED_REQUIRED_SQL} > 0
                              AND on_hand >= {self._GROUPED_REQUIRED_SQL}
                              AND {self._GROUPED_NOT_SLOW_SQL}
                    THEN 1 ELSE 0 END) AS healthy_count,
                SUM(CASE WHEN {self._GROUPED_REQUIRED_SQL} > 0
                              AND on_hand < {self._GROUPED_REQUIRED_SQL}
                              AND on_hand >= {self._GROUPED_REQUIRED_SQL} * 0.6
                              AND {self._GROUPED_NOT_SLOW_SQL}
                    THEN 1 ELSE 0 END) AS low_count,
                SUM(CASE WHEN {self._GROUPED_REQUIRED_SQL} > 0
                              AND on_hand < {self._GROUPED_REQUIRED_SQL} * 0.6
                              AND {self._GROUPED_NOT_SLOW_SQL}
                    THEN 1 ELSE 0 END) AS critical_count,
                -- Tonnage of everything under its benchmark, low and critical
                -- together. Gross weight of one sales case x cases held: the
                -- only weight SAP records, and it includes the packaging.
                SUM(CASE WHEN {under_benchmark}
                    THEN on_hand / pieces_per_case * gross_weight_per_case / 1000
                    ELSE 0 END) AS below_benchmark_tonnes,
                -- Rows the tonnage above could not count, because SAP holds no
                -- case weight for them. Disclosed rather than treated as
                -- weightless: a confident total over a half-weighed set looks
                -- identical to a correct one.
                SUM(CASE WHEN {under_benchmark} AND gross_weight_per_case IS NULL
                    THEN 1 ELSE 0 END) AS unweighed_below_benchmark
            FROM (
                SELECT
                    SUM(w."OnHand")   AS on_hand,
                    SUM(w."MinStock") AS min_stock,
                    MAX({gross_weight}) AS gross_weight_per_case,
                    -- One piece IS one case where SAP bills the piece, so a
                    -- missing factor falls back to 1 rather than dropping the
                    -- row out of the weight entirely.
                    MAX(NULLIF(IFNULL(m."SalFactor2", 1), 0)) AS pieces_per_case,
                    MIN(CASE
                        WHEN mov."LastConsumptionDate" IS NULL THEN NULL
                        ELSE DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE)
                    END) AS days_since_last_consumption
                FROM "{schema}"."OITW" w
                JOIN "{schema}"."OITM" m ON w."ItemCode" = m."ItemCode"
                LEFT JOIN "{schema}"."OITB" grp
                    ON m."ItmsGrpCod" = grp."ItmsGrpCod"
                {self._movement_joins(schema)}
                {base_where}
                GROUP BY w."ItemCode"
            ) g
            {post_group_where}
        """
        return query, params

    def _map_grouped_row(self, row) -> Dict:
        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "on_hand": float(row[2] or 0),
            "min_stock": float(row[3] or 0),
            "uom": row[4] or "",
            "warehouse_count": int(row[5] or 0),
            "critical_warehouses": int(row[6] or 0),
            "low_warehouses": int(row[7] or 0),
            "last_consumption_date": self._format_date(row[8]),
            "days_since_last_consumption": int(row[9]) if row[9] is not None else None,
        }

    # ------------------------------------------------------------------
    # Row Mapper
    # ------------------------------------------------------------------

    def _map_row(self, row) -> Dict:
        on_hand = float(row[3] or 0)
        min_stock = float(row[4] or 0)

        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "warehouse": row[2] or "",
            "on_hand": on_hand,
            "min_stock": min_stock,
            "uom": row[5] or "",
            "last_consumption_date": self._format_date(row[6]),
            "days_since_last_consumption": int(row[7]) if row[7] is not None else None,
        }

    @staticmethod
    def _format_date(value) -> Optional[str]:
        if not value:
            return None
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        return str(value)[:10]

    # ------------------------------------------------------------------
    # Execution Helper
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: List) -> List:
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
            cursor.execute(query, params)
            return cursor.fetchall()

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error in stock dashboard: {e}")
            raise SAPDataError(
                "Failed to retrieve stock dashboard data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error in stock dashboard: {e}")
            raise SAPDataError(
                "Failed to retrieve stock dashboard data from SAP. Please try again."
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

    def get_item_weights(self, item_codes) -> Dict[str, float]:
        """Kilograms in one piece, per item code.

        The same chain the occupancy read uses -- gross case weight over the
        pack factor -- exposed for callers that hold quantities from somewhere
        other than SAP stock. The branch-transfer register is the case in point:
        it records pieces and no weight at all, so its tonnage can only come
        from the item master.

        Items with no case weight, no pack factor, or no rows at all are simply
        absent from the result rather than mapped to zero, so a caller can count
        what it could not weigh.
        """
        codes = [code for code in {(c or "").strip() for c in item_codes} if code]
        if not codes:
            return {}

        schema = self.connection.schema
        gross_weight_expr = self._gross_weight_expr(self._table_columns("OITM"))
        placeholders = ", ".join("?" for _ in codes)
        query = f"""
            SELECT
                i."ItemCode",
                {gross_weight_expr} AS "GrossWeightPerCase",
                COALESCE(i."SalFactor2", 0) AS "PiecesPerBox"
            FROM "{schema}"."OITM" i
            WHERE i."ItemCode" IN ({placeholders})
        """
        weights = {}
        for row in self._execute(query, codes):
            per_case, pieces_per_case = row[1], row[2]
            if per_case is None or not per_case or not pieces_per_case:
                continue
            weights[row[0]] = float(per_case) / float(pieces_per_case)
        return weights


    def get_unreceived_intercompany_dispatches(
        self,
        *,
        receiving_schema: str,
        customer_codes,
        warehouses,
        lookback_days: int = 60,
    ) -> List[Dict]:
        """Stock invoiced out to a sister company that SAP has not booked in yet.

        This is the board's definition of "in transit", and it is SAP's own
        evidence rather than a status somebody sets by hand: the sending company
        raises an A/R invoice, the receiving company answers it with a Goods
        Receipt PO carrying the invoice number in ``NumAtCard``. Until that
        receipt exists, the load is still on the road.

        Two filters carry real weight and neither is cosmetic:

        * ``warehouses`` keeps the read to floors that actually ship. Without
          it the query picks up rate-difference debit notes raised against
          other plants' warehouses -- three of them in August 2026 came to 533
          tonnes between them, four times the genuine figure, and being purely
          financial they can never be received.
        * ``lookback_days`` bounds the tail. A receipt that was never keyed in
          leaves its invoice unmatched forever, so an unbounded read would
          accumulate every clerical miss since go-live and call it traffic.

        Weight follows the same chain as the rest of the board -- the line's own
        ``Weight1`` where SAP recorded one, otherwise gross case weight over the
        pack factor. Lines that chain cannot weigh are counted per document, so
        the caller can report a tonnage as the floor it is.

        Returns one row per unreceived invoice, newest first.
        """
        codes = [code for code in {(c or "").strip() for c in customer_codes} if code]
        floors = [w for w in {(w or "").strip().upper() for w in warehouses} if w]
        if not codes or not floors:
            return []

        schema = self.connection.schema
        gross = self._gross_weight_expr(self._table_columns("OITM"))
        code_slots = ", ".join("?" for _ in codes)
        floor_slots = ", ".join("?" for _ in floors)

        # `weighed` is the per-line kilogram chain; naming it once keeps the
        # SUM and the unweighed count from drifting apart.
        weighed = f"""
            CASE
                WHEN COALESCE(l."Weight1", 0) > 0 THEN l."Weight1"
                WHEN {gross} IS NOT NULL AND COALESCE(i."SalFactor2", 0) > 0
                    THEN l."Quantity" * {gross} / i."SalFactor2"
                ELSE NULL
            END
        """

        query = f"""
            WITH sent AS (
                SELECT
                    h."DocNum" AS "DocNum",
                    h."DocDate" AS "DocDate",
                    SUM(COALESCE({weighed}, 0)) AS "Kilograms",
                    SUM(CASE WHEN {weighed} IS NULL THEN 1 ELSE 0 END) AS "Unweighed"
                FROM "{schema}"."OINV" h
                JOIN "{schema}"."INV1" l ON l."DocEntry" = h."DocEntry"
                JOIN "{schema}"."OITM" i ON i."ItemCode" = l."ItemCode"
                WHERE h."CANCELED" = 'N'
                  AND h."CardCode" IN ({code_slots})
                  AND UPPER(l."WhsCode") IN ({floor_slots})
                  AND h."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
                GROUP BY h."DocNum", h."DocDate"
            ),
            received AS (
                SELECT DISTINCT TO_NVARCHAR(TRIM(r."NumAtCard")) AS "Ref"
                FROM "{receiving_schema}"."OPDN" r
                WHERE r."CANCELED" = 'N' AND r."NumAtCard" IS NOT NULL
            )
            SELECT
                s."DocNum",
                s."DocDate",
                DAYS_BETWEEN(s."DocDate", CURRENT_DATE) AS "DaysOut",
                s."Kilograms",
                s."Unweighed"
            FROM sent s
            LEFT JOIN received r ON r."Ref" = TO_NVARCHAR(s."DocNum")
            WHERE r."Ref" IS NULL
            ORDER BY s."DocDate" DESC, s."DocNum" DESC
        """

        params = codes + floors + [-abs(int(lookback_days))]
        return [
            {
                "doc_num": int(row[0]),
                "doc_date": self._format_date(row[1]),
                "days_out": int(row[2] or 0),
                "kilograms": float(row[3] or 0),
                "unweighed_lines": int(row[4] or 0),
            }
            for row in self._execute(query, params)
        ]
