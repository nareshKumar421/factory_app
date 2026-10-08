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
        self.company_code = company_code
        self.client = SAPClient(company_code=company_code)

    def list_items(self, search: str = '', limit: int = 100) -> list[dict]:
        """The picker's items, from SAP -- or, when HANA cannot be reached, from
        the nightly copy (``sap_mirror``), each row then carrying the copy's
        ``sap_copy_as_of`` so the page can say how old the list is."""
        try:
            limit = int(limit or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 200))

        try:
            return self._list_items_live(search, limit)
        except OitmItemReadError as exc:
            from sap_mirror import services as sap_mirror

            if not sap_mirror.hana_unreachable(exc):
                raise
            copy = sap_mirror.copied_rows(
                self.company_code, sap_mirror.FG_ITEMS, search=search, limit=limit
            )
            if copy is None:
                raise
            rows, as_of = copy
            logger.warning(
                'HANA unreachable; serving %s items for %s from the copy of %s',
                len(rows), self.company_code, as_of,
            )
            return [{**row, 'sap_copy_as_of': as_of.isoformat()} for row in rows]

    def list_all_for_copy(self) -> list[dict]:
        """Every item the picker can offer, for the nightly copy."""
        return self._list_items_live('', None)

    def _finished_goods_where(self) -> str:
        # Restrict to real finished goods (FG*) only. FB* bundle/combo SKUs share
        # the same finished-goods item group, so the group filter alone lets them
        # into the picker where operators pick them by accident during box
        # generation — exclude them by code prefix.
        return """
            WHERE "InvntItem" = 'Y'
              AND "ItmsGrpCod" = {finished_goods_item_group_code}
              AND "ItemCode" LIKE 'FG%'
              AND "validFor" = 'Y'
              AND "frozenFor" = 'N'
        """.format(
            finished_goods_item_group_code=self.FINISHED_GOODS_ITEM_GROUP_CODE,
        )

    def _list_items_live(self, search: str, limit: int | None) -> list[dict]:
        schema = self.client.context.config['hana']['schema']
        where_clause = self._finished_goods_where()

        if search:
            safe_search = search.replace("'", "''")
            where_clause += f"""
              AND (
                LOWER("ItemCode") LIKE LOWER('%{safe_search}%')
                OR LOWER("ItemName") LIKE LOWER('%{safe_search}%')
              )
            """

        sql = """
            SELECT {top}
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
            top=f'TOP {int(limit)}' if limit else '',
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
            raise OitmItemReadError(str(exc)) from exc

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
        """Forward map (JIVO OIL → JIVO MART): given an Oil ItemCode, find the Jivo
        Mart item(s) that carry it in their ``U_Oil_ItemCode`` column. Queried on the
        JIVO MART OITM (this service's company)."""
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
        except OitmItemReadError as exc:
            # HANA down: the nightly copy of the mapping. A code it does not map
            # may have been mapped since last night, so that is SAP being down,
            # not "no mapping".
            mapped = [
                row['item_code'] for row in self._mapping_copy(exc)
                if row.get('oil_item_code') == oil_item_code
            ]
            if not mapped:
                raise
            return mapped
        except Exception as exc:
            logger.error('Failed to fetch Jivo Mart item mapping for %s: %s', oil_item_code, exc)
            raise OitmItemReadError(str(exc))

    def find_oil_item_code_by_mart_item_code(self, mart_item_code: str) -> str | None:
        """Reverse map (JIVO MART → JIVO OIL): read the Oil ItemCode stored on a Jivo
        Mart item's own ``U_Oil_ItemCode`` column. Queried on the JIVO MART OITM (this
        service's company). Returns ``None`` when the item is unknown or the column is
        blank (i.e. no mapping is maintained). This is inherently unambiguous — the
        value is a single column on one row, unlike the forward search."""
        mart_item_code = str(mart_item_code or '').strip()
        if not mart_item_code:
            return None

        schema = self.client.context.config['hana']['schema']
        sql = """
            SELECT
                "U_Oil_ItemCode"
            FROM "{schema}"."{table_name}"
            WHERE "ItemCode" = ?
        """.format(
            schema=schema,
            table_name=self.TABLE_NAME,
        )

        try:
            rows = self._execute(sql, (mart_item_code,))
        except OitmItemReadError as exc:
            # HANA down: the nightly copy, as for the forward lookup.
            for row in self._mapping_copy(exc):
                if row.get('item_code') == mart_item_code and row.get('oil_item_code'):
                    return row['oil_item_code']
            raise
        except Exception as exc:
            logger.error('Failed to fetch Oil item mapping for Jivo Mart %s: %s', mart_item_code, exc)
            raise OitmItemReadError(str(exc))

        for row in rows:
            oil_code = str(row.get('U_Oil_ItemCode') or '').strip()
            if oil_code:
                return oil_code
        return None

    def list_oil_item_mappings_for_copy(self) -> list[dict]:
        """Every Jivo Mart item that carries an Oil ItemCode, for the nightly
        copy of the Oil <-> Mart mapping (``sap_mirror``, list ``oil_item_mapping``)."""
        schema = self.client.context.config['hana']['schema']
        sql = """
            SELECT "ItemCode", "U_Oil_ItemCode"
            FROM "{schema}"."{table_name}"
            WHERE IFNULL("U_Oil_ItemCode", '') <> ''
            ORDER BY "ItemCode"
        """.format(schema=schema, table_name=self.TABLE_NAME)
        return [
            {
                'item_code': row.get('ItemCode') or '',
                'oil_item_code': str(row.get('U_Oil_ItemCode') or '').strip(),
            }
            for row in self._execute(sql)
            if row.get('ItemCode')
        ]

    def _mapping_copy(self, exc) -> list[dict]:
        """The copied Oil <-> Mart mapping for a failed read, or re-raise ``exc``."""
        from sap_mirror import services as sap_mirror

        if not sap_mirror.hana_unreachable(exc):
            raise exc
        copy = sap_mirror.copied_rows(self.company_code, sap_mirror.OIL_ITEM_MAPPING)
        if copy is None:
            raise exc
        logger.warning(
            'HANA unreachable; Oil <-> Mart item mapping for %s answered from the copy of %s',
            self.company_code, copy[1],
        )
        return copy[0]

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
            from sap_client.hana.connection import HanaConnection

            # Through HanaConnection for its fail-fast: while HANA is known to be
            # down, a picker search goes to the copy at once, not after the
            # connect timeout.
            connection = HanaConnection(self.client.context.hana).connect()
            cursor = connection.cursor()
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
            cols = [col[0] for col in cursor.description]
            rows = cursor.fetchall()
            return [dict(zip(cols, row)) for row in rows]
        except Exception as exc:
            raise OitmItemReadError(str(exc)) from exc
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
