"""
admin_board/services.py

The admin control board, composed.

Three tiles, in the order an owner asks about them: what the plant MADE and
SHIPPED, what is STANDING in it, and what it COST. Then an action centre that
turns the first three into things to do.

WHY THE ALERTS ARE COMPUTED HERE AND NOT ON THE SCREEN
------------------------------------------------------
Every alert is a rule over figures this service already holds, and the rules are
the part a business argues about — "90% full" and "behind plan" are thresholds
somebody chose. Deriving them in the front end would put the definitions in the
one place that cannot be unit-tested against live data and would let a second
consumer of this endpoint reach different conclusions from identical numbers.
So the service emits the alerts and the page renders them.

WHY EVERY SECTION CATCHES ITS OWN FAILURE
-----------------------------------------
Copied deliberately from ``plant_board``: this screen is also read by people
who are not sitting in front of it, and a 500 on a dashboard stays up until
somebody notices. A section that cannot be read is named in ``meta.degraded``
and rendered as a rule, never as a confident zero.
"""

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from django.db.models import Sum
from django.utils import timezone

from company.models import Company
from planning_purchase.services.plan_service import PlanService
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from stock_dashboard.models import PlantBoardSettings, WarehouseBoardSettings
from stock_dashboard.services import StockDashboardService

from .alerts import build_alerts
from .constants import (
    COST_SLICES,
    DISPATCH_COMPANIES,
    FG_STORES,
    INTERCOMPANY_CARD_CODES,
    LITRES_PER_TON,
    OIL_TANK,
    PM_STORES,
    PRODUCTION_FLOOR,
    REFRESH_SECONDS,
    TREND_DAYS,
    UNRATED_FG_WAREHOUSES,
)
from .hana_reader import AdminBoardReader
from .tonnage import FINISHED_ITEM_GROUP, litres as sum_litres, roll_up

logger = logging.getLogger(__name__)


def _f(value) -> float:
    """A float, whatever SAP handed back. Decimals and None both land here."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _f_or_none(value) -> Optional[float]:
    """A float, or None — for figures where absent and zero differ."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tons(litres) -> float:
    """Litres to tonnes at the business's fixed 1,000 L = 1 T."""
    return round(_f(litres) / LITRES_PER_TON, 2)


def _pct(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    """A percentage, or None where there is no denominator.

    None rather than zero is the whole point: an unrated warehouse and an empty
    one must not render the same. Zero capacity is treated as no capacity —
    a warehouse that holds nothing is not a warehouse that is infinitely full.
    """
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


def _as_date(value) -> Optional[date]:
    """SAP hands back datetimes; the board compares dates.

    ``datetime`` IS a subclass of ``date``, so an ``isinstance(value, date)``
    test matches a datetime and hands it straight back — and
    ``datetime(2026, 9, 15, 0, 0) != date(2026, 9, 15)``, so every lookup keyed
    on a real date then misses. That silently emptied the trend strip and
    today's figure while leaving the month total correct, which is the worst
    shape a bug like this can take. Narrow to ``datetime`` first.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


class AdminBoardService:
    """Builds the whole admin board for one company context.

    Readers are injectable so every tile's arithmetic can be exercised against
    fixed rows, with no HANA connection and no live database.
    """

    def __init__(
        self,
        company_code: str,
        readers: Optional[Dict[str, AdminBoardReader]] = None,
        cost_board: Optional[Callable[..., Dict[str, Any]]] = None,
        stock: Optional[Dict[str, StockDashboardService]] = None,
        plans: Optional[PlanService] = None,
        today: Optional[date] = None,
    ):
        self.company_code = company_code
        self.today = today or timezone.localdate()
        self.month_first = self.today.replace(day=1)
        self.days_in_month = monthrange(self.today.year, self.today.month)[1]

        self._readers: Dict[str, AdminBoardReader] = dict(readers or {})
        self._contexts: Dict[str, CompanyContext] = {}
        self._cost_board = cost_board
        #: One stock service per company, built on first use.
        #:
        #: The EXISTING service, not a private query. Its occupancy reader is
        #: what the Logistics and Production boards weigh their warehouses
        #: from, and the whole point of this rewrite is that three boards in one
        #: room must not each have their own idea of what a tonne is.
        self._stock: Dict[str, StockDashboardService] = dict(stock or {})
        self._plans = plans

        self._degraded: List[str] = []
        self._warnings: List[str] = []
        #: Set the first time SAP times out and never cleared within a build.
        #: A board that re-reads every minute cannot afford to discover the same
        #: outage four times and wear four connection timeouts doing it.
        self._sap_down = False

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _reader(self, company_code: str) -> AdminBoardReader:
        """One reader per company, built on first use.

        Per company rather than one for the board, because dispatch spans two
        SAP schemas and a schema is reached only by connecting as that company.
        """
        if company_code not in self._readers:
            if company_code not in self._contexts:
                self._contexts[company_code] = CompanyContext(company_code)
            self._readers[company_code] = AdminBoardReader(self._contexts[company_code])
        return self._readers[company_code]

    def _stock_for(self, company_code: str) -> StockDashboardService:
        """This company's stock service, built on first use."""
        if company_code not in self._stock:
            self._stock[company_code] = StockDashboardService(company_code)
        return self._stock[company_code]

    def _plan_service(self) -> PlanService:
        if self._plans is None:
            self._plans = PlanService(self.company_code)
        return self._plans

    def _occupancy(self, company_code: str, warehouse: str, finished_only: bool = True):
        """One warehouse's rows, scoped the way the Logistics board scopes them.

        ``finished_only`` is the scope half of the bug this method was written
        to fix: a tile labelled "FG storage" that counts every item group also
        counts 230 tonnes of group 105 standing in BH-BT. Passed False only for
        the tank farm, which holds raw oil and no finished goods at all.
        """
        groups = [FINISHED_ITEM_GROUP] if finished_only else None
        payload = self._stock_for(company_code).get_warehouse_occupancy(
            warehouse, item_groups=groups
        )
        return payload.get("data") or []

    def _section(self, name: str, build: Callable[[], Any], needs_sap: bool = True) -> Any:
        """Run one tile, or report it as degraded and carry on.

        Once SAP is known to be down, remaining SAP-backed sections are skipped
        rather than retried — four consecutive connection timeouts is how a
        one-minute refresh turns into a four-minute one.
        """
        if needs_sap and self._sap_down:
            self._degraded.append(name)
            return None
        try:
            return build()
        except (SAPConnectionError, SAPDataError) as exc:
            logger.warning("admin_board: %s unreadable for %s: %s", name, self.company_code, exc)
            if isinstance(exc, SAPConnectionError):
                self._sap_down = True
            self._degraded.append(name)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.exception("admin_board: %s failed for %s", name, self.company_code)
            self._degraded.append(name)
            return None

    # ------------------------------------------------------------------
    # The board
    # ------------------------------------------------------------------

    def build(self) -> Dict[str, Any]:
        """The whole screen. Never raises for a single tile's failure."""
        production = self._section("production", self._production)
        # Postgres for the headline, so it survives a HANA outage; the
        # invoiced comparison inside it guards itself separately.
        dispatch = self._section("dispatch", self._dispatch, needs_sap=False)
        fg = self._section("fg_storage", self._fg_storage)
        pm = self._section("pm_storage", self._pm_storage)
        oil = self._section("oil_storage", self._oil_storage)
        # Postgres, not SAP — it survives anything HANA does, which is half the
        # point of keeping the cost tile on this board.
        cost = self._section("cost", self._cost, needs_sap=False)

        board = {
            "output": {"production": production, "dispatch": dispatch},
            "storage": {"fg": fg, "pm": pm, "oil": oil},
            "cost": cost,
            "meta": {
                "company_code": self.company_code,
                "date": self.today.isoformat(),
                "period": {
                    "from": self.month_first.isoformat(),
                    "to": self.today.isoformat(),
                    "day_of_month": self.today.day,
                    "days_in_month": self.days_in_month,
                    "elapsed_pct": round(self.today.day / self.days_in_month * 100, 1),
                },
                "refresh_seconds": REFRESH_SECONDS,
                "generated_at": timezone.now().isoformat(),
                "degraded": self._degraded,
                "warnings": self._warnings,
                "tonnage_basis": (
                    f"{LITRES_PER_TON} litres = 1 ton, applied to oil and finished "
                    "goods only. Packing material carries no litre volume in SAP "
                    "and is reported at value."
                ),
            },
        }
        # Last, because every rule reads the tiles above it. A tile that failed
        # simply produces no alerts rather than a false all-clear — see
        # `build_alerts`, which is explicit about that.
        board["alerts"] = build_alerts(board, today=self.today)
        return board

    # ------------------------------------------------------------------
    # Tile 1 — output
    # ------------------------------------------------------------------

    def _production(self) -> Dict[str, Any]:
        """What the plant made this month, against what it said it would.

        READ FROM THE PLAN ITSELF, the way the Plant board reads it — not from
        two independent queries. ``PlanService.get_plan(include_actuals=True)``
        returns planned and produced against the SAME plan lines, so the
        comparison is apples to apples by construction. The first cut of this
        tile summed ``OINM`` receipts on one side and ``FCT1`` quantities on the
        other, which can disagree with the plan's own actuals for reasons
        neither figure exposes.

        Litres to tonnes at the business's fixed rule, matching the Plant board.
        Finished goods DO have a recorded case weight, so the gross-weight basis
        used by the storage tiles could in principle apply here too — but the
        plan is the authority for production on this site and it speaks in
        pieces and litres, so converting would mean weighing the plan by a
        second method and calling the difference an error.

        The average divides by days that ACTUALLY PRODUCED, per the business's
        definition — so a month with a shut Sunday reads as typical output on a
        working day rather than being dragged down by the calendar.
        """
        plan_header = self._resolve_plan()
        if not plan_header or not plan_header.get("abs_id"):
            self._warnings.append(
                "No production plan is filed in SAP for this month, so output is "
                "reported without a target."
            )
            return self._production_without_plan()

        detail = self._plan_service().get_plan(plan_header["abs_id"], include_actuals=True)
        # The totals live under "plan", NOT under a "totals" key. Reading the
        # wrong one returns an empty dict and every figure below degrades to
        # zero while the tile still renders confidently -- which is exactly what
        # it did on the first attempt.
        totals = detail.get("plan") or {}
        lines = detail.get("lines") or []

        mtd_tons = _tons(totals.get("produced_litres"))
        plan_tons = _tons(totals.get("planned_litres")) or None
        mtd_pieces = _f(totals.get("produced_qty"))
        # SAP's own attainment, served by the plan service, rather than
        # recomputed here. It is the settled definition and the Plant board
        # quotes the same one; deriving a second would let two screens disagree
        # about a percentage they both call "of plan".
        attainment = _f_or_none(totals.get("attainment_pct"))

        # Lines the tonnage cannot speak for. A litre volume exists in SAP only
        # where `U_IsLitre = 'Y'`, so an item without it is produced in pieces
        # and contributes nothing to the ton figure. Counted rather than hidden.
        unweighed_lines = sum(
            1
            for line in lines
            if _f(line.get("produced_qty")) > 0 and not _f(line.get("produced_litres"))
        )

        # The daily shape still comes from the movement ledger: the plan carries
        # totals, not a day-by-day actual, and "which days produced" is the
        # denominator of the average.
        #
        # NOTE THE BASIS SEAM. The ledger counts everything received onto the
        # floor; the plan counts only what was planned. So today's figure and
        # the trend can exceed what the month total implies, and that is not an
        # error -- it is unplanned output, which the plan is entitled to ignore
        # and the floor is not. `basis` below says so on the tile.
        by_day = self._daily_production()
        producing_days = len([day for day, entry in by_day.items() if entry["tons"] > 0])
        today_tons = by_day.get(self.today, {}).get("tons", 0.0)

        plan_to_date = (
            round(plan_tons * self.today.day / self.days_in_month, 2) if plan_tons else None
        )
        remaining_days = max(self.days_in_month - self.today.day, 0)
        required_rate = (
            round(max(plan_tons - mtd_tons, 0) / remaining_days, 2)
            if plan_tons and remaining_days > 0
            else None
        )

        return {
            "mtd_tons": mtd_tons,
            "mtd_pieces": round(mtd_pieces),
            "today_tons": today_tons,
            "producing_days": producing_days,
            "avg_tons_per_producing_day": (
                round(mtd_tons / producing_days, 2) if producing_days else None
            ),
            "plan_tons": plan_tons,
            "plan_to_date_tons": plan_to_date,
            "plan_pct": attainment if attainment is not None else _pct(mtd_tons, plan_tons),
            "required_tons_per_day": required_rate,
            "remaining_days": remaining_days,
            "plan_name": plan_header.get("name"),
            "warehouse": PRODUCTION_FLOOR,
            "unweighed_lines": unweighed_lines,
            "basis": (
                "SAP production plan actuals, litres at 1,000 L = 1 T — the same "
                "source and basis as the Plant board. Counts output against "
                "PLANNED items only, so it reads below the floor's total "
                "receipts, which include items nobody planned."
            ),
            "trend": self._trend(by_day),
        }

    def _resolve_plan(self) -> Optional[Dict[str, Any]]:
        """The plan this month reports against.

        The one whose dates contain today; failing that the newest, with a
        warning. A board silently reporting last month's plan as though it were
        this month's is the worst of the available outcomes.
        """
        plans = (self._plan_service().list_plans(limit=24) or {}).get("data") or []
        if not plans:
            return None
        current = next((row for row in plans if row.get("is_current")), None)
        if current is None:
            current = plans[0]
            self._warnings.append(
                "No SAP plan covers today; showing the most recent plan "
                f"({current.get('name') or current.get('code')})."
            )
        return current

    def _daily_production(self) -> Dict[date, Dict[str, float]]:
        """Output per day from the movement ledger, keyed by real dates."""
        rows = self._reader(self.company_code).production(self.month_first, self.today)["days"]
        by_day: Dict[date, Dict[str, float]] = {}
        for row in rows:
            day = _as_date(row.get("Day"))
            if day is not None:
                by_day[day] = {
                    "tons": _tons(row.get("Litres")),
                    "pieces": _f(row.get("Pieces")),
                }
        return by_day

    def _production_without_plan(self) -> Dict[str, Any]:
        """Output alone, when SAP holds no plan to measure it against."""
        by_day = self._daily_production()
        mtd_tons = round(sum(entry["tons"] for entry in by_day.values()), 2)
        producing_days = len([d for d, e in by_day.items() if e["tons"] > 0])
        return {
            "mtd_tons": mtd_tons,
            "mtd_pieces": round(sum(entry["pieces"] for entry in by_day.values())),
            "today_tons": by_day.get(self.today, {}).get("tons", 0.0),
            "producing_days": producing_days,
            "avg_tons_per_producing_day": (
                round(mtd_tons / producing_days, 2) if producing_days else None
            ),
            "plan_tons": None,
            "plan_to_date_tons": None,
            "plan_pct": None,
            "required_tons_per_day": None,
            "remaining_days": max(self.days_in_month - self.today.day, 0),
            "plan_name": None,
            "warehouse": PRODUCTION_FLOOR,
            "unweighed_lines": 0,
            "basis": "Production receipts onto the finished floor, litres at 1,000 L = 1 T.",
            "trend": self._trend(by_day),
        }

    def _trend(self, by_day: Dict[date, Dict[str, float]]) -> List[Dict[str, Any]]:
        """The last few days, oldest first, INCLUDING the ones that made nothing.

        A day of no production has to appear as an empty column. Dropping it
        would close the gap and draw a shut Sunday as if it never happened,
        which is exactly the shape a reader uses this strip to spot.
        """
        start = self.today - timedelta(days=TREND_DAYS - 1)
        days = []
        for offset in range(TREND_DAYS):
            day = start + timedelta(days=offset)
            days.append(
                {
                    "date": day.isoformat(),
                    "label": day.strftime("%d"),
                    "tons": by_day.get(day, {}).get("tons", 0.0),
                }
            )
        return days

    def _dispatch(self) -> Dict[str, Any]:
        """What physically left the gate, month to date.

        THE GATE-OUT REGISTER, NOT SAP INVOICES — the same basis and the same
        table as the Logistics board (``SalesDispatchGateOut`` via
        ``DispatchDashboardService``), so the two screens cannot report
        different tonnages for a word they both spell "dispatch".

        WHY THIS BASIS AND NOT THE INVOICE ONE
        --------------------------------------
        This tile sits beside Total Production, which counts a physical event in
        the month. Pairing "what we made" with "what we billed" compares a
        physical fact against a financial one, and the difference between them
        then reads as performance when it is really billing timing. Measured on
        15 Sep 2026 the two differ by more than they agree:

          * billed in Sep AND shipped in Sep   615.1 t   (the only overlap)
          * billed in Aug,  shipped in Sep     821.8 t   (in the gate figure only)
          * billed in Sep,  NOT yet shipped    325.1 t   (in the invoice figure only)

        So 57% of what went out the gate in the first half of September was
        August's billing finally moving. Both figures are kept below —
        ``mtd_tons`` is what left, ``invoiced_tons`` is what was billed — because
        the gap between them is the useful number, not a discrepancy to hide.

        WHAT IS *NOT* IN HERE, AND WHY THAT IS RIGHT
        --------------------------------------------
        The Oil to Jivo Mart leg. Those movements live in the **BST shifting
        register**, an entirely different table this one never touches — 101
        transfers to ``CUSTA000606`` in the first half of September, none of them
        in the gate-out rows. So no intercompany filter is needed here to keep
        the figure honest; the exclusion below removes nothing today and exists
        only so that a future change routing group transfers through this
        register cannot silently inflate the tile.

        **Per-company intercompany lists, never a combined one.** ``CUSTA000906``
        is a group company to Oil and a real customer to Mart, so a merged list
        wrongly strips 64 t of genuine Mart sales. Same shape as the item-code
        divergence between the two schemas.
        """
        from dispatch_plans.dashboard_service import DispatchDashboardService
        from gate_core.models.sales_dispatch import (
            SalesDispatchGateOut,
            SalesDispatchGateOutStatus,
        )
        from django.db.models import Count, Sum

        company_ids: Dict[int, str] = {}
        for code in DISPATCH_COMPANIES:
            company_id = Company.objects.filter(code=code).values_list("id", flat=True).first()
            if company_id is None:
                self._warnings.append(
                    f"{code} is not configured on this deployment, so dispatch is "
                    "the remaining companies only."
                )
                continue
            company_ids[company_id] = code

        if not company_ids:
            raise ValueError("no dispatch company is configured")

        rows = SalesDispatchGateOut.objects.filter(
            company_id__in=list(company_ids),
            gate_out_date__range=(self.month_first, self.today),
            status=SalesDispatchGateOutStatus.DISPATCHED,
        )
        # The guard described above. Applied per company, so a code that is a
        # group entity to one and a customer to the other is judged correctly.
        for company_id, code in company_ids.items():
            excluded = INTERCOMPANY_CARD_CODES.get(code, [])
            if excluded:
                rows = rows.exclude(company_id=company_id, customer_code__in=excluded)

        companies = []
        for company_id, code in company_ids.items():
            agg = rows.filter(company_id=company_id).aggregate(
                weight=Sum("total_weight"),
                trucks=Count("id"),
                bills=Count("dispatch_plan_id", distinct=True),
            )
            companies.append(
                {
                    "company_code": code,
                    "tons": round(_f(agg["weight"]) / 1000, 2),
                    "trucks": agg["trucks"] or 0,
                    "bills": agg["bills"] or 0,
                }
            )

        total = rows.aggregate(
            weight=Sum("total_weight"),
            trucks=Count("id"),
            # A truck carrying four invoices is four bills but one truck, so the
            # bill count is distinct on the plan and never a sum of trucks.
            bills=Count("dispatch_plan_id", distinct=True),
        )
        today_weight = rows.filter(gate_out_date=self.today).aggregate(
            weight=Sum("total_weight")
        )["weight"]

        mtd_tons = round(_f(total["weight"]) / 1000, 2)
        # Days on which a truck actually left. Zero-dispatch days are excluded so
        # the average reads as a typical working day, matching the production
        # tile's "producing days" rule.
        dispatch_days = (
            rows.values("gate_out_date").distinct().count()
        )

        return {
            "mtd_tons": mtd_tons,
            "today_tons": round(_f(today_weight) / 1000, 2),
            "trucks": total["trucks"] or 0,
            "bills": total["bills"] or 0,
            "dispatch_days": dispatch_days,
            "avg_tons_per_dispatch_day": (
                round(mtd_tons / dispatch_days, 2) if dispatch_days else None
            ),
            "companies": companies,
            # The other half of the story, kept rather than discarded.
            "invoiced_tons": self._invoiced_tons(),
            "basis": (
                "Trucks that left the gate this month, weighed in kg — the same "
                "register and basis as the Logistics board. Includes bills raised "
                "in an earlier month, and excludes this month's bills that have "
                "not shipped yet."
            ),
        }

    def _invoiced_tons(self) -> Optional[float]:
        """What was BILLED this month, for the comparison line.

        Deliberately still the SAP invoice read, intercompany excluded, because
        it answers the other question — and the gap between it and the gate
        figure is the tile's most useful number. Returns None rather than zero if
        SAP cannot be reached: the headline does not depend on it, so a failure
        here must cost the comparison line and nothing else.
        """
        try:
            total = 0.0
            for code in DISPATCH_COMPANIES:
                if not Company.objects.filter(code=code).exists():
                    continue
                days = self._reader(code).dispatch(
                    self.month_first,
                    self.today,
                    INTERCOMPANY_CARD_CODES.get(code, []),
                )["days"]
                total += sum(_tons(row.get("Litres")) for row in days)
            return round(total, 2)
        except (SAPConnectionError, SAPDataError):
            return None

    def _month_end(self) -> date:
        return self.month_first.replace(day=self.days_in_month)

    # ------------------------------------------------------------------
    # Tile 2 — storage
    # ------------------------------------------------------------------

    def _capacity(self, company_code: str, warehouse: str) -> Optional[float]:
        """The rated tonnage somebody typed in, or None.

        Read from ``stock_dashboard.WarehouseBoardSettings`` rather than held
        here: SAP records no capacity anywhere, and the figure is already
        edited by two existing settings pages. A board with its own private
        copy is a board that disagrees with them.
        """
        row = (
            WarehouseBoardSettings.objects.filter(
                company_code=company_code, warehouse=warehouse
            )
            .values("capacity_tonnes", "last_audit_date")
            .first()
        )
        if not row:
            return None
        return _f_or_none(row["capacity_tonnes"])

    def _last_audit(self, company_code: str, warehouse: str) -> Optional[str]:
        row = (
            WarehouseBoardSettings.objects.filter(
                company_code=company_code, warehouse=warehouse
            )
            .values_list("last_audit_date", flat=True)
            .first()
        )
        return row.isoformat() if row else None

    def _fg_storage(self) -> Dict[str, Any]:
        """Finished goods on site: BH-BT and the Gupta godown.

        WEIGHED THE WAY THE LOGISTICS BOARD WEIGHS THEM, and scoped the way it
        scopes them. Both halves matter and both were wrong in the first cut of
        this tile — see ``tonnage.py`` for the full account. In short: real
        recorded gross case weight rather than litres at density 1.0, and item
        group 102 only rather than everything standing in the building.

        Spans two SAP companies — BH-BT is Oil's, Gupta is Mart's — because the
        question "how much finished goods is standing here" does not respect the
        legal entity. Each row names its company so the split stays visible.

        Unrated warehouses holding stock are reported ALONGSIDE the total and
        never inside it. See ``constants`` for why GP-FG is not quietly folded
        into Gupta.
        """
        rows = []
        total_tons = 0.0
        total_capacity = 0.0
        capacity_known = True
        unweighed = 0
        non_piece = 0

        for store in FG_STORES:
            occupancy = self._occupancy(store["company"], store["warehouse"])
            weighed = roll_up(occupancy)
            tons = weighed["tonnes"]
            unweighed += weighed["unweighed_items"]
            non_piece += weighed["non_piece_items"]

            capacity = self._capacity(store["company"], store["warehouse"])
            if capacity is None:
                capacity_known = False
            else:
                total_capacity += capacity
            total_tons += tons
            rows.append(
                {
                    "warehouse": store["warehouse"],
                    "label": store["label"],
                    "company_code": store["company"],
                    "tons": tons,
                    "capacity_tons": capacity,
                    "used_pct": _pct(tons, capacity),
                    "free_tons": round(capacity - tons, 2) if capacity else None,
                    "last_audit_date": self._last_audit(store["company"], store["warehouse"]),
                    "unweighed_items": weighed["unweighed_items"],
                }
            )

        unrated = []
        for store in UNRATED_FG_WAREHOUSES:
            weighed = roll_up(self._occupancy(store["company"], store["warehouse"]))
            if weighed["tonnes"] > 0:
                unrated.append(
                    {
                        "warehouse": store["warehouse"],
                        "label": store["label"],
                        "company_code": store["company"],
                        "tons": weighed["tonnes"],
                    }
                )

        return {
            "unit": "tonnes",
            "total_tons": round(total_tons, 2),
            "capacity_tons": round(total_capacity, 2) if capacity_known else None,
            "used_pct": _pct(total_tons, total_capacity) if capacity_known else None,
            "free_tons": (
                round(total_capacity - total_tons, 2) if capacity_known else None
            ),
            "rows": rows,
            "unrated": unrated,
            # A tonnage is only as complete as the item master behind it. A
            # confident total over a half-weighed warehouse is the failure mode
            # here, and it looks identical to a correct one.
            "unweighed_items": unweighed,
            "non_piece_items": non_piece,
            "basis": (
                "Gross case weight (OITM.U_Gross_Weight / SalFactor2), finished "
                "goods only — the same basis and scope as the Logistics board."
            ),
        }

    def _pm_storage(self) -> Dict[str, Any]:
        """Packaging material: what it is worth, and how much floor it stands on.

        VALUE, NOT TONNES. ``U_IsLitre`` is 'N' on every packaging item so it has
        no litre volume, and ``U_Gross_Weight`` is a finished-goods field —
        neither of this board's two tonnage bases applies. Value is the only
        figure true for a preform and a carton alike.

        SPACE IS MEASURED IN SQUARE FEET, VIA THE PLANT BOARD'S OWN BRIDGE.
        SAP cannot connect stock to floor — across all 878 packaging items it
        holds no volume and no dimensions — so the factory bridges it in two
        measured steps, and this reuses both rather than inventing a third:

        1. **Pieces per pallet, PER ITEM**, from the factory's stacking sheet
           (``plant_board/data/stacking.json``). Per item is the whole point:
           300 five-litre bottles fill a pallet and 60,000 caps fill one, so a
           blended rate would be dominated by whichever item is numerous — which
           here is caps.
        2. **Square feet under one pallet**, from the Plant board's settings
           (15 sq ft as measured on site).

        WHAT IT CANNOT MEASURE, IT NAMES. An item with no pallet figure is
        counted in the pieces and the value but left out of the floor, and the
        count rides on the response — every one of them makes the stores read
        EMPTIER than they are, which is the one direction this tile must not
        fail in silently.

        If the pallet footprint has been cleared, both halves are still reported
        in their own real units and the percentage reads as a rule. An invented
        percentage on a wall is worse than an honest gap: somebody would plan a
        building against it.
        """
        from plant_board.constants import (
            DEFAULT_SQFT_PER_PALLET,
            PACKAGING_FLOOR_BLOCKS,
            PACKAGING_FLOOR_SQFT,
        )
        from plant_board.stacking import measured_on, pieces_per_pallet

        per_pallet = pieces_per_pallet()

        rows = []
        total_value = 0.0
        total_pieces = 0.0
        total_pallets = 0.0
        unmeasured_codes: set = set()
        unmeasured_pieces = 0.0

        for warehouse in PM_STORES:
            occupancy = self._occupancy(self.company_code, warehouse, finished_only=False)
            value = 0.0
            pieces = 0.0
            pallets = 0.0
            for row in occupancy:
                on_hand = float(row.get("on_hand") or 0)
                value += float(row.get("stock_value") or 0)
                pieces += on_hand
                factor = per_pallet.get((row.get("item_code") or "").strip())
                if factor:
                    pallets += on_hand / factor
                else:
                    unmeasured_codes.add(row.get("item_code"))
                    unmeasured_pieces += on_hand

            total_value += value
            total_pieces += pieces
            total_pallets += pallets
            rows.append(
                {
                    "warehouse": warehouse,
                    "label": warehouse,
                    "value": round(value, 2),
                    "pieces": round(pieces),
                    "pallets": round(pallets, 1),
                }
            )

        rows.sort(key=lambda entry: entry["value"], reverse=True)

        sqft_per_pallet = self._sqft_per_pallet(DEFAULT_SQFT_PER_PALLET)
        occupied_sqft = free_sqft = used_pct = None
        if sqft_per_pallet:
            occupied_sqft = round(total_pallets * sqft_per_pallet, 1)
            # Floored at zero: a store packed past its measured area is a real
            # thing, and negative free space is arithmetic nobody can act on.
            # The percentage is left UNCAPPED so an overfill is still visible.
            free_sqft = round(max(0.0, PACKAGING_FLOOR_SQFT - occupied_sqft), 1)
            used_pct = round(occupied_sqft / PACKAGING_FLOOR_SQFT * 100, 1)

        if unmeasured_codes:
            self._warnings.append(
                f"{len(unmeasured_codes)} packaging items have no pallet figure on the "
                "stacking sheet, so the floor used is understated."
            )

        return {
            "unit": "value",
            "total_value": round(total_value, 2),
            "total_pieces": round(total_pieces),
            "pallets": round(total_pallets, 1),
            # Space, in the unit the business actually rates these stores in.
            "floor_sqft": PACKAGING_FLOOR_SQFT,
            "occupied_sqft": occupied_sqft,
            "free_sqft": free_sqft,
            "used_pct": used_pct,
            "sqft_per_pallet": sqft_per_pallet,
            "blocks": [
                {"label": b["label"], "sqft": b["sqft"]} for b in PACKAGING_FLOOR_BLOCKS
            ],
            # Every one of these makes the stores read emptier than they are.
            "unmeasured_items": len(unmeasured_codes),
            "unmeasured_pieces": round(unmeasured_pieces),
            "stacking_measured_on": measured_on(),
            "capacity_tons": None,
            "no_capacity_reason": (
                "Rated in square feet, not tonnes — packaging has no litre volume "
                "and no case weight."
            ),
            "basis": (
                "Stock value, with floor use bridged pieces → pallets → square feet "
                "by the factory's own stacking sheet — the Plant board's method."
            ),
            "rows": rows,
        }

    def _sqft_per_pallet(self, default: float) -> Optional[float]:
        """Square feet under one pallet, from the Plant board's settings.

        Shared with that board rather than copied, so a figure re-measured on
        its settings page moves both screens at once.

        An explicit null means somebody CLEARED it: they are saying the figure is
        not known here, which is different from never having been asked, so it is
        honoured rather than defaulted over. A missing table is tolerated too —
        that app's settings arrive by migration, and a board that died before it
        ran would take out a tile that has nothing to do with the setting.
        """
        try:
            row = PlantBoardSettings.objects.filter(
                company_code=self.company_code
            ).first()
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("admin_board: space settings unreadable: %s", exc)
            return default
        if row is None:
            return default
        return float(row.sqft_per_pallet) if row.sqft_per_pallet is not None else None

    def _oil_storage(self) -> Dict[str, Any]:
        """Loose oil in the tank farm.

        THE ONE TILE THAT IS STILL LITRES, AND CORRECTLY SO. The gross-weight
        basis used for finished goods cannot apply here: the tanks are stocked
        in LTR, which is not a piece unit, so ``U_Gross_Weight`` — a weight per
        sales CASE — has nothing to divide into. Asking the finished-goods
        roll-up to weigh this warehouse returns zero tonnes for a full tank
        farm, which is exactly the trap ``is_piece_uom`` exists to prevent.

        Here the on-hand figure IS the volume, so the business's 1,000 L = 1 T
        rule applies directly with no pack factor and no case weight involved.
        The tile says so on its face rather than leaving a reader to assume the
        number beside it was weighed the same way.

        Scoped to every item group, not just finished goods: this warehouse
        holds raw oil, which is not group 102 at all.
        """
        occupancy = self._occupancy(self.company_code, OIL_TANK, finished_only=False)
        volume = sum_litres(occupancy)
        capacity = self._capacity(self.company_code, OIL_TANK)

        by_item = sorted(
            (
                {
                    "label": row.get("item_name") or row.get("item_code") or "",
                    "tons": _tons(
                        float(row.get("on_hand") or 0)
                        * float(row.get("litres_per_piece") or 1)
                        if row.get("uom") and row.get("litres_per_piece")
                        else float(row.get("on_hand") or 0)
                    ),
                }
                for row in occupancy
            ),
            key=lambda entry: entry["tons"],
            reverse=True,
        )

        return {
            "unit": "tonnes",
            "warehouse": OIL_TANK,
            "total_tons": _tons(volume),
            "total_litres": round(volume),
            "capacity_tons": capacity,
            "used_pct": _pct(_tons(volume), capacity),
            "no_capacity_reason": "The tanks carry no rated capacity in any system.",
            "basis": (
                "Litres at 1,000 L = 1 T. The tanks are stocked by volume, so no "
                "case weight applies — unlike the finished-goods tiles."
            ),
            "rows": by_item[:4],
        }

    # ------------------------------------------------------------------
    # Tile 3 — cost
    # ------------------------------------------------------------------

    def _cost(self) -> Dict[str, Any]:
        """The month's spend, as four slices.

        Reuses the factory expense wall board wholesale rather than re-deriving
        anything: the rates, the labour double-count rule and the electricity
        mains decision all live there, and a second implementation would drift.

        A slice with no source keeps its warning and reports zero explicitly —
        ``has_source`` is what lets the screen draw it as an empty legend row
        rather than as a zero-width arc nobody can see.
        """
        from factory_expense import services as expense_services

        build = self._cost_board or expense_services.build_board
        companies = list(Company.objects.all())
        board = build(companies=companies, date_from=self.month_first, date_to=self.today)

        buckets = board.get("buckets") or {}
        labour = self._labour_gate()
        details = {
            "labour": labour["detail"],
            "electricity": self._electricity_detail(board),
        }
        # The wall board's labour warning is dropped in favour of this tile's,
        # which is stated over the rows this tile prices.
        warnings = [
            entry
            for entry in (board.get("warnings") or [])
            if labour["bucket_warning_prefix"] not in entry
        ]
        if labour["warning"]:
            warnings.append(labour["warning"])

        slices = []
        total = 0.0
        for spec in COST_SLICES:
            bucket = buckets.get(spec["bucket"]) or {}
            amount = _f(bucket.get("mtd"))
            warning = bucket.get("warning")
            detail = details.get(spec["key"]) or {}
            basis = None
            if spec["key"] == "labour":
                # NOT the wall board's figure. See _labour_gate: the wall board
                # prices the HOD's departmental split on top of the gate count
                # it re-describes, so its labour line is ~1.7x the people who
                # were actually here. This tile shows the head count, and a
                # count that does not divide into the money beside it is worse
                # than no count at all.
                amount = labour["cost"]
                warning = labour["warning"]
                basis = labour["note"] if labour["double_counted"] else None
            slices.append(
                {
                    "key": spec["key"],
                    "label": spec["label"],
                    "bucket": spec["bucket"],
                    "amount": round(amount, 2),
                    # Zero WITH a warning is "nobody configured this"; zero
                    # without one is a real nil. The screen renders them
                    # differently and the alerts only fire on the first.
                    "has_source": amount > 0 or not warning,
                    "warning": warning,
                    # What the money is made of, in the line's own unit. Rupees
                    # alone cannot be sanity-checked by anyone standing at the
                    # board; "1,045 gated in" and "13 meters" can.
                    "detail": detail.get("text"),
                    "detail_value": detail.get("value"),
                    # Why this line differs from the same line on another
                    # board. Shown where the line is, not in the alert list:
                    # it is an explanation, not something anybody must act on.
                    "basis": basis,
                }
            )
            total += amount

        for entry in slices:
            entry["share_pct"] = _pct(entry["amount"], total)

        return {
            "currency": "INR",
            "total": round(total, 2),
            "slices": slices,
            "warnings": warnings,
            # The electricity line reads every meter on the site, and the site's
            # mains are meters too. Said on the tile because the figure is
            # ~3x the metered bill by design — see the Company Expense board.
            "electricity_note": self._electricity_note(),
        }

    def _labour_gate(self) -> Dict[str, Any]:
        """Month-to-date gate labour: how many came in, and what they cost.

        **Only rows with no department are the gate count.** The register keeps
        the barrier tally and the HOD's departmental split in rows of identical
        shape; a row WITH a department re-describes people a row WITHOUT one has
        already counted. Summing both double-counts every allocated labourer —
        1,683 man-days against 1,045 real ones on the live register for 1-15
        Sep, and ₹7.65 L against ₹4.39 L. This is the rule
        ``useGateBoard.splitLabourDay`` applies on the gate wall and
        ``factory_expense.matrix.labour_by_company`` applies on the matrix.

        The expense WALL board still sums both, so **this tile's labour figure
        is deliberately lower than the Factory Expense board's for the same
        span**, and the gap is named in the tile's warnings rather than left for
        somebody to discover by comparing two screens. Pricing reuses the wall
        board's own ``resolve``/``_price_labour`` primitives, so a Cost Master
        change still lands here and only the row selection differs.
        """
        from factory_expense.constants import LABOUR_COST_TYPE_CODE
        from factory_expense.rates import load_rates_by_company, resolve
        from factory_expense.services import _price_labour, get_settings
        from labour_gate.models import LabourGateEntry

        companies = list(Company.objects.all())
        entries = list(
            LabourGateEntry.objects.filter(
                company__in=companies,
                work_date__gte=self.month_first,
                work_date__lte=self.today,
                is_active=True,
            )
        )
        # The cost type is configurable on the wall board and this tile must
        # follow it, or a site that repointed its labour rate would see two
        # different labour figures for the same month.
        code = (
            get_settings(companies[0]).labour_cost_type_code
            if companies
            else None
        ) or LABOUR_COST_TYPE_CODE
        rates = load_rates_by_company(code, companies, self.today)

        heads = 0
        allocated = 0
        cost = Decimal("0")
        unpriced = 0
        days = set()
        flat_charged = set()
        for entry in entries:
            count = entry.count_in or 0
            if entry.department_id is not None:
                allocated += count
                continue
            heads += count
            if count:
                days.add(entry.work_date)
            rate = resolve(rates.get(entry.company_id, []), None, entry.work_date)
            if rate is None:
                unpriced += count
                continue
            charge = _price_labour(rate, count)
            if rate.basis == "PER_DAY":
                # A flat daily charge lands once per company per day, however
                # many contractors walked through — same rule as the wall board.
                key = (entry.work_date, entry.company_id)
                if key in flat_charged:
                    charge = Decimal("0")
                else:
                    flat_charged.add(key)
            cost += charge

        if not heads:
            detail = {"value": 0, "text": "nobody through the gate this month"}
        else:
            day_count = len(days)
            text = f"{heads:,} gated in over {day_count} day{'s' if day_count != 1 else ''}"
            if unpriced:
                text += f" · {unpriced:,} unpriced"
            detail = {"value": heads, "text": text}

        # The wall board's own labour warning counts the allocation rows too,
        # so it names more unpriced people than this tile has. Restated over the
        # rows this tile actually prices, or the two numbers argue.
        warning = None
        if unpriced:
            warning = (
                f"{unpriced:,} of the {heads:,} labourers at the gate have no "
                f"rate in this period — set '{code}' in Admin › Cost Master, "
                "effective from the start of the month."
            )

        return {
            "cost": round(float(cost), 2),
            "heads": heads,
            "days": len(days),
            "unpriced": unpriced,
            "detail": detail,
            "warning": warning,
            "bucket_warning_prefix": "labourers have no rate",
            "double_counted": allocated > 0,
            "note": (
                f"Labour is the {heads:,} people the gate counted. The Factory "
                f"Expense board also prices {allocated:,} departmental "
                "allocation rows, which describe those same people again, so it "
                "reads higher for this month."
            ),
        }

    def _electricity_detail(self, board: Dict[str, Any]) -> Dict[str, Any]:
        """Every meter on the site, and the units they recorded.

        The wall board is already asked for the whole month and for every
        company, so its ``meters`` rows ARE all meters — no meter on the live
        master is untagged, so the ``electricity_only_company_meters`` filter
        excludes none of them. Counting the rows here rather than the meter
        master is deliberate: a meter nobody has read this month contributes no
        rupees and must not be claimed as measured.
        """
        meters = list(board.get("meters") or [])
        units = sum(_f(row.get("units")) for row in meters)
        if not meters:
            return {"value": 0, "text": "no meter read this month"}
        return {
            "value": len(meters),
            "text": (
                f"all {len(meters)} meters · {units:,.0f} units"
                if units
                else f"all {len(meters)} meters"
            ),
        }

    def _electricity_note(self) -> str:
        """Why the electricity figure is bigger than the electricity bill."""
        return (
            "Every meter, mains included — the sub-meters measure the same "
            "supply again, so this runs about 3x the metered bill."
        )
