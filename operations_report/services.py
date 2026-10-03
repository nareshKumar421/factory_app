"""
operations_report/services.py

A company's production, wastage, labour and electricity, one record a day.

The Operations Report's page reads a period (a day or a month), the period it
is compared with and the run of days around it, so it asks for ONE span that
covers all three and does the adding up itself. That keeps this service to the
part only the server can do -- reading four registers -- and keeps the
definitions of the totals (what a litre's cost includes, what a month's head
count averages over) in one place, the page's ``summarise.ts``.

WHERE EACH FIGURE COMES FROM
----------------------------
- **Production** -- the floor's own runs, not SAP. Floor figures are the ones
  the plant stands behind. A run is filed under its own date; a draft is not
  production. A completed run counts the cases typed when it closed, an open
  one its segments so far -- the rule the run list already applies. Litres are
  cases x bottles a case x litres a bottle, the volumes snapshotted onto the run
  from SAP; never parsed out of a product name.
- **Wastage** -- the packing material waste register, dated by the run it was
  spoiled on (it is typed days later) and valued at the price that run was
  costed at, exactly as the Plant Control board does it. Grouped by the kind of
  material its name starts with, because 115 materials is not a report.
  Oil lost to yield is NOT here: SAP only answers that over a span, not a day.
- **Labour** -- the labour gate's intake rows (what walked through the
  barrier), per contractor and shift, priced at the Cost Master labour rate the
  Factory Expense board uses. The gate records heads only: there are no hours
  and no overtime anywhere to report.
- **Electricity** -- Daily Electricity++, the company's share of each meter's
  own units (reading less sub-meters), which is what every board now reads.

A section that could not be read is ``None`` on every day and named in
``meta.degraded``; one this reader may not see is ``None`` and named in
``meta.withheld``. A day with nothing booked is an empty list, which is a
different fact from a section nobody could read.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from django.db.models import Q, Sum
from django.utils import timezone

from control_boards.sections import SectionBuilder
from factory_expense.constants import LABOUR_COST_TYPE_CODE
from factory_expense.models import FactoryExpenseSettings
from factory_expense.rates import load_rates, resolve
from factory_expense.services import _price_labour
from labour_gate.models import LabourGateEntry, LabourShift
from production_execution.models import (
    ProductionMaterialUsage,
    ProductionRun,
    RunStatus,
    WasteApprovalStatus,
    WasteLog,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")

#: The longest span one read may cover. A month on the page is read with the
#: month before it, so two months and change is the most it ever asks for.
MAX_SPAN_DAYS = 93

#: A waste row's kind of material, by the word its name starts with. The
#: register's names are written type-first ("CAPS 1 LTR ...", "LABEL 5 LTR
#: ...", "CARTON ..."), and that word is the only category it carries.
#: Checked in order, so the longer prefixes come first.
WASTE_KINDS = (
    ("PET BOTTLE", "Bottles & jars"),
    ("HDPE BOTTLE", "Bottles & jars"),
    ("BOTTLE", "Bottles & jars"),
    ("JAR", "Bottles & jars"),
    ("PREFORM", "Bottles & jars"),
    ("CAP", "Caps"),
    ("LABEL", "Labels"),
    ("CARTON", "Cartons"),
    ("SHRINK", "Shrink film"),
    ("TIN", "Tins"),
    ("POUCH", "Pouch film"),
    ("LAMINATE", "Pouch film"),
    ("TAPE", "Tape"),
)
OTHER_WASTE = "Other packing"

#: The register's units, as the page prints them.
UNITS = {
    "PCS": "pcs",
    "NOS": "pcs",
    "PC": "pcs",
    "MTR": "m",
    "MTRS": "m",
    "KG": "kg",
    "KGS": "kg",
    "GM": "g",
    "GMS": "g",
}


def waste_kind(material_name: str) -> str:
    name = (material_name or "").strip().upper()
    for prefix, kind in WASTE_KINDS:
        if name.startswith(prefix):
            return kind
    return OTHER_WASTE


def _unit(uom: str) -> str:
    raw = (uom or "").strip().upper()
    return UNITS.get(raw, raw.lower() or "units")


def _num(value: Optional[Decimal], places: int = 2) -> Optional[float]:
    """A Decimal as a JSON number, rounded; None stays None."""
    if value is None:
        return None
    return float(round(Decimal(value), places))


def days_between(date_from: date, date_to: date) -> List[date]:
    return [date_from + timedelta(days=offset) for offset in range((date_to - date_from).days + 1)]


class OperationsReportService(SectionBuilder):
    """The report's days for one company and one span."""

    def __init__(self, company, date_from: date, date_to: date, user=None, now=None):
        self.company = company
        self.date_from = date_from
        self.date_to = date_to
        self.user = user
        self.now = now or timezone.now()
        self.days = days_between(date_from, date_to)
        self._init_sections()

    # ------------------------------------------------------------------ build

    def build(self) -> Dict:
        # None of these reads SAP: runs, waste, the gate and the meters are all
        # the app's own registers, so an SAP outage cannot blank this report.
        lines = self.section(
            "production", self._production, needs_sap=False, feed="production_cost"
        )
        wastage = self.section(
            "wastage", self._wastage, needs_sap=False, feed="production_cost"
        )
        labour = self.section("labour", self._labour, needs_sap=False, feed="factory_expense")
        power = self.section("power", self._power, needs_sap=False, feed="factory_expense")

        def day_of(section, day):
            # None for the whole section when it was not read; for a day inside
            # a section that was, whatever the section said -- an empty list
            # for nothing booked, or None where the register itself has a gap.
            if section is None:
                return None
            return section.get(day, [])

        return {
            "company": {"code": self.company.code, "name": self.company.name},
            "from": self.date_from.isoformat(),
            "to": self.date_to.isoformat(),
            "days": [
                {
                    "date": day.isoformat(),
                    "lines": day_of(lines, day),
                    "wastage": day_of(wastage, day),
                    "labour": day_of(labour, day),
                    "power": day_of(power, day),
                }
                for day in self.days
            ],
            "meta": self.section_meta(),
        }

    # ------------------------------------------------------------- production

    def _production(self) -> Dict[date, List[Dict]]:
        runs = (
            ProductionRun.objects.filter(
                company=self.company, date__range=(self.date_from, self.date_to)
            )
            .exclude(status=RunStatus.DRAFT)
            .select_related("line")
            .annotate(segment_cases=Sum("segments__produced_cases"))
            .order_by("date", "line__name", "run_number")
        )

        rows: Dict[date, Dict[str, Dict]] = defaultdict(dict)
        no_volume = []
        for run in runs:
            if run.status == RunStatus.COMPLETED and run.total_production:
                cases = Decimal(run.total_production)
            else:
                cases = Decimal(run.segment_cases or 0)

            name = run.line.name if run.line_id else "No line"
            row = rows[run.date].setdefault(
                name, {"line": name, "runs": 0, "cases": ZERO, "litres": ZERO}
            )
            row["runs"] += 1
            row["cases"] += cases
            if run.pieces_per_case and run.litres_per_piece:
                if row["litres"] is not None:
                    row["litres"] += cases * run.pieces_per_case * run.litres_per_piece
            elif cases:
                # Unknown, not short: a line-day's litres missing one run's
                # volume would understate it and overstate its cost per litre.
                row["litres"] = None
                no_volume.append(run)

        if no_volume:
            first = min(run.date for run in no_volume)
            last = max(run.date for run in no_volume)
            when = (
                first.strftime("%-d %b")
                if first == last
                else f"{first.strftime('%-d %b')} to {last.strftime('%-d %b')}"
            )
            count = len(no_volume)
            self.warn(
                f"{count} run{'s' if count != 1 else ''} ({when}) "
                f"{'have' if count != 1 else 'has'} no bottle size or litres a bottle, "
                "so the litres of those lines and days are not known. Their cases are "
                "counted."
            )

        return {
            day: [
                {
                    "line": row["line"],
                    "runs": row["runs"],
                    "cases": _num(row["cases"], 1),
                    "litres": _num(row["litres"], 1),
                }
                for row in sorted(by_line.values(), key=lambda r: -(r["litres"] or 0))
            ]
            for day, by_line in rows.items()
        }

    # ---------------------------------------------------------------- wastage

    def _wastage(self) -> Dict[date, List[Dict]]:
        in_span = Q(production_run__date__range=(self.date_from, self.date_to)) | Q(
            production_run__isnull=True,
            created_at__date__range=(self.date_from, self.date_to),
        )
        logs = list(
            WasteLog.objects.filter(
                Q(company=self.company) | Q(production_run__company=self.company)
            )
            .filter(in_span)
            .exclude(production_run__is_deleted=True)
            .exclude(wastage_approval_status=WasteApprovalStatus.REJECTED)
            .values(
                "production_run_id",
                "production_run__date",
                "created_at",
                "material_code",
                "material_name",
                "wastage_qty",
                "uom",
            )
        )

        exact, latest = self._waste_prices(logs)
        rows: Dict[date, Dict[tuple, Dict]] = defaultdict(dict)
        unpriced = set()
        for log in logs:
            day = log["production_run__date"] or timezone.localtime(log["created_at"]).date()
            kind = waste_kind(log["material_name"])
            unit = _unit(log["uom"])
            row = rows[day].setdefault(
                (kind, unit),
                {"item": kind, "unit": unit, "quantity": ZERO, "value": ZERO, "unpriced": 0},
            )
            qty = Decimal(log["wastage_qty"] or 0)
            row["quantity"] += qty
            price = exact.get((log["production_run_id"], log["material_code"])) or latest.get(
                log["material_code"]
            )
            if price is None:
                row["unpriced"] += 1
                unpriced.add(log["material_name"])
            else:
                row["value"] += qty * price

        if unpriced:
            listed = ", ".join(sorted(unpriced)[:6])
            more = f" and {len(unpriced) - 6} more" if len(unpriced) > 6 else ""
            self.warn(
                f"No SAP price for {listed}{more}: that waste is counted but left out "
                "of the wastage value."
            )

        return {
            day: [
                {
                    "item": row["item"],
                    "unit": row["unit"],
                    "quantity": _num(row["quantity"], 2),
                    "value": _num(row["value"]),
                    "unpriced": row["unpriced"],
                }
                for row in sorted(by_kind.values(), key=lambda r: -r["value"])
            ]
            for day, by_kind in rows.items()
        }

    def _waste_prices(self, logs):
        """The price each run was costed at for a material, and the newest price
        seen for it anywhere as the stand-in -- Plant Control's rule."""
        codes = sorted({log["material_code"] for log in logs if log["material_code"]})
        if not codes:
            return {}, {}
        exact, latest = {}, {}
        for line in (
            ProductionMaterialUsage.objects.filter(
                production_run__company=self.company, material_code__in=codes
            )
            .exclude(unit_price=None)
            .exclude(unit_price=0)
            .order_by("production_run__date", "id")
            .values("production_run_id", "material_code", "unit_price")
        ):
            exact[(line["production_run_id"], line["material_code"])] = line["unit_price"]
            latest[line["material_code"]] = line["unit_price"]
        return exact, latest

    # ----------------------------------------------------------------- labour

    def _labour(self) -> Dict[date, List[Dict]]:
        # Intake rows only. A row with a department is an HOD splitting the
        # same people afterwards; adding the two counts every allocated
        # labourer twice.
        entries = (
            LabourGateEntry.objects.filter(
                company=self.company,
                department__isnull=True,
                is_active=True,
                deleted_at__isnull=True,
                work_date__range=(self.date_from, self.date_to),
            )
            .values("work_date", "shift", "contractor__contractor_name")
            .annotate(heads=Sum("count_in"))
        )

        by_day: Dict[date, Dict[str, Dict]] = defaultdict(dict)
        for entry in entries:
            heads = entry["heads"] or 0
            if not heads:
                continue
            name = entry["contractor__contractor_name"] or "No contractor"
            row = by_day[entry["work_date"]].setdefault(
                name, {"group": name, "heads": 0, "day_shift": 0, "night_shift": 0}
            )
            row["heads"] += heads
            if entry["shift"] == LabourShift.NIGHT:
                row["night_shift"] += heads
            else:
                row["day_shift"] += heads

        code = (
            FactoryExpenseSettings.objects.filter(company=self.company)
            .values_list("labour_cost_type_code", flat=True)
            .first()
            or LABOUR_COST_TYPE_CODE
        )
        rates = load_rates(code, self.company, self.date_to)

        out: Dict[date, List[Dict]] = {}
        unpriced_days = []
        for day, groups in by_day.items():
            day_heads = sum(row["heads"] for row in groups.values())
            rate = resolve(rates, None, day)
            # Priced for the whole day and then shared out by heads, so a flat
            # daily charge lands once rather than once per contractor.
            amount = _price_labour(rate, day_heads) if rate is not None else None
            if amount is None:
                unpriced_days.append(day)
            out[day] = [
                {
                    **row,
                    "cost": None
                    if amount is None
                    else _num(amount * row["heads"] / day_heads),
                }
                for row in sorted(groups.values(), key=lambda r: -r["heads"])
            ]

        if not by_day:
            # The gate books contract labour under the company that runs it, so a
            # plant with none of its own reads as nil here. Said, because a nil
            # wage bill under a full production column looks like free labour.
            self.warn(
                f"No contract labour was booked through the labour gate under "
                f"{self.company.name} in this span, so labour reads as nil."
            )
        if unpriced_days:
            self.warn(
                f"{len(unpriced_days)} day{'s' if len(unpriced_days) != 1 else ''} of "
                f"labour have no '{code}' rate in the Cost Master on that date, so "
                "their people are counted but not costed."
            )
        return out

    # ------------------------------------------------------------ electricity

    def _power(self) -> Dict[date, Optional[List[Dict]]]:
        from maintenance.electricity import boards

        result = boards.breakdown(self.date_from, self.date_to)
        mine = boards.company(result, self.company.code)
        entered = set(result.get("entered_days") or [])

        out: Dict[date, Optional[List[Dict]]] = {}
        unread = []
        for day in self.days:
            meters = mine["by_day_meter"].get(day)
            if not meters and day not in entered:
                # Nobody entered the register that day: a gap, not zero units.
                out[day] = None
                unread.append(day)
                continue
            out[day] = [
                {"area": name, "kwh": _num(part["units"]), "cost": _num(part["cost"])}
                for name, part in sorted(
                    (meters or {}).items(), key=lambda kv: -kv[1]["units"]
                )
                if part["units"]
            ]

        if unread:
            self.warn(
                f"No meter readings were entered for {len(unread)} "
                f"day{'s' if len(unread) != 1 else ''} in this span; those days show "
                "no electricity rather than zero."
            )
        if not mine["units"] and len(unread) < len(self.days):
            self.warn(
                f"Daily Electricity++ has no units for {self.company.name} in this span."
            )
        return out
