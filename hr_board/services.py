"""
hr_board/services.py

The HR control board, composed server-side in one read.

TWO REGISTERS, TWO DIFFERENT SCOPES, AND THE DIFFERENCE IS LOAD-BEARING
-----------------------------------------------------------------------
The two halves of this board do NOT answer to the company switcher in the same
way, and pretending they did would make one of them wrong.

**Head count is group-wide.** ``employee_hierarchy.Employee`` is one directory
for the whole factory: every one of its people sits under a single company FK,
and which plant they are costed to is ``sap_segment`` instead. The model says so
in as many words, and gives the reason -- splitting the directory across three
companies would fragment the org chart and break department name uniqueness. So
filtering the head count by the signed-in company would report the entire
directory under Oil and **zero** under Beverages, which is not a smaller truth,
it is a false one. The tile therefore reports the whole directory and breaks it
down by segment, and says so on its face via ``scope``.

**Labour is per company.** ``labour_gate.LabourGateEntry`` does carry a real
company FK and the two plants book their labour separately, so this half follows
the switcher like every other board.

That asymmetry is the single most surprising thing here. It is surfaced in the
payload (``headcount.scope`` / ``labour.scope``) rather than left implicit,
because a reader comparing "249 people" against "77 labour" needs to know that
only one of those two numbers changed when they switched company.

THE DAILY LABOUR FIGURE IS NOT ``Sum(count_in)``
------------------------------------------------
It is the sum over *intake* rows only. See :mod:`hr_board.constants` for why the
gate books every labourer twice and why the naive sum is exactly double.

NOTHING HERE READS SAP
----------------------
Both registers are Postgres, so every section is built with ``needs_sap=False``
and the SAP latch in :class:`~control_boards.sections.SectionBuilder` never
fires. That is why this board has no degraded state in normal operation: if it
is up at all, both tiles are real.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from django.db.models import Count, Sum
from django.utils import timezone

from control_boards.sections import SectionBuilder
from employee_hierarchy.constants import EmploymentStatus, IN_SERVICE_STATUSES
from employee_hierarchy.models import Employee
from labour_gate.models import LabourGateEntry

from .constants import (
    HR_BOARD_REFRESH_SECONDS,
    HR_BOARD_TOP_DEPARTMENTS,
    HR_BOARD_TOP_LABOUR_ROWS,
    HR_BOARD_TREND_DAYS,
    UNASSIGNED_LABEL,
)

logger = logging.getLogger(__name__)


def _labelled(rows, key: str, *, blank_label: str = UNASSIGNED_LABEL) -> List[Dict[str, Any]]:
    """``[{label, count}]`` from a ``values(...).annotate(n=...)`` result.

    A null or empty group becomes ``blank_label`` rather than being dropped.
    Dropping it is the tempting bug: the bars would still look sensible and
    would no longer add up to the total, which nobody notices on a wall screen.
    """
    out = []
    for row in rows:
        raw = row.get(key)
        label = str(raw).strip() if raw else ""
        out.append({"label": label or blank_label, "count": row["n"] or 0})
    return out


def _capped(rows: List[Dict[str, Any]], limit: int) -> Dict[str, Any]:
    """The top ``limit`` rows, with everything below them summed into one.

    Returned as ``{rows, other, other_count}`` rather than a truncated list so
    the tile can still show a total that adds up -- a chart whose bars sum to
    less than the headline is read as a broken chart.
    """
    head = rows[:limit]
    tail = rows[limit:]
    return {
        "rows": head,
        "other": sum(row["count"] for row in tail),
        "other_count": len(tail),
    }


class HrBoardService(SectionBuilder):
    """The whole HR board for one company, in one build.

    ``user`` may be ``None``, which withholds nothing -- the injectable
    collaborator pattern the other boards use, so a test can build the payload
    without a request.
    """

    def __init__(
        self,
        *,
        company_code: str,
        user=None,
        today: Optional[date] = None,
    ):
        self.company_code = company_code
        self.user = user
        self.today = today or timezone.localdate()
        self._init_sections()

    # ------------------------------------------------------------------ build

    def build(self) -> Dict[str, Any]:
        headcount = self.section(
            "headcount", self._headcount, needs_sap=False, feed="workforce"
        )
        labour = self.section("labour", self._labour, needs_sap=False, feed="labour")

        return {
            "headcount": headcount,
            "labour": labour,
            "meta": {
                "company": self.company_code,
                "as_of": self.today.isoformat(),
                "generated_at": timezone.now().isoformat(),
                "refresh_seconds": HR_BOARD_REFRESH_SECONDS,
                **self.section_meta(),
            },
        }

    # -------------------------------------------------------------- headcount

    def _headcount(self) -> Dict[str, Any]:
        """Who is on the rolls, by plant segment and by department.

        Counts people IN SERVICE rather than merely ``ACTIVE``: probation, leave
        and suspension are all still on the payroll, and
        ``IN_SERVICE_STATUSES`` is the directory's own definition of that. Using
        ``ACTIVE`` alone would quietly shrink the plant every time somebody took
        a month off.

        **No joiners, leavers, tenure or attrition figure is produced**, and
        that is deliberate. Every ``joining_date`` in the live directory is the
        date of the bulk import, because the field defaults to today and the
        sheet carried no real dates; ``exit_date`` has never been set and no
        record has ever reached an exit status. Any of those tiles would render
        a confident zero. They become possible the day HR back-fills the dates,
        and not before.
        """
        qs = Employee.objects.filter(employment_status__in=IN_SERVICE_STATUSES)

        total = qs.count()

        segments = _labelled(
            qs.values("sap_segment").annotate(n=Count("id")).order_by("-n", "sap_segment"),
            "sap_segment",
        )

        department_rows = _labelled(
            qs.values("department__name")
            .annotate(n=Count("id"))
            .order_by("-n", "department__name"),
            "department__name",
        )

        statuses = [
            {
                "key": row["employment_status"],
                "label": EmploymentStatus(row["employment_status"]).label,
                "count": row["n"],
            }
            for row in qs.values("employment_status")
            .annotate(n=Count("id"))
            .order_by("-n")
        ]

        unassigned = qs.filter(department__isnull=True).count()
        if unassigned:
            self.warn(
                f"{unassigned} of {total} people have no department on their "
                "record, and are grouped as Unassigned."
            )

        return {
            # The whole directory, not this company's slice. See the module
            # docstring -- the front end prints this so nobody reads the figure
            # as belonging to the company in the switcher.
            "scope": "group",
            "total": total,
            "segments": segments,
            "departments": _capped(department_rows, HR_BOARD_TOP_DEPARTMENTS),
            "department_count": qs.filter(department__isnull=False)
            .values("department")
            .distinct()
            .count(),
            "unassigned": unassigned,
            "managers": qs.filter(is_manager=True).count(),
            "statuses": statuses,
        }

    # ----------------------------------------------------------------- labour

    def _labour(self) -> Dict[str, Any]:
        """How many contract labourers entered, today and over the last month.

        Every figure here is built from INTAKE rows -- ``department IS NULL`` --
        except the departmental split, which is the one thing only the
        allocation rows know. :mod:`hr_board.constants` carries the why.
        """
        window_start = self.today - timedelta(days=HR_BOARD_TREND_DAYS - 1)

        entries = LabourGateEntry.objects.filter(
            company__code=self.company_code,
            deleted_at__isnull=True,
            # Never read ahead of today. A row dated forward is a typo, and on a
            # wall board it would land as a future day with a real-looking bar.
            work_date__lte=self.today,
        )
        intake = entries.filter(department__isnull=True)
        allocated = entries.filter(department__isnull=False)

        today_in = self._sum(intake.filter(work_date=self.today))
        today_allocated = self._sum(allocated.filter(work_date=self.today))

        # Allocation runs behind intake through the morning, so a shortfall is
        # normal at 09:00 and a standing problem at 18:00. Reported as its own
        # figure rather than as a warning for that reason -- the tile shows the
        # gap and lets the reader judge it by the clock.
        pending = max(today_in - today_allocated, 0)

        trend = self._trend(intake, window_start)
        worked = [day["count"] for day in trend if day["count"]]
        peak = max(trend, key=lambda day: day["count"]) if trend else None

        return {
            "scope": "company",
            "work_date": self.today.isoformat(),
            "today_in": today_in,
            "today_allocated": today_allocated,
            "pending_allocation": pending,
            "shifts": self._by_shift(intake.filter(work_date=self.today)),
            "contractors": _capped(
                _labelled(
                    intake.filter(work_date=self.today)
                    .values("contractor__contractor_name")
                    .annotate(n=Sum("count_in"))
                    .order_by("-n"),
                    "contractor__contractor_name",
                    blank_label="Unnamed contractor",
                ),
                HR_BOARD_TOP_LABOUR_ROWS,
            ),
            "departments": _capped(
                _labelled(
                    allocated.filter(work_date=self.today)
                    .values("department__name")
                    .annotate(n=Sum("count_in"))
                    .order_by("-n"),
                    "department__name",
                ),
                HR_BOARD_TOP_LABOUR_ROWS,
            ),
            "contractor_count": intake.filter(work_date=self.today)
            .values("contractor")
            .distinct()
            .count(),
            "trend": trend,
            "window_days": HR_BOARD_TREND_DAYS,
            # Averaged over days the gate actually booked somebody, not over the
            # calendar: a month containing four Sundays and two shutdowns would
            # otherwise report an average no real day ever looked like.
            "average_per_working_day": (
                round(sum(worked) / len(worked), 1) if worked else None
            ),
            "working_days": len(worked),
            "peak": peak,
        }

    @staticmethod
    def _sum(queryset) -> int:
        return queryset.aggregate(total=Sum("count_in"))["total"] or 0

    @staticmethod
    def _by_shift(queryset) -> List[Dict[str, Any]]:
        """Day and night, both always present.

        A night shift that has not started yet is a zero, not a missing row: the
        board is read at every hour and a tile whose shape changes through the
        day is one people stop trusting.
        """
        counts = {
            row["shift"]: row["n"] or 0
            for row in queryset.values("shift").annotate(n=Sum("count_in"))
        }
        return [
            {"key": "DAY", "label": "Day", "count": counts.get("DAY", 0)},
            {"key": "NIGHT", "label": "Night", "count": counts.get("NIGHT", 0)},
        ]

    def _trend(self, intake, window_start: date) -> List[Dict[str, Any]]:
        """One row per calendar day in the window, zeros included.

        Days with no intake are emitted as zero rather than skipped so the
        weekly rhythm stays visible -- a line chart that closes the gap over a
        shutdown draws a straight run through it and hides the very thing
        somebody is looking for.
        """
        booked = {
            row["work_date"]: row["n"] or 0
            for row in intake.filter(work_date__gte=window_start)
            .values("work_date")
            .annotate(n=Sum("count_in"))
        }
        days = (self.today - window_start).days
        return [
            {
                "date": (window_start + timedelta(days=offset)).isoformat(),
                "count": booked.get(window_start + timedelta(days=offset), 0),
            }
            for offset in range(days + 1)
        ]
