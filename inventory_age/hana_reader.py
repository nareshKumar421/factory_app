"""
inventory_age/hana_reader.py

Calls SAP HANA stored procedure SP_INVENTORYAGEVALUE to retrieve
inventory age and valuation data for the current company schema.
"""

import logging
from typing import Any, Dict, List

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaInventoryAgeReader:
    """
    Reads inventory age & value data by calling SP_INVENTORYAGEVALUE.

    The stored procedure accepts no parameters and returns one row per
    item-warehouse combination with columns:
        ItemCode, ItemName, U_IsLitre, ItemGroup, U_Unit, U_Variety,
        U_SKU, U_Sub_Group, WhsCode, OnHand, Litres, InStockValue,
        CalcPrice, EffectiveDate, DaysAge
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Public: lightweight query for dropdown options only
    # ------------------------------------------------------------------

    def get_filter_options(self) -> Dict[str, List]:
        """
        Return distinct dropdown values via lightweight SQL queries
        (no SP call).  Uses OITB for item groups, OITM/OITW for the rest.
        """
        schema = self.connection.schema

        # Item groups from OITB (same pattern as non_moving_rm)
        item_groups_rows = self._execute_query(
            f'SELECT "ItmsGrpCod", "ItmsGrpNam" FROM "{schema}"."OITB" ORDER BY "ItmsGrpNam"',
            [],
        )

        # Distinct warehouses, sub-groups, varieties from OITM + OITW
        distinct_rows = self._execute_query(
            f"""
            SELECT DISTINCT
                IFNULL(w."WhsCode", '')     AS "Warehouse",
                IFNULL(m."U_Sub_Group", '') AS "SubGroup",
                IFNULL(m."U_Variety", '')   AS "Variety"
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m ON w."ItemCode" = m."ItemCode"
            WHERE w."OnHand" != 0
            """,
            [],
        )

        warehouses: set[str] = set()
        sub_groups: set[str] = set()
        varieties: set[str] = set()

        for r in distinct_rows:
            if r[0]:
                warehouses.add(r[0])
            if r[1]:
                sub_groups.add(r[1])
            if r[2]:
                varieties.add(r[2])

        return {
            "item_groups": [
                {"item_group_code": r[0], "item_group_name": r[1] or ""}
                for r in item_groups_rows
            ],
            "sub_groups": sorted(sub_groups),
            "warehouses": sorted(warehouses),
            "varieties": sorted(varieties),
        }

    # ------------------------------------------------------------------
    # Public: full SP call (returns all rows, filtered in service)
    # ------------------------------------------------------------------

    def get_inventory_age(self) -> List[Dict]:
        """Call SP_INVENTORYAGEVALUE and return mapped rows."""
        schema = self.connection.schema
        rows = self._execute_query(
            f'CALL "{schema}"."SP_INVENTORYAGEVALUE"()', []
        )
        return [self._map_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Row Mapper
    # ------------------------------------------------------------------

    def _map_row(self, row) -> Dict:
        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "is_litre": (row[2] or "N") == "Y",
            "item_group": row[3] or "",
            "unit": row[4] or "",
            "variety": row[5] or "",
            "sku": row[6] or "",
            "sub_group": row[7] or "",
            "warehouse": row[8] or "",
            "on_hand": float(row[9] or 0),
            "litres": float(row[10] or 0),
            "in_stock_value": float(row[11] or 0),
            "calc_price": float(row[12] or 0),
            "effective_date": str(row[13]) if row[13] else None,
            "days_age": int(row[14] or 0),
        }

    # ------------------------------------------------------------------
    # Execution Helper
    # ------------------------------------------------------------------

    def _execute_query(self, query: str, params: List[Any]) -> List:
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
            logger.error(f"SAP HANA query error in inventory age: {e}")
            raise SAPDataError(
                f"Inventory age SP error: {e}"
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error in inventory age: {e}")
            raise SAPDataError(
                f"Inventory age HANA error: {e}"
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
