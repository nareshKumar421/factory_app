import logging
from typing import Optional

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaBOMReader:
    """
    Reads Bill of Materials (BOM) from SAP HANA.

    Tables used:
      OITT  — BOM header  (keyed by finished-good ItemCode)
      ITT1  — BOM lines   (components per BOM)
      OITM  — Item master (component name + total on-hand stock)
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def get_bom(self, item_code: str, planned_qty: float = 1.0) -> dict:
        """
        Return BOM components for *item_code* scaled to *planned_qty*.

        Each component includes:
          - required_qty  = bom_qty_per_unit × planned_qty
          - available_stock = OITM.OnHand  (total across all warehouses)
          - shortage_qty  = max(0, required_qty − available_stock)

        Returns an empty components list if no production BOM exists.
        """
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            # ------------------------------------------------------------------
            # Fetch finished-good item name
            # ------------------------------------------------------------------
            cursor.execute(
                f'SELECT "ItemName" FROM "{schema}"."OITM" WHERE "ItemCode" = ?',
                [item_code],
            )
            row = cursor.fetchone()
            item_name = row[0] if row else item_code

            # ------------------------------------------------------------------
            # Fetch BOM components from OITT → ITT1 → OITM
            # ITT1 actual columns: Father (component code), Quantity, Uom, ItemName
            # ------------------------------------------------------------------
            query = f"""
                SELECT
                    T1."Father"                             AS component_code,
                    T2."ItemName"                           AS component_name,
                    T1."Quantity"                           AS qty_per_unit,
                    IFNULL(T1."Uom", IFNULL(T2."InvntryUom", '')) AS uom,
                    IFNULL(T2."OnHand", 0)                  AS on_hand
                FROM "{schema}"."OITT" T0
                INNER JOIN "{schema}"."ITT1" T1
                    ON T0."Code" = T1."Code"
                INNER JOIN "{schema}"."OITM" T2
                    ON T1."Father" = T2."ItemCode"
                WHERE T0."Code" = ?
                ORDER BY T1."VisOrder", T1."Father"
            """
            cursor.execute(query, [item_code])
            rows = cursor.fetchall()

            components = []
            for r in rows:
                comp_code    = r[0]
                comp_name    = r[1]
                qty_per_unit = float(r[2])
                uom          = r[3] or ''
                on_hand      = float(r[4])

                required_qty = round(qty_per_unit * planned_qty, 4)
                shortage_qty = round(max(0.0, required_qty - on_hand), 4)

                components.append({
                    'component_code':  comp_code,
                    'component_name':  comp_name,
                    'uom':             uom,
                    'qty_per_unit':    qty_per_unit,
                    'required_qty':    required_qty,
                    'available_stock': round(on_hand, 4),
                    'shortage_qty':    shortage_qty,
                    'has_shortage':    shortage_qty > 0,
                })

            return {
                'item_code':    item_code,
                'item_name':    item_name,
                'planned_qty':  planned_qty,
                'components':   components,
                'bom_found':    len(components) > 0,
                'has_shortage': any(c['has_shortage'] for c in components),
            }

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA BOM query error for '{item_code}': {e}")
            raise SAPDataError(f"Failed to retrieve BOM from SAP: {e}") from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA error fetching BOM for '{item_code}': {e}")
            raise SAPDataError("Failed to retrieve BOM from SAP. Please try again.") from e
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
