import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.hana.connection import HanaConnection

from .models import PROCEDURE_NAME

logger = logging.getLogger(__name__)


HANA_TYPE_NAMES = {
    5: "DECIMAL",
    11: "NVARCHAR",
}

PROCEDURE_COMPANY_CONFIG = {
    "JIVO_BEVERAGES": {
        "parameter": "forecast_id",
    },
    "JIVO_OIL": {
        "parameter": "forecast_name",
    },
}


@dataclass(frozen=True)
class ForecastSelection:
    forecast_id: Optional[int]
    forecast_name: str
    start_date: Optional[date]
    end_date: Optional[date]
    line_count: int = 0


@dataclass(frozen=True)
class ProcedureResult:
    rows: List[Dict[str, Any]]
    column_metadata: List[Dict[str, Any]]
    forecast: ForecastSelection
    source_schema: str
    parameter_name: str
    parameter_value: Any


class HanaSalesPlanningRequirementReader:
    def __init__(self, context):
        self.connection = HanaConnection(context.hana)
        company_code = getattr(context, "company_code", "")
        self.company_code = company_code if isinstance(company_code, str) else ""

    @property
    def source_schema(self) -> str:
        return self._validate_identifier(self.connection.schema)

    def get_forecasts(self, limit: int = 24) -> List[ForecastSelection]:
        query = f"""
SELECT
    F."AbsID",
    F."Name",
    F."StartDate",
    F."EndDate",
    COUNT(L."ItemCode") AS "LineCount"
FROM "{self.source_schema}"."OFCT" F
JOIN "{self.source_schema}"."FCT1" L
    ON L."AbsID" = F."AbsID"
GROUP BY F."AbsID", F."Name", F."StartDate", F."EndDate"
ORDER BY
    CASE
        WHEN CURRENT_DATE BETWEEN F."StartDate" AND F."EndDate" THEN 0
        WHEN F."EndDate" >= CURRENT_DATE THEN 1
        ELSE 2
    END,
    F."StartDate" DESC,
    F."AbsID" DESC
LIMIT ?
"""
        rows = self._execute(query, [limit])
        return [self._map_forecast(row) for row in rows]

    def fetch_latest_forecast(self) -> ForecastSelection:
        forecasts = self.get_forecasts(limit=1)
        if not forecasts:
            raise SAPDataError("No SAP forecast found for sales planning refresh.")
        return forecasts[0]

    def execute_procedure(
        self,
        *,
        forecast_id: Optional[int] = None,
        forecast_name: Optional[str] = None,
    ) -> ProcedureResult:
        config = PROCEDURE_COMPANY_CONFIG.get(self.company_code)
        if not config:
            raise SAPValidationError(
                f"{PROCEDURE_NAME} is not configured for company {self.company_code}."
            )

        forecast = self._resolve_forecast(
            forecast_id=forecast_id,
            forecast_name=forecast_name,
        )
        parameter_name = config["parameter"]
        parameter_value = forecast.forecast_id if parameter_name == "forecast_id" else forecast.forecast_name

        if parameter_value in (None, ""):
            raise SAPDataError("Selected forecast does not have a valid procedure parameter.")

        query = f'CALL "{self.source_schema}"."{PROCEDURE_NAME}"(?)'
        rows, metadata = self._execute_with_metadata(query, [parameter_value])
        return ProcedureResult(
            rows=rows,
            column_metadata=metadata,
            forecast=forecast,
            source_schema=self.source_schema,
            parameter_name=parameter_name,
            parameter_value=parameter_value,
        )

    def received_quantities(
        self,
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> Dict[str, Decimal]:
        """
        What each item has been received on posted GRPOs inside the forecast's
        own window -- the planning month the requirement is for.

        In the inventory unit (`InvQty`), so it reads in the same unit as
        Stock In Hand beside it; a carton line received in boxes of 100 would
        otherwise be a hundred times too small. Cancelled GRPOs, and the
        cancellation documents that undo them, are both left out.

        A forecast with no dates has no month to count, so it gets nothing
        rather than every GRPO ever posted.
        """
        if start_date is None or end_date is None:
            return {}
        query = f"""
SELECT
    L."ItemCode",
    SUM(IFNULL(L."InvQty", 0)) AS "Received"
FROM "{self.source_schema}"."OPDN" H
JOIN "{self.source_schema}"."PDN1" L
    ON L."DocEntry" = H."DocEntry"
WHERE H."CANCELED" = 'N'
  AND H."DocDate" BETWEEN ? AND ?
GROUP BY L."ItemCode"
"""
        rows = self._execute(query, [start_date, end_date])
        return {row[0]: Decimal(str(row[1] or 0)) for row in rows if row[0]}

    def _resolve_forecast(
        self,
        *,
        forecast_id: Optional[int],
        forecast_name: Optional[str],
    ) -> ForecastSelection:
        if forecast_id is not None:
            return self._get_forecast_by_where('F."AbsID" = ?', [forecast_id])
        if forecast_name:
            return self._get_forecast_by_where('F."Name" = ?', [forecast_name])
        return self.fetch_latest_forecast()

    def _get_forecast_by_where(self, where_clause: str, params: List[Any]) -> ForecastSelection:
        query = f"""
SELECT
    F."AbsID",
    F."Name",
    F."StartDate",
    F."EndDate",
    COUNT(L."ItemCode") AS "LineCount"
FROM "{self.source_schema}"."OFCT" F
LEFT JOIN "{self.source_schema}"."FCT1" L
    ON L."AbsID" = F."AbsID"
WHERE {where_clause}
GROUP BY F."AbsID", F."Name", F."StartDate", F."EndDate"
"""
        rows = self._execute(query, params)
        if not rows:
            raise SAPDataError("Selected SAP forecast was not found.")
        return self._map_forecast(rows[0])

    @staticmethod
    def _map_forecast(row) -> ForecastSelection:
        return ForecastSelection(
            forecast_id=int(row[0]) if row[0] is not None else None,
            forecast_name=row[1] or "",
            start_date=row[2].date() if isinstance(row[2], datetime) else row[2],
            end_date=row[3].date() if isinstance(row[3], datetime) else row[3],
            line_count=int(row[4] or 0),
        )

    def _execute(self, query: str, params: List[Any]) -> List:
        rows, _metadata = self._execute_raw(query, params)
        return rows

    def _execute_with_metadata(
        self,
        query: str,
        params: List[Any],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        rows, description = self._execute_raw(query, params)
        columns = [column[0] for column in description or []]
        metadata = [self._map_column_metadata(column) for column in description or []]
        return [dict(zip(columns, row)) for row in rows], metadata

    def _execute_raw(self, query: str, params: List[Any]) -> tuple[List, List]:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.error("SAP HANA connection failed for sales planning requirement: %s", exc)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            description = list(cursor.description or [])
            return cursor.fetchall(), description
        except dbapi.ProgrammingError as exc:
            logger.error(
                "SAP HANA procedure/query failed for sales planning requirement: %s",
                exc,
            )
            raise SAPDataError(
                "Failed to retrieve sales planning requirement data from SAP."
            ) from exc
        except dbapi.Error as exc:
            logger.error("SAP HANA data error for sales planning requirement: %s", exc)
            raise SAPDataError(
                "Failed to retrieve sales planning requirement data from SAP."
            ) from exc
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
    def _map_column_metadata(description_column) -> Dict[str, Any]:
        type_code = description_column[1] if len(description_column) > 1 else None
        return {
            "name": description_column[0],
            "hana_type_code": type_code,
            "hana_type": HANA_TYPE_NAMES.get(type_code, str(type_code)),
            "display_size": description_column[2] if len(description_column) > 2 else None,
            "internal_size": description_column[3] if len(description_column) > 3 else None,
            "precision": description_column[4] if len(description_column) > 4 else None,
            "scale": description_column[5] if len(description_column) > 5 else None,
            "nullable": description_column[6] if len(description_column) > 6 else None,
        }

    @staticmethod
    def _validate_identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", value or ""):
            raise SAPValidationError("Invalid SAP HANA schema identifier.")
        return value


def to_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
