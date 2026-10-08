import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from sap_client.context import CompanyContext

from .analysis import NORMALIZED_COLUMN_ANALYSIS, PROCEDURE_OUTPUT_ANALYSIS
from .hana_reader import (
    PROCEDURE_COMPANY_CONFIG,
    HanaSalesPlanningRequirementReader,
    ProcedureResult,
    to_json_value,
)
from .models import (
    PROCEDURE_NAME,
    SalesPlanningRequirementRefreshRun,
    SalesPlanningRequirementRow,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
DECIMAL_OUTPUT = DecimalField(max_digits=24, decimal_places=6)


class SalesPlanningRefreshInProgress(Exception):
    pass


class SalesPlanningUnsupportedCompany(Exception):
    pass


class SalesPlanningRequirementService:
    def __init__(self, company_code: str):
        self.company_code = company_code

    def refresh(
        self,
        *,
        triggered_by: str,
        user=None,
        forecast_id: Optional[int] = None,
        forecast_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_supported_company()
        context = CompanyContext(self.company_code)
        reader = HanaSalesPlanningRequirementReader(context)
        run = self._start_run(
            source_schema=reader.source_schema,
            triggered_by=triggered_by,
            user=user,
        )

        try:
            result = reader.execute_procedure(
                forecast_id=forecast_id,
                forecast_name=forecast_name,
            )
            received = reader.received_quantities(
                result.forecast.start_date,
                result.forecast.end_date,
            )
            rows = list(self._build_rows(result, run, received))
            with transaction.atomic():
                run.source_schema = result.source_schema
                run.forecast_id = result.forecast.forecast_id
                run.forecast_name = result.forecast.forecast_name
                run.forecast_start_date = result.forecast.start_date
                run.forecast_end_date = result.forecast.end_date
                run.column_metadata = result.column_metadata
                run.procedure_parameters = {
                    "name": result.parameter_name,
                    "value": result.parameter_value,
                }
                run.save(
                    update_fields=[
                        "source_schema",
                        "forecast_id",
                        "forecast_name",
                        "forecast_start_date",
                        "forecast_end_date",
                        "column_metadata",
                        "procedure_parameters",
                        "updated_at",
                    ]
                )
                SalesPlanningRequirementRow.objects.filter(
                    company_code=self.company_code
                ).delete()
                SalesPlanningRequirementRow.objects.bulk_create(rows, batch_size=1000)

            run.mark_success(len(rows))
            logger.info(
                "Sales planning requirement refresh completed for %s: %s rows",
                self.company_code,
                len(rows),
            )
            return {
                "refresh": self.serialize_refresh_run(run),
                "summary": self.get_report({})["summary"],
            }
        except Exception as exc:
            logger.error(
                "Sales planning requirement refresh failed for %s: %s",
                self.company_code,
                exc,
                exc_info=True,
            )
            run.mark_failed(str(exc))
            raise

    def get_report(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        queryset = SalesPlanningRequirementRow.objects.filter(company_code=self.company_code)
        queryset = self._apply_filters(queryset, filters)

        page = max(int(filters.get("page") or 1), 1)
        page_size = min(max(int(filters.get("page_size") or DEFAULT_PAGE_SIZE), 1), MAX_PAGE_SIZE)
        total_items = queryset.count()
        total_pages = max((total_items + page_size - 1) // page_size, 1)
        offset = (page - 1) * page_size
        rows = queryset.order_by("-net_shortage_qty", "item_code")[offset:offset + page_size]

        return {
            "data": [self.serialize_row(row) for row in rows],
            "summary": self._build_summary(queryset),
            "refresh": self.get_refresh_status(),
            "meta": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1,
                "fetched_at": timezone.now().isoformat(),
            },
        }

    def get_refresh_status(self) -> Dict[str, Any]:
        latest = SalesPlanningRequirementRefreshRun.objects.filter(
            company_code=self.company_code
        ).first()
        latest_success = SalesPlanningRequirementRefreshRun.objects.filter(
            company_code=self.company_code,
            status=SalesPlanningRequirementRefreshRun.Status.SUCCESS,
        ).first()
        return {
            "latest": self.serialize_refresh_run(latest) if latest else None,
            "last_success": (
                self.serialize_refresh_run(latest_success) if latest_success else None
            ),
        }

    def get_analysis(self) -> Dict[str, Any]:
        latest_success = SalesPlanningRequirementRefreshRun.objects.filter(
            company_code=self.company_code,
            status=SalesPlanningRequirementRefreshRun.Status.SUCCESS,
        ).first()
        return {
            "procedure_name": PROCEDURE_NAME,
            "company_code": self.company_code,
            "supported_companies": list(PROCEDURE_COMPANY_CONFIG.keys()),
            "procedure_output": PROCEDURE_OUTPUT_ANALYSIS.get(self.company_code, []),
            "postgres_table": {
                "name": "sales_planning_requirement_salesplanningrequirementrow",
                "refresh_strategy": "Replace rows for the company inside a transaction only after SAP procedure succeeds.",
                "columns": NORMALIZED_COLUMN_ANALYSIS,
            },
            "latest_procedure_metadata": (
                latest_success.column_metadata if latest_success else []
            ),
            "scheduler": {
                "frequency": "Monthly",
                "command": "python manage.py run_sales_planning_requirement_scheduler",
                "default_cron": {
                    "day": getattr(settings, "SALES_PLANNING_REQUIREMENT_REFRESH_DAY", 1),
                    "hour": getattr(settings, "SALES_PLANNING_REQUIREMENT_REFRESH_HOUR", 2),
                    "minute": getattr(settings, "SALES_PLANNING_REQUIREMENT_REFRESH_MINUTE", 30),
                },
            },
        }

    def get_forecasts(self) -> Dict[str, Any]:
        self._ensure_supported_company()
        context = CompanyContext(self.company_code)
        reader = HanaSalesPlanningRequirementReader(context)
        forecasts = reader.get_forecasts()
        return {
            "data": [
                {
                    "forecast_id": forecast.forecast_id,
                    "forecast_name": forecast.forecast_name,
                    "start_date": forecast.start_date.isoformat() if forecast.start_date else None,
                    "end_date": forecast.end_date.isoformat() if forecast.end_date else None,
                    "line_count": forecast.line_count,
                }
                for forecast in forecasts
            ],
            "meta": {
                "source_schema": reader.source_schema,
                "fetched_at": timezone.now().isoformat(),
            },
        }

    def _ensure_supported_company(self) -> None:
        if self.company_code not in PROCEDURE_COMPANY_CONFIG:
            raise SalesPlanningUnsupportedCompany(
                f"{PROCEDURE_NAME} is not configured for {self.company_code}."
            )

    def _start_run(
        self,
        *,
        source_schema: str,
        triggered_by: str,
        user=None,
    ) -> SalesPlanningRequirementRefreshRun:
        stale_after = timezone.now() - timedelta(
            hours=getattr(settings, "SALES_PLANNING_REQUIREMENT_RUN_TIMEOUT_HOURS", 4)
        )
        SalesPlanningRequirementRefreshRun.objects.filter(
            company_code=self.company_code,
            status=SalesPlanningRequirementRefreshRun.Status.RUNNING,
            started_at__lt=stale_after,
        ).update(
            status=SalesPlanningRequirementRefreshRun.Status.FAILED,
            completed_at=timezone.now(),
            error_message="Marked failed because the refresh exceeded the timeout window.",
        )

        try:
            with transaction.atomic():
                return SalesPlanningRequirementRefreshRun.objects.create(
                    company_code=self.company_code,
                    source_schema=source_schema,
                    status=SalesPlanningRequirementRefreshRun.Status.RUNNING,
                    triggered_by=triggered_by,
                    started_by=user if getattr(user, "is_authenticated", False) else None,
                )
        except IntegrityError as exc:
            raise SalesPlanningRefreshInProgress(
                "A sales planning refresh is already running for this company."
            ) from exc

    def _build_rows(
        self,
        result: ProcedureResult,
        run: SalesPlanningRequirementRefreshRun,
        received: Dict[str, Decimal],
    ) -> Iterable[SalesPlanningRequirementRow]:
        for raw in result.rows:
            planned_qty = self._decimal(raw.get("Planned Qty"))
            base_required_qty = self._decimal(raw.get("RequiredQty"))
            beverage_required_qty = self._decimal(raw.get("Required Qty"))
            oil_required_qty = self._decimal(raw.get("Final Required Qty"))
            required_qty = oil_required_qty if oil_required_qty is not None else beverage_required_qty
            if base_required_qty is None:
                base_required_qty = required_qty

            open_po_qty = self._decimal(raw.get("Open PO Quantity")) or ZERO
            min_stock = self._decimal(raw.get("Min Stock")) or ZERO
            stock_in_hand = self._decimal(raw.get("Stock In Hand")) or ZERO
            required_qty = required_qty or ZERO
            net_shortage_qty = max(required_qty - open_po_qty, ZERO)
            planning_month = raw.get("Planning Month") or result.forecast.forecast_name
            report_execution_at = self._parse_datetime(
                raw.get("Report Execution Date & Time")
            ) or timezone.now()

            yield SalesPlanningRequirementRow(
                company_code=self.company_code,
                source_schema=result.source_schema,
                forecast_id=result.forecast.forecast_id,
                forecast_name=result.forecast.forecast_name,
                forecast_start_date=result.forecast.start_date,
                forecast_end_date=result.forecast.end_date,
                planning_month=planning_month or "",
                item_code=raw.get("ItemCode") or "",
                # Shown beside Open PO, not taken off the shortage: what has
                # been received is already in Stock In Hand, so subtracting
                # it again would count the same goods twice.
                received_qty=received.get(raw.get("ItemCode") or "", ZERO),
                item_name=raw.get("ItemName") or "",
                planned_qty=planned_qty,
                base_required_qty=base_required_qty,
                min_stock=min_stock,
                stock_in_hand=stock_in_hand,
                required_qty=required_qty,
                open_po_qty=open_po_qty,
                net_shortage_qty=net_shortage_qty,
                report_execution_at=report_execution_at,
                raw_payload={key: to_json_value(value) for key, value in raw.items()},
                refresh_run=run,
            )

    @staticmethod
    def _decimal(value: Any) -> Optional[Decimal]:
        if value is None or value == "":
            return None
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _parse_datetime(value: Any):
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str) and value.strip():
            text = value.strip().replace(" ", "T", 1)
            if "." in text:
                prefix, fraction = text.split(".", 1)
                fraction_digits = "".join(ch for ch in fraction if ch.isdigit())[:6]
                text = f"{prefix}.{fraction_digits}" if fraction_digits else prefix
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
        else:
            return None

        if timezone.is_naive(dt):
            return timezone.make_aware(dt, timezone.get_current_timezone())
        return dt

    def _apply_filters(self, queryset, filters: Dict[str, Any]):
        search = (filters.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(item_code__icontains=search)
                | Q(item_name__icontains=search)
                | Q(planning_month__icontains=search)
                | Q(forecast_name__icontains=search)
            )

        status_filter = filters.get("status")
        if status_filter == "shortage":
            queryset = queryset.filter(net_shortage_qty__gt=0)
        elif status_filter == "po_covered":
            queryset = queryset.filter(required_qty__gt=0, net_shortage_qty__lte=0)

        return queryset

    def _build_summary(self, queryset) -> Dict[str, Any]:
        aggregates = queryset.aggregate(
            total_items=Count("id"),
            total_planned_qty=Coalesce(
                Sum("planned_qty"),
                Value(ZERO),
                output_field=DECIMAL_OUTPUT,
            ),
            total_base_required_qty=Coalesce(
                Sum("base_required_qty"),
                Value(ZERO),
                output_field=DECIMAL_OUTPUT,
            ),
            total_required_qty=Coalesce(
                Sum("required_qty"),
                Value(ZERO),
                output_field=DECIMAL_OUTPUT,
            ),
            total_min_stock=Coalesce(
                Sum("min_stock"),
                Value(ZERO),
                output_field=DECIMAL_OUTPUT,
            ),
            total_stock_in_hand=Coalesce(
                Sum("stock_in_hand"),
                Value(ZERO),
                output_field=DECIMAL_OUTPUT,
            ),
            total_open_po_qty=Coalesce(
                Sum("open_po_qty"),
                Value(ZERO),
                output_field=DECIMAL_OUTPUT,
            ),
            total_net_shortage_qty=Coalesce(
                Sum("net_shortage_qty"),
                Value(ZERO),
                output_field=DECIMAL_OUTPUT,
            ),
            shortage_items=Count("id", filter=Q(net_shortage_qty__gt=0)),
            po_covered_items=Count("id", filter=Q(required_qty__gt=0, net_shortage_qty__lte=0)),
        )
        required = aggregates["total_required_qty"] or ZERO
        open_po = aggregates["total_open_po_qty"] or ZERO
        coverage_percent = (open_po / required * Decimal("100")) if required > 0 else ZERO

        return {
            "total_items": aggregates["total_items"],
            "total_planned_qty": self._float(aggregates["total_planned_qty"]),
            "total_base_required_qty": self._float(aggregates["total_base_required_qty"]),
            "total_required_qty": self._float(aggregates["total_required_qty"]),
            "total_min_stock": self._float(aggregates["total_min_stock"]),
            "total_stock_in_hand": self._float(aggregates["total_stock_in_hand"]),
            "total_open_po_qty": self._float(aggregates["total_open_po_qty"]),
            "total_net_shortage_qty": self._float(aggregates["total_net_shortage_qty"]),
            "shortage_items": aggregates["shortage_items"],
            "po_covered_items": aggregates["po_covered_items"],
            "open_po_coverage_percent": round(float(coverage_percent), 2),
        }

    @classmethod
    def serialize_row(cls, row: SalesPlanningRequirementRow) -> Dict[str, Any]:
        return {
            "id": row.id,
            "company_code": row.company_code,
            "forecast_id": row.forecast_id,
            "forecast_name": row.forecast_name,
            "forecast_start_date": (
                row.forecast_start_date.isoformat() if row.forecast_start_date else None
            ),
            "forecast_end_date": (
                row.forecast_end_date.isoformat() if row.forecast_end_date else None
            ),
            "planning_month": row.planning_month,
            "item_code": row.item_code,
            "item_name": row.item_name,
            "planned_qty": cls._float(row.planned_qty),
            "base_required_qty": cls._float(row.base_required_qty),
            "min_stock": cls._float(row.min_stock),
            "stock_in_hand": cls._float(row.stock_in_hand),
            "required_qty": cls._float(row.required_qty),
            "received_qty": (
                cls._float(row.received_qty) if row.received_qty is not None else None
            ),
            "open_po_qty": cls._float(row.open_po_qty),
            "net_shortage_qty": cls._float(row.net_shortage_qty),
            "status": "shortage" if row.net_shortage_qty > 0 else "po_covered",
            "report_execution_at": (
                row.report_execution_at.isoformat() if row.report_execution_at else None
            ),
            "loaded_at": row.loaded_at.isoformat() if row.loaded_at else None,
        }

    @staticmethod
    def serialize_refresh_run(
        run: Optional[SalesPlanningRequirementRefreshRun],
    ) -> Optional[Dict[str, Any]]:
        if not run:
            return None
        return {
            "id": run.id,
            "company_code": run.company_code,
            "source_schema": run.source_schema,
            "procedure_name": run.procedure_name,
            "forecast_id": run.forecast_id,
            "forecast_name": run.forecast_name,
            "forecast_start_date": (
                run.forecast_start_date.isoformat() if run.forecast_start_date else None
            ),
            "forecast_end_date": (
                run.forecast_end_date.isoformat() if run.forecast_end_date else None
            ),
            "status": run.status,
            "triggered_by": run.triggered_by,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "duration_seconds": run.duration_seconds,
            "rows_loaded": run.rows_loaded,
            "error_message": run.error_message,
            "procedure_parameters": run.procedure_parameters,
        }

    @staticmethod
    def _float(value: Any) -> float:
        if value is None:
            return 0.0
        return float(value)
