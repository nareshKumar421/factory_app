"""
sap_reports/services/runner.py

Running one SAP report and handing back its result set.

The path is short on purpose: check the report may be run, turn the supplied
values into bind parameters, execute, cap the rows, record the run. Nothing is
cached and no result is stored -- a report always shows what SAP holds now.
"""

import logging
import time
from typing import Dict, List, Optional

from django.utils import timezone

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from ..exceptions import SapReportError, SapReportParameterError
from ..hana_reader import HanaSapReportReader
from ..models import SapReport, SapReportRun
from ..parameters import build_bind_values
from ..sql import assert_read_only, bind_prompts

logger = logging.getLogger(__name__)

# How many rows one run may return. These queries are wide date-range scans over
# a shared SAP box, so an unbounded pull is a real risk to everyone else on it.
DEFAULT_ROW_LIMIT = 5000
MAX_ROW_LIMIT = 50000

# An export is a file, not a screen, so it is allowed to be bigger.
EXPORT_ROW_LIMIT = 50000


class SapReportRunner:
    """Executes a catalogued SAP report for one company."""

    def __init__(self, company, user=None):
        self.company = company
        self.user = user
        self.context = CompanyContext(company.code)
        self.reader = HanaSapReportReader(self.context)

    def run(
        self,
        report: SapReport,
        supplied_parameters: Optional[Dict] = None,
        *,
        row_limit: Optional[int] = None,
        export_format: str = "",
    ) -> Dict:
        """
        Runs ``report`` and returns ``{columns, rows, meta}``.

        Raises ``SapReportError`` (report not runnable), ``SapReportParameterError``
        (bad or missing filter), or the ``sap_client`` SAP errors.
        """
        self._assert_runnable(report)

        parameters = list(report.parameters.all())
        values = build_bind_values(parameters, supplied_parameters or {})
        statement, binds = bind_prompts(report.sql_text, values)

        limit = self._resolve_row_limit(report, row_limit, export_format)
        started = time.monotonic()

        try:
            columns, rows, was_truncated = self.reader.run_statement(
                statement, binds, row_limit=limit
            )
        except (SAPConnectionError, SAPDataError) as error:
            self._record(
                report,
                values,
                status=SapReportRun.Status.ERROR,
                duration_ms=self._elapsed_ms(started),
                export_format=export_format,
                error_message=str(error),
            )
            raise

        duration_ms = self._elapsed_ms(started)
        self._record(
            report,
            values,
            status=SapReportRun.Status.SUCCESS,
            duration_ms=duration_ms,
            export_format=export_format,
            row_count=len(rows),
            was_truncated=was_truncated,
        )
        report.mark_run()

        return {
            "columns": columns,
            "rows": rows,
            "meta": {
                "report": report.slug,
                "title": report.title,
                "company": self.company.code,
                "row_count": len(rows),
                "row_limit": limit,
                "was_truncated": was_truncated,
                "duration_ms": duration_ms,
                "executed_at": timezone.now().isoformat(),
                "parameters": self._echo_parameters(parameters, values),
            },
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_runnable(report: SapReport) -> None:
        if not report.is_enabled:
            raise SapReportError("This report is switched off.")
        if report.is_missing_in_sap:
            raise SapReportError(
                "This report no longer exists in SAP. Ask an administrator to re-sync."
            )
        if not report.is_runnable:
            raise SapReportError(
                report.not_runnable_reason or "This report cannot be run from the app."
            )
        # Re-checked at run time, not just at sync time: the stored SQL is the
        # thing about to be executed, so it is the thing that must be verified.
        assert_read_only(report.sql_text)

    @staticmethod
    def _resolve_row_limit(
        report: SapReport,
        requested: Optional[int],
        export_format: str,
    ) -> int:
        ceiling = EXPORT_ROW_LIMIT if export_format else MAX_ROW_LIMIT
        default = EXPORT_ROW_LIMIT if export_format else report.effective_row_limit
        limit = requested or default
        return max(1, min(int(limit), ceiling))

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _echo_parameters(parameters, values: Dict[int, object]) -> List[Dict]:
        """The filters the result was produced with, labelled for display."""
        return [
            {
                "position": parameter.position,
                "label": parameter.label,
                "kind": parameter.kind,
                "value": values.get(parameter.position),
            }
            for parameter in parameters
        ]

    def _record(
        self,
        report: SapReport,
        values: Dict[int, object],
        *,
        status: str,
        duration_ms: int,
        export_format: str = "",
        row_count: int = 0,
        was_truncated: bool = False,
        error_message: str = "",
    ) -> None:
        """Writes the audit row. A failure to audit must not fail the report."""
        try:
            SapReportRun.objects.create(
                report=report,
                company=self.company,
                run_by=self.user if getattr(self.user, "is_authenticated", False) else None,
                parameters={str(position): value for position, value in values.items()},
                status=status,
                row_count=row_count,
                was_truncated=was_truncated,
                duration_ms=duration_ms,
                export_format=export_format,
                error_message=error_message[:2000],
            )
        except Exception:  # pragma: no cover - auditing is best-effort
            logger.exception("Could not record SAP report run for %s", report.slug)


__all__ = [
    "SapReportRunner",
    "DEFAULT_ROW_LIMIT",
    "MAX_ROW_LIMIT",
    "EXPORT_ROW_LIMIT",
    "SapReportError",
    "SapReportParameterError",
]
