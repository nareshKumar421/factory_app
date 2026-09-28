import logging
from decimal import Decimal, InvalidOperation

from sap_client.client import SAPClient

logger = logging.getLogger(__name__)


class ProductionReleaseReadError(Exception):
    pass


class ProductionReleaseOilService:
    """Read label source rows from the SAP HANA PRODUCTION_RELEASE_OIL view."""

    VIEW_NAME = 'PRODUCTION_RELEASE_OIL'

    def __init__(self, company_code: str):
        self.client = SAPClient(company_code=company_code)

    def list_releases(self, search: str = '', limit: int = 100) -> list[dict]:
        try:
            limit = int(limit or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 200))
        schema = self.client.context.config['hana']['schema']
        where_clause = 'WHERE "Status" = \'R\''

        if search:
            safe_search = search.replace("'", "''")
            where_clause += f"""
              AND (
                LOWER(TO_NVARCHAR("DocNum")) LIKE LOWER('%{safe_search}%')
                OR LOWER("ItemCode") LIKE LOWER('%{safe_search}%')
                OR LOWER("ItemName") LIKE LOWER('%{safe_search}%')
                OR LOWER("U_BATCH_NO") LIKE LOWER('%{safe_search}%')
              )
            """

        sql = """
            SELECT TOP {limit}
                "DocEntry",
                "DocNum",
                "PostDate",
                "ItemCode",
                "ItemName",
                "Liter Countable",
                "ManBtchNum",
                "PlannedQty",
                "Box",
                "Liter",
                "Box Size",
                "Volume Per Pc",
                "Volume Per Box",
                "U_BATCH_NO",
                "MFG Date",
                "Expiry Date",
                "Status"
            FROM "{schema}"."{view_name}"
            {where_clause}
            ORDER BY "PostDate" DESC, "DocNum" DESC
        """.format(
            limit=limit,
            schema=schema,
            view_name=self.VIEW_NAME,
            where_clause=where_clause,
        )

        try:
            return [self._normalize_row(row) for row in self._execute(sql)]
        except ProductionReleaseReadError:
            raise
        except Exception as exc:
            logger.error('Failed to fetch production release oil rows: %s', exc)
            raise ProductionReleaseReadError(str(exc))

    @staticmethod
    def _normalize_row(row: dict) -> dict:
        return {
            'doc_entry': ProductionReleaseOilService._to_int(row.get('DocEntry')),
            'doc_num': ProductionReleaseOilService._to_int(row.get('DocNum')),
            'post_date': ProductionReleaseOilService._date_to_iso(row.get('PostDate')),
            'item_code': row.get('ItemCode') or '',
            'item_name': row.get('ItemName') or '',
            'liter_countable': row.get('Liter Countable') or '',
            'man_btch_num': row.get('ManBtchNum') or '',
            'planned_qty': ProductionReleaseOilService._decimal_to_string(row.get('PlannedQty')),
            'box_count': ProductionReleaseOilService._integer_decimal_to_string(row.get('Box')),
            'liter': ProductionReleaseOilService._decimal_to_string(row.get('Liter')),
            'box_size': ProductionReleaseOilService._integer_decimal_to_string(row.get('Box Size')),
            'volume_per_pc': ProductionReleaseOilService._decimal_to_string(row.get('Volume Per Pc')),
            'volume_per_box': ProductionReleaseOilService._decimal_to_string(row.get('Volume Per Box')),
            'batch_number': row.get('U_BATCH_NO') or '',
            'mfg_date': ProductionReleaseOilService._date_to_iso(row.get('MFG Date')),
            'exp_date': ProductionReleaseOilService._date_to_iso(row.get('Expiry Date')),
            'status': row.get('Status') or '',
        }

    @staticmethod
    def _date_to_iso(value) -> str:
        if not value:
            return ''
        if hasattr(value, 'date') and not hasattr(value, 'year'):
            value = value.date()
        if hasattr(value, 'isoformat'):
            return value.isoformat()[:10]
        return str(value)[:10]

    @staticmethod
    def _to_int(value) -> int | None:
        if value in (None, ''):
            return None
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _decimal_to_string(value) -> str:
        if value in (None, ''):
            return ''
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return str(value)
        return format(decimal_value.normalize(), 'f')

    @staticmethod
    def _integer_decimal_to_string(value) -> str:
        if value in (None, ''):
            return ''
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return str(value)
        if decimal_value == decimal_value.to_integral_value():
            return str(int(decimal_value))
        return format(decimal_value.normalize(), 'f')

    def _execute(self, sql: str) -> list[dict]:
        try:
            conn = self.client.context.hana
            from hdbcli import dbapi
            from sap_client.hana.connection import HANA_TIMEOUTS

            connection = dbapi.connect(
                address=conn['host'],
                port=conn['port'],
                user=conn['user'],
                password=conn['password'],
                **HANA_TIMEOUTS,
            )
            cursor = connection.cursor()
            cursor.execute(sql)
            cols = [col[0] for col in cursor.description]
            rows = cursor.fetchall()
            cursor.close()
            connection.close()
            return [dict(zip(cols, row)) for row in rows]
        except Exception as exc:
            raise ProductionReleaseReadError(str(exc))
