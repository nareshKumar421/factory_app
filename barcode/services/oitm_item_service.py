import logging
from decimal import Decimal, InvalidOperation

from sap_client.client import SAPClient

logger = logging.getLogger(__name__)


class OitmItemReadError(Exception):
    pass


class OitmItemService:
    """Read active inventory items from SAP HANA OITM for barcode label generation."""

    TABLE_NAME = 'OITM'
    FINISHED_GOODS_ITEM_GROUP_CODE = 102

    def __init__(self, company_code: str):
        self.client = SAPClient(company_code=company_code)

    def list_items(self, search: str = '', limit: int = 100) -> list[dict]:
        try:
            limit = int(limit or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 200))

        schema = self.client.context.config['hana']['schema']
        where_clause = """
            WHERE "InvntItem" = 'Y'
              AND "ItmsGrpCod" = {finished_goods_item_group_code}
              AND "validFor" = 'Y'
              AND "frozenFor" = 'N'
        """.format(
            finished_goods_item_group_code=self.FINISHED_GOODS_ITEM_GROUP_CODE,
        )

        if search:
            safe_search = search.replace("'", "''")
            where_clause += f"""
              AND (
                LOWER("ItemCode") LIKE LOWER('%{safe_search}%')
                OR LOWER("ItemName") LIKE LOWER('%{safe_search}%')
              )
            """

        sql = """
            SELECT TOP {limit}
                "ItemCode",
                "ItemName",
                "InvntryUom",
                "SalUnitMsr",
                "BuyUnitMsr",
                "ItmsGrpCod",
                "ManBtchNum",
                "ManSerNum",
                "InvntItem",
                "SellItem",
                "PrchseItem",
                "SalFactor2",
                "validFor",
                "frozenFor"
            FROM "{schema}"."{table_name}"
            {where_clause}
            ORDER BY "ItemCode"
        """.format(
            limit=limit,
            schema=schema,
            table_name=self.TABLE_NAME,
            where_clause=where_clause,
        )

        try:
            return [self._normalize_row(row) for row in self._execute(sql)]
        except OitmItemReadError:
            raise
        except Exception as exc:
            logger.error('Failed to fetch OITM item rows: %s', exc)
            raise OitmItemReadError(str(exc))

    def get_item(self, item_code: str) -> dict | None:
        """Look up one item by exact code, with its item group name (OITM ⋈ OITB).

        Unlike ``list_items`` this is NOT restricted to finished goods, so it can
        report the real item group of any item. Returns ``None`` when no item
        matches. Used to show an item's "type" (its SAP item group) on scan.
        """
        item_code = str(item_code or '').strip()
        if not item_code:
            return None

        schema = self.client.context.config['hana']['schema']
        sql = """
            SELECT
                T0."ItemCode",
                T0."ItemName",
                T0."ItmsGrpCod",
                T1."ItmsGrpNam"
            FROM "{schema}"."{table_name}" T0
            LEFT JOIN "{schema}"."OITB" T1 ON T0."ItmsGrpCod" = T1."ItmsGrpCod"
            WHERE T0."ItemCode" = ?
        """.format(
            schema=schema,
            table_name=self.TABLE_NAME,
        )

        try:
            rows = self._execute(sql, (item_code,))
        except OitmItemReadError:
            raise
        except Exception as exc:
            logger.error('Failed to fetch OITM item %s: %s', item_code, exc)
            raise OitmItemReadError(str(exc))

        if not rows:
            return None
        row = rows[0]
        return {
            'item_code': row.get('ItemCode') or '',
            'item_name': row.get('ItemName') or '',
            'item_group_code': self._to_int(row.get('ItmsGrpCod')),
            'item_group_name': (row.get('ItmsGrpNam') or '').strip(),
        }

    def find_item_codes_by_oil_item_code(self, oil_item_code: str) -> list[str]:
        oil_item_code = str(oil_item_code or '').strip()
        if not oil_item_code:
            return []

        schema = self.client.context.config['hana']['schema']
        sql = """
            SELECT
                "ItemCode"
            FROM "{schema}"."{table_name}"
            WHERE "U_Oil_ItemCode" = ?
        """.format(
            schema=schema,
            table_name=self.TABLE_NAME,
        )

        try:
            rows = self._execute(sql, (oil_item_code,))
            return [row.get('ItemCode') for row in rows if row.get('ItemCode')]
        except OitmItemReadError:
            raise
        except Exception as exc:
            logger.error('Failed to fetch Jivo Mart item mapping for %s: %s', oil_item_code, exc)
            raise OitmItemReadError(str(exc))

    @staticmethod
    def _normalize_row(row: dict) -> dict:
        sal_factor2 = OitmItemService._to_int(row.get('SalFactor2'))
        pieces_per_box, source = OitmItemService._resolve_pieces_per_box(sal_factor2)
        return {
            'item_code': row.get('ItemCode') or '',
            'item_name': row.get('ItemName') or '',
            'inventory_uom': row.get('InvntryUom') or '',
            'sales_uom': row.get('SalUnitMsr') or '',
            'purchase_uom': row.get('BuyUnitMsr') or '',
            'item_group_code': OitmItemService._to_int(row.get('ItmsGrpCod')),
            'manage_batch_numbers': row.get('ManBtchNum') == 'Y',
            'manage_serial_numbers': row.get('ManSerNum') == 'Y',
            'is_inventory_item': row.get('InvntItem') == 'Y',
            'is_sales_item': row.get('SellItem') == 'Y',
            'is_purchase_item': row.get('PrchseItem') == 'Y',
            'sal_factor2': sal_factor2,
            'pieces_per_box': pieces_per_box,
            'pieces_per_box_source': source,
            'valid_for': row.get('validFor') == 'Y',
            'frozen_for': row.get('frozenFor') == 'Y',
        }

    @staticmethod
    def _resolve_pieces_per_box(sal_factor2: int | None) -> tuple[int | None, str]:
        """Authoritative pieces-per-box for locking the generation qty field.

        SAP ``OITM.SalFactor2`` is the single source of truth: it is how every
        downstream flow (dispatch scan, bills, stock transfer) counts a box.
        This holds for CSD SKUs too, where one box is the sellable unit and is
        billed as a single piece (``SalFactor2 = 1``) — so we trust the value
        directly and never parse the item name (which states the physical
        bottle count, not how the box is transacted).

        Returns ``(pieces_per_box, source)``. When SalFactor2 is missing (an
        unconfigured item), returns ``(None, 'unknown')`` so the UI leaves the
        field editable rather than guessing.
        """
        if sal_factor2 and sal_factor2 > 0:
            return sal_factor2, 'sap'
        return None, 'unknown'

    @staticmethod
    def _to_int(value) -> int | None:
        if value in (None, ''):
            return None
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            return None

    def _execute(self, sql: str, params: tuple | list | None = None) -> list[dict]:
        connection = None
        cursor = None
        try:
            conn = self.client.context.hana
            from hdbcli import dbapi

            connection = dbapi.connect(
                address=conn['host'],
                port=conn['port'],
                user=conn['user'],
                password=conn['password'],
            )
            cursor = connection.cursor()
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
            cols = [col[0] for col in cursor.description]
            rows = cursor.fetchall()
            return [dict(zip(cols, row)) for row in rows]
        except Exception as exc:
            raise OitmItemReadError(str(exc))
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
