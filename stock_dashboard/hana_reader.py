"""
stock_dashboard/hana_reader.py

Executes SAP HANA SQL queries for the Stock Dashboard.
Reads from SAP B1 HANA tables: OITW (Item Warehouses), OITM (Item Master).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

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

    def get_warehouse_occupancy(self, warehouse: str) -> List[Dict]:
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

        Rows with no stock are dropped. A negative on-hand is returned as-is
        rather than clamped, so a board can show that SAP is carrying a negative
        instead of silently reading it as an empty shelf.
        """
        schema = self.connection.schema
        query = f"""
            SELECT
                w."ItemCode",
                i."ItemName",
                COALESCE(w."OnHand", 0)       AS "OnHand",
                COALESCE(i."SalFactor2", 0)   AS "PiecesPerBox",
                COALESCE(i."SalPackUn", 0)    AS "LitresPerPiece",
                COALESCE(w."StockValue", 0)   AS "StockValue",
                COALESCE(i."U_Sub_Group", '') AS "SubGroup",
                COALESCE(i."InvntryUom", '')  AS "Uom"
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" i ON i."ItemCode" = w."ItemCode"
            WHERE w."WhsCode" = ?
              AND COALESCE(w."OnHand", 0) <> 0
            ORDER BY COALESCE(w."OnHand", 0) DESC
        """
        rows = self._execute(query, [warehouse])
        return [self._map_occupancy_row(r) for r in rows]

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
        row = rows[0] if rows else (0, 0, 0, 0)
        return {
            "total_items": int(row[0] or 0),
            "healthy_count": int(row[1] or 0),
            "low_count": int(row[2] or 0),
            "critical_count": int(row[3] or 0),
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
                    THEN 1 ELSE 0 END) AS critical_count
            FROM (
                SELECT
                    SUM(w."OnHand")   AS on_hand,
                    SUM(w."MinStock") AS min_stock,
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
