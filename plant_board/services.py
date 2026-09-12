"""
plant_board/services.py

The plant control board, composed into one response.

ONE ENDPOINT, NOT TWENTY
------------------------
This is a wall board on a factory TV: full screen, no clicks, re-reading on a
timer. Every tile re-runs on every refresh, which makes SAP query cost a
first-class design concern rather than an afterthought. Twenty independent
panel requests a minute is how a board ends up being switched off. So the whole
screen is one composed read.

EVERY BAND FAILS ON ITS OWN
---------------------------
There is nobody standing at a TV to press retry, so a band that cannot be read
must not take the screen with it. Each one is built inside ``_section``, which
catches its own failure, records the band in ``meta.degraded`` and returns
``None`` for it. The front end then renders that band's tiles with the reason on
their face. A blank band with an explanation is useful; a blank board is not.

This also covers permissions. The board accepts any one of four rights (see
``permissions.py``) while the screen itself is all-or-nothing, so a login
holding only some of them degrades band by band instead of being refused.

WHAT IT DOES NOT COMPUTE
------------------------
Nine tiles are waiting on modules being built separately — the salary module and
the warehouse configuration page. They are declared in
``constants.PENDING_TILES`` and echoed back in ``meta.pending`` rather than
returned as zeros. A ₹0 with no explanation gets believed for a week and then
ignored forever, which is the failure mode the Factory Expense board already
solved by naming its missing cost type on screen.
"""

import calendar
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from django.db.models import Count, Q, Sum
from django.utils import timezone

from blowing.models import BlowingRun
from packing_material.constants import consumption_warehouses
from packing_material.services import PackingMaterialService
from planning_purchase.hana_reader import HanaProductionPlanReader
from planning_purchase.services.plan_service import PlanService
from production_execution.models import (
    ProductionMaterialUsage,
    ProductionRun,
    ProductionSegment,
    WasteLog,
)
from raw_material_gatein.models import POItemReceipt
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError
from stock_dashboard.models import (
    PlantBoardSettings,
    PlantBoardWorkforce,
    WarehouseBoardSettings,
)
from stock_dashboard.services import StockDashboardService
from warehouse.models_bst import BSTBoxScan, BSTReceiveStatus, BSTSourceType, BSTTransferStatus
from warehouse.models_pf_movement import PFStockMovement

from .constants import (
    DEFAULT_SQFT_PER_PALLET,
    PACKAGING_FLOOR_BLOCKS,
    PACKAGING_FLOOR_SQFT,
    WORKFORCE_BANDS,
    WORKFORCE_DEPARTMENTS,
    SHIFTING_DECLARED_ROUTES,
    SHIFTING_DISPATCH,
    SHIFTING_ELSEWHERE,
    SHIFTING_ROUTES,
    SHIFTING_ROUTE_NAMES,
    AGE_BUCKET_LOWER,
    TREND_DAYS,
    AGE_BUCKET_UPPER,
    BENCHMARK_ITEM_GROUP,
    BENCHMARK_MOVEMENT,
    BENCHMARK_STATUSES,
    LITRES_PER_TON,
    MAX_LISTED_ROWS,
    PENDING_TILES,
    PRODUCTION_FLOOR,
    REFRESH_SECONDS,
    STORE_WAREHOUSES,
)
from .hana_reader import PlantBoardReader
from .non_moving import non_moving_snapshot
from .stacking import measured_on, pieces_per_pallet
from .workforce import departments as workforce_departments

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


def _f(value) -> float:
    """A float the JSON encoder will accept, from anything numeric or null."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    return value.date() if hasattr(value, "date") else value


def _f_or_none(value) -> Optional[float]:
    """A float, or None. Distinct from ``_f``, which turns None into 0.0."""
    return None if value is None else float(value)


def _per_day(monthly: Optional[float], days: int) -> Optional[float]:
    """A month's wage bill as a daily run rate, or nothing at all."""
    if monthly is None or not days:
        return None
    return round(monthly / days, 2)


def _is_sap_unreachable(exc: Exception) -> bool:
    """Is this the connection failing, rather than a query being wrong?

    Matched on the exception TYPE where the app defines one, because four
    different services wrap HANA and each raises its own class. The message
    fallback exists for the ones that raise a bare ``Exception`` with the
    driver's text in it; it is deliberately narrow, since latching this flag on
    an ordinary query error would blank bands that had nothing wrong with them.
    """
    if isinstance(exc, SAPConnectionError):
        return True
    text = str(exc).lower()
    return "unable to connect to sap" in text or "connection failed" in text


def _tons(litres) -> float:
    """Litres to tons on the business's fixed rule — see ``constants``.

    1000 L = 1 t is a density of 1.0 where edible oil is about 0.91, so this
    reads roughly 10% above a weighbridge. That was accepted knowingly; it is
    named here so nobody has to go looking for where the conversion happened.
    """
    return round(_f(litres) / LITRES_PER_TON, 3)


class PlantBoardService:
    """Builds the whole board for one company.

    Readers are injectable so every band's arithmetic can be exercised against
    fixed rows, with no HANA connection and no live database.
    """

    def __init__(
        self,
        company_code: str,
        reader: Optional[PlantBoardReader] = None,
        stock: Optional[StockDashboardService] = None,
        plans: Optional[PlanService] = None,
        packing: Optional[PackingMaterialService] = None,
        plan_reader: Optional[HanaProductionPlanReader] = None,
        today: Optional[date] = None,
    ):
        self.company_code = company_code
        self.today = today or timezone.localdate()

        # The SAP context is built only if something actually needs it. A test
        # that injects every reader must not have to own a company's HANA
        # config just to exercise the arithmetic.
        self._context = None

        self.reader = reader if reader is not None else PlantBoardReader(self._sap())
        self.stock = stock if stock is not None else StockDashboardService(company_code)
        self.plans = plans if plans is not None else PlanService(company_code)
        self.packing = (
            packing if packing is not None else PackingMaterialService(company_code)
        )
        self.plan_reader = (
            plan_reader
            if plan_reader is not None
            else HanaProductionPlanReader(self._sap())
        )

        self._degraded: List[str] = []
        #: Set the first time a SAP read times out, and never cleared within a
        #: build. See `_section` for why a board that re-reads every minute
        #: cannot afford to find out twice.
        self._sap_down = False
        self._warnings: List[str] = []

    def _sap(self):
        """This company's SAP context, built once and only when needed."""
        if self._context is None:
            self._context = CompanyContext(self.company_code)
        return self._context

    # ------------------------------------------------------------------
    # The board
    # ------------------------------------------------------------------

    def build(self) -> Dict[str, Any]:
        """The whole screen. Never raises for a single band's failure."""
        plan = self._section("plan", self._resolve_plan)

        return {
            "purchase": self._section("purchase", lambda: self._purchase(plan)),
            "store": self._section("store", self._store),
            "production": self._section("production", lambda: self._production(plan)),
            # The one band that is not SAP's: its register is Postgres, and
            # only its tonnage comes from the item master.
            "shifting": self._section("shifting", self._shifting, needs_sap=False),
            # Typed on the settings page, so it survives anything SAP does.
            "workforce": self._section("workforce", self._workforce, needs_sap=False),
            "meta": {
                "company_code": self.company_code,
                "date": self.today.isoformat(),
                "plan": plan,
                "refresh_seconds": REFRESH_SECONDS,
                "generated_at": timezone.now().isoformat(),
                # Bands that could not be read at all, so the wall can say which
                # part of the screen is stale rather than showing a confident 0.
                "degraded": self._degraded,
                # Tiles nobody has the data for yet, and what each waits on.
                "pending": PENDING_TILES,
                "warnings": self._warnings,
                "tonnage_basis": (
                    f"{LITRES_PER_TON} litres = 1 ton, applied to oil and finished "
                    "goods only. Packing material carries no litre volume in SAP "
                    "and is shown in pieces."
                ),
            },
        }

    # ------------------------------------------------------------------
    # Who is working, per band
    # ------------------------------------------------------------------

    def _workforce(self) -> Dict[str, Any]:
        """Head count and wage bill for each band, from the settings page.

        NO SYSTEM HOLDS THIS. The two department masters the app runs on --
        ``accounts.Department`` and ``employee_hierarchy.Department`` -- are
        disjoint, and not one of the six departments the business reports on
        appears in either. Salary is withheld by the employee endpoint from any
        login without a salary grant, which a wall-board login is. So an
        operator types it, the same way the Logistics board's staffing is typed.

        MONTHLY IN, DAILY OUT. The wage bill is authored per month, because that
        is how it is paid. The board shows it per day so it sits beside the
        day's output on the same scale -- divided by the CALENDAR days in the
        month, not by working days, because a wage is paid for the Sunday too.

        UNSET IS NOT ZERO, AND THE DIFFERENCE IS THE POINT. A band whose
        departments have no configured figure reports null, which the strip
        draws as a rule. Reporting zero would say the band is unstaffed, and
        nobody would question it.
        """
        # Through the shared resolver, so a department added on the settings
        # page appears on the wall without a second list having to learn about
        # it. Built-ins first, then whatever the plant has grown.
        catalogue = workforce_departments(self.company_code)
        days_in_month = calendar.monthrange(self.today.year, self.today.month)[1]

        bands: Dict[str, Dict[str, Any]] = {
            band: {
                "employees": None,
                "employee_salary_monthly": None,
                "labour": None,
                "labour_salary_monthly": None,
                "departments": [],
            }
            for band in WORKFORCE_BANDS
        }
        unconfigured: List[str] = []

        for entry in catalogue:
            people = entry["employees"]
            salary = entry["salary_monthly"]
            if people is None and salary is None:
                unconfigured.append(entry["label"])

            slot = bands[entry["band"]]
            slot["departments"].append(
                {
                    "key": entry["key"],
                    "label": entry["label"],
                    "kind": entry["kind"],
                    "employees": people,
                    "salary_monthly": salary,
                }
            )
            # Summed only over what is configured, so one unfilled department
            # cannot turn its whole half into a zero.
            head_key = "employees" if entry["kind"] == "employee" else "labour"
            money_key = (
                "employee_salary_monthly"
                if entry["kind"] == "employee"
                else "labour_salary_monthly"
            )
            if people is not None:
                slot[head_key] = (slot[head_key] or 0) + people
            if salary is not None:
                slot[money_key] = round((slot[money_key] or 0.0) + salary, 2)

        for slot in bands.values():
            slot["employee_cost_per_day"] = _per_day(
                slot["employee_salary_monthly"], days_in_month
            )
            slot["labour_cost_per_day"] = _per_day(
                slot["labour_salary_monthly"], days_in_month
            )

        heads = [
            row["employees"]
            for slot in bands.values()
            for row in slot["departments"]
            if row["employees"] is not None
        ]
        money = [
            row["salary_monthly"]
            for slot in bands.values()
            for row in slot["departments"]
            if row["salary_monthly"] is not None
        ]

        return {
            "days_in_month": days_in_month,
            "bands": bands,
            "total_people": sum(heads) if heads else None,
            "total_salary_monthly": round(sum(money), 2) if money else None,
            "total_cost_per_day": _per_day(
                round(sum(money), 2) if money else None, days_in_month
            ),
            # Named, not silently absent: a total that quietly leaves two
            # departments out is worse than one that says which.
            "unconfigured": unconfigured,
            "basis": (
                "Typed on the board's settings page; no system holds it. The "
                "monthly wage bill is divided by the "
                f"{days_in_month} calendar days in this month."
            ),
        }

    def _section(self, name: str, build: Callable[[], Any], needs_sap: bool = True) -> Any:
        """Run one band, and let it fail without taking the board down.

        ONE OUTAGE COSTS ONE TIMEOUT, NOT ONE PER BAND. A HANA connect attempt
        blocks for fifteen seconds before it gives up. Six of those in a row —
        the plan, three bands, and the Shifting band's two litre lookups — is
        ninety seconds, which is past the client's own thirty-second limit, so
        the whole board came back as a failed request and the screen went blank.
        A wall board losing three bands is the design working; losing all four
        because the first three were slow to fail is not.

        So the first connection failure latches, and every later band that
        cannot run without SAP is skipped rather than retried. It is still
        reported as degraded, which is the truth: it could not be read. Bands
        that can stand without SAP pass ``needs_sap=False`` and still run.
        """
        if needs_sap and self._sap_down:
            logger.info("plant_board: %s skipped, SAP already timed out", name)
            self._degraded.append(name)
            return None
        try:
            return build()
        except Exception as exc:  # noqa: BLE001 - a wall board degrades, never 500s
            if _is_sap_unreachable(exc):
                self._sap_down = True
                self._warnings.append(
                    "SAP did not answer. The bands that read it are showing "
                    "nothing rather than a stale figure."
                )
            logger.warning("plant_board: %s could not be built: %s", name, exc)
            self._degraded.append(name)
            return None

    # ------------------------------------------------------------------
    # The plan, which is the window for two of the four bands
    # ------------------------------------------------------------------

    def _resolve_plan(self) -> Optional[Dict[str, Any]]:
        """The plan month the board reports against.

        The business fixed the window as the SAP plan month rather than the
        calendar month, so the plan header IS the period: its own start and end
        dates decide what "so far" means on every tile in the Purchase and
        Production bands.

        The current plan is the one whose dates contain today. Failing that the
        newest is used and a warning says so — a board silently reporting last
        month's plan as though it were this month's is the worst of the
        available outcomes.
        """
        plans = (self.plans.list_plans(limit=24) or {}).get("data") or []
        if not plans:
            return None

        current = next((row for row in plans if row.get("is_current")), None)
        if current is None:
            current = plans[0]
            self._warnings.append(
                "No SAP plan covers today; showing the most recent plan "
                f"({current.get('name') or current.get('code')})."
            )

        start = _as_date(current.get("start_date"))
        end = _as_date(current.get("end_date"))
        elapsed_end = min(self.today, end) if end else self.today

        return {
            "abs_id": current.get("abs_id"),
            "code": current.get("code") or "",
            "name": current.get("name") or "",
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "is_current": bool(current.get("is_current")),
            # Plan-to-date, which is what "purchased so far" is measured over.
            "days_total": (end - start).days + 1 if (start and end) else None,
            "days_elapsed": (elapsed_end - start).days + 1 if start else None,
        }

    def _plan_window(self, plan: Optional[Dict[str, Any]]):
        """Plan start through today, or the calendar month if there is no plan."""
        if plan and plan.get("start_date"):
            start = date.fromisoformat(plan["start_date"])
            end = date.fromisoformat(plan["end_date"]) if plan.get("end_date") else self.today
            return start, min(self.today, end)
        return self.today.replace(day=1), self.today

    # ------------------------------------------------------------------
    # Band 1: Purchase
    # ------------------------------------------------------------------

    def _purchase(self, plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The packing-material buyer's question, in five figures.

        The plan and the open-order position come from the requirement sheet,
        which already does the BOM explosion and already netted stock and open
        orders off it. Nothing here recomputes that arithmetic; the board would
        only end up disagreeing with the page it links to.

        "Purchased" is deliberately TWO figures. Open PO is what is on order;
        GRPO is what has physically arrived. The business rejected a single
        blended number, and rightly: on this company every open packing-material
        line was already past due on 9 September, so "on order" and "arriving"
        are not the same claim.
        """
        requirement = self.packing.get_requirement(plan.get("abs_id") if plan else None)
        totals = requirement.get("totals") or {}
        rows = requirement.get("data") or []

        benchmark = self._benchmark()
        window_from, window_to = self._plan_window(plan)
        orders = self.reader.pm_purchase_orders(window_from, window_to)

        return {
            # The plan, in pieces. Never tons: no packaging item is a litre item.
            "planning_qty": _f(totals.get("planning_qty")),
            "planning_unit": "PCS",
            # The same figures in rupees, priced per item off the item master.
            "planning_value": _f(totals.get("planning_value")),
            "issued_value": _f(totals.get("issued_value")),
            "on_hand_value": _f(totals.get("on_hand_value")),
            "open_po_value": _f(totals.get("open_po_value")),
            "unpriced_count": int(totals.get("unpriced_count") or 0),
            # Two different questions, and the module's own naming makes them
            # easy to confuse. What ARRIVED on the floor is not what the line
            # USED: the first is a movement into BH-PC, the second a goods
            # issue out of it, and between them sits whatever is standing on
            # the floor unopened. Named unambiguously here for that reason.
            "reached_floor_qty": _f(totals.get("issued_pc_qty")),
            "consumed_qty": self._consumed(window_from, window_to),
            "on_hand_qty": _f(totals.get("on_hand_qty")),
            # Buying activity for the plan month: how many orders were placed,
            # how much they ordered, and how much of it has arrived. All three
            # come off the same PO lines, so they tie: ordered = received + open.
            "po_count": orders["orders"],
            "po_lines": orders["lines"],
            "ordered_qty": orders["ordered_qty"],
            "po_received_qty": orders["received_qty"],
            "po_open_qty": orders["open_qty"],
            "ordered_value": orders["ordered_value"],
            "po_received_value": orders["received_value"],
            "po_open_value": orders["open_value"],
            "po_closed_lines": orders["closed_lines"],
            "po_basis": (
                "Purchase orders raised between "
                f"{window_from} and {window_to}. Received is SAP's ordered "
                "less still-open; a line closed by hand reads as fully "
                "received."
            ),
            # The standing open-order book, which is a different question from
            # the month's buying and is what the shortage is netted against.
            "open_po_qty": _f(totals.get("open_po_qty")),
            "open_po_overdue_count": int(totals.get("po_overdue_count") or 0),
            # The gate's own count of what physically arrived, kept as the
            # independent check on the SAP figure above.
            "grpo_received_qty": self._grpo_received(window_from, window_to),
            "grpo_basis": (
                "Accepted quantity on gate PO receipts landing in "
                f"{', '.join(STORE_WAREHOUSES)} between {window_from} and {window_to}."
            ),
            # Over-purchase, read straight off the requirement sheet rather
            # than recomputed here. That sheet already does this arithmetic —
            # ordered beyond what the plan still needs once stock is counted —
            # and the figure the business asked for is its `Req after PO`
            # column summed over the rows its Over-purchased filter selects.
            # Recomputing it in this app produced a second answer to the same
            # question, which is the one thing a wall board must not do.
            "over_purchased_qty": _f(totals.get("over_purchased_req_after_po_qty")),
            "over_purchased_count": int(totals.get("over_purchased_count") or 0),
            "over_purchase_value": _f(totals.get("over_purchase_value")),
            "over_purchase_basis": (
                "PM Requirement sheet: Req after PO, summed over the rows its "
                "Over-purchased filter selects."
            ),
            # A different question again, and kept: the floor drawing more than
            # the plan called for is not the same as the buyer ordering too
            # much.
            "over_issued_count": int(totals.get("over_issued_count") or 0),
            "short_count": int(totals.get("short_after_po_count") or 0),
            "short_value": _f(totals.get("short_after_po_value")),
            # The benchmark position across the three stores.
            "sku_count": benchmark["sku_count"],
            "healthy_count": benchmark["healthy_count"],
            "below_benchmark_count": benchmark["below_benchmark_count"],
            "low_count": benchmark["low_count"],
            "critical_count": benchmark["critical_count"],
            "below_benchmark_tonnes": benchmark["below_benchmark_tonnes"],
            "unweighed_below_benchmark": benchmark["unweighed_below_benchmark"],
            "benchmark_basis": benchmark["benchmark_basis"],
            "stores": STORE_WAREHOUSES,
            # Worst rows, for the band's ticker. Ranked by shortage value, which
            # is how the requirement sheet itself ranks: 500,000 caps short and
            # 500 tins short are not the same problem.
            "worst": [
                {
                    "item_code": row.get("item_code"),
                    "item_name": row.get("item_name"),
                    "short_qty": _f(row.get("short_qty")),
                    "short_value": _f(row.get("short_value")),
                    "po_overdue": bool(row.get("po_overdue")),
                }
                for row in rows[:MAX_LISTED_ROWS]
                if _f(row.get("short_qty")) > 0
            ],
        }

    def _benchmark(self) -> Dict[str, Any]:
        """The Stock Benchmark dashboard's own numbers, for the same stores.

        THE FILTER IS THE POINT. This sends the warehouse, status, movement and
        item-group filters that page opens with, so somebody reading this tile
        who then opens that page finds the same count. An earlier version sent
        only the warehouses and reported an order of magnitude more SKUs,
        because it was counting slow movers and items with no benchmark that the
        page sets aside.

        That version also derived the unset count by subtraction, which was
        wrong for the same reason: SAP's status SQL excludes slow movers from
        ALL four statuses, so "everything that is not healthy, low or critical"
        is not "has no benchmark" — it is those plus every slow mover.

        Tonnage comes back from the same read, so the weight and the count can
        never describe different sets. It is gross case weight, incomplete
        wherever SAP holds none, which is why the unweighed count rides
        alongside rather than being rounded away.
        """
        filters = {
            "warehouse": list(STORE_WAREHOUSES),
            "status": list(BENCHMARK_STATUSES),
            "movement_status": list(BENCHMARK_MOVEMENT),
            "item_group": BENCHMARK_ITEM_GROUP,
            "page": 1,
            "page_size": 1,
        }
        meta = (self.stock.get_stock_levels(filters) or {}).get("meta") or {}

        healthy = int(meta.get("healthy_count") or 0)
        low = int(meta.get("low_stock_count") or 0)
        critical = int(meta.get("critical_stock_count") or 0)
        tonnes = meta.get("below_benchmark_tonnes")

        return {
            # What the page's own "Total Items" card shows: the three health
            # statuses added, not the unfiltered row count.
            "sku_count": healthy + low + critical,
            "healthy_count": healthy,
            "below_benchmark_count": low + critical,
            "low_count": low,
            "critical_count": critical,
            "below_benchmark_tonnes": (
                round(float(tonnes), 3) if tonnes is not None else None
            ),
            "unweighed_below_benchmark": int(meta.get("unweighed_below_benchmark") or 0),
            "benchmark_basis": (
                "Stock Benchmark filters: "
                f"{', '.join(STORE_WAREHOUSES)} · {BENCHMARK_ITEM_GROUP} · "
                "recently used · benchmark set."
            ),
        }

    def _consumed(self, window_from: date, window_to: date) -> float:
        """Packing material the line actually used, plan-to-date.

        ``TransType`` 60 out of the consumption store — the goods issue, which
        is the only figure that answers "what did the line use". Deliberately
        NOT the transfer into that store: on a bottle this factory blows rather
        than buys, reading the transfer reported 2 pieces against 603,505
        really consumed.

        Read through the packing-material reader rather than its
        ``get_production`` service call, which would run this same query and
        then rank and truncate a result the board only needs the total of.
        """
        rows = self.packing.reader.pm_issued(
            consumption_warehouses(self.company_code), window_from, window_to
        )
        return round(sum(_f(row.get("issued_qty")) for row in rows), 3)

    def _grpo_received(self, window_from: date, window_to: date) -> float:
        """Packing material physically received into the stores, plan-to-date.

        Read from FactoryFlow's own gate PO receipts rather than from SAP's
        GRPO documents, because the gate register is where the accepted and
        rejected split is recorded — and accepted is what "purchased" means to
        a buyer counting what he can actually use.

        Scoped by destination warehouse rather than by item group, which costs
        no SAP round trip on a board that re-reads every minute: a packaging PO
        lands in a packaging store. Documented as the basis on the response so
        the tile can be read for what it is.
        """
        total = (
            POItemReceipt.objects.filter(
                po_receipt__vehicle_entry__company__code=self.company_code,
                po_receipt__vehicle_entry__entry_time__date__gte=window_from,
                po_receipt__vehicle_entry__entry_time__date__lte=window_to,
                warehouse_code__in=STORE_WAREHOUSES,
            )
            .aggregate(total=Sum("accepted_qty"))
            .get("total")
        )
        return _f(total)

    # ------------------------------------------------------------------
    # Band 2: Store
    # ------------------------------------------------------------------

    def _store(self) -> Dict[str, Any]:
        """Non-moving stock, today's packaging trucks, and today's blowing cost.

        Stock Space now answers, from the board's own settings page rather than
        from SAP — which holds neither a tonnage capacity nor anything an audit
        date can be inferred from. What is still missing there is the tonnage
        HELD: no stock query in this app reads a weight for these stores, so the
        tile can report its capacity and its last count but not yet how full it
        is.
        """
        # Calendar month to date, NOT the plan month. Two reasons: it is what
        # the Blowing dashboard's own monthly summary uses (`date__year` /
        # `date__month`), so the two screens agree; and it cannot be dragged
        # onto a past month by a plan that has not been created yet, which the
        # plan window legitimately can.
        month_from = self.today.replace(day=1)
        return {
            "stock_space": self._stock_space(),
            "non_moving": non_moving_snapshot(self.company_code, STORE_WAREHOUSES),
            "pm_vehicles_today": self._pm_vehicles_today(),
            "blowing": self._blowing(month_from, self.today),
        }

    def _stock_space(self) -> Dict[str, Any]:
        """Rated capacity and the last physical count, per store.

        Both are typed in on the board's settings page, because neither is
        derivable: SAP's warehouse master holds no tonnage capacity, and a clean
        stock count writes no movement row at all — so a date read off the
        movement log would mean "the last count that found a discrepancy",
        which is a different and much bleaker statistic.

        Two choices worth stating:

        **The audit date reported is the OLDEST of the configured stores, not
        the newest.** One store counted yesterday says nothing about the two
        that have not been counted since March, and the point of the figure is
        to find the store nobody has been to.

        **Capacity only totals when every store has one.** Adding two rated
        capacities and calling it the total quietly understates the
        denominator, which would make the stores look fuller than they are —
        the one direction an unset capacity must never fail in.
        """
        rows = {
            row.warehouse: row
            for row in WarehouseBoardSettings.objects.filter(
                company_code=self.company_code, warehouse__in=STORE_WAREHOUSES
            )
        }

        stores: List[Dict[str, Any]] = []
        capacities: List[float] = []
        audited: List[Dict[str, Any]] = []

        for code in STORE_WAREHOUSES:
            row = rows.get(code)
            capacity = None
            audit = None
            if row is not None:
                capacity = float(row.capacity_tonnes) if row.capacity_tonnes is not None else None
                audit = row.last_audit_date
            if capacity is not None:
                capacities.append(capacity)
            if audit is not None:
                audited.append({"warehouse": code, "date": audit})
            stores.append(
                {
                    "warehouse": code,
                    "capacity_tonnes": capacity,
                    "last_audit_date": audit.isoformat() if audit else None,
                    "audit_days_ago": (self.today - audit).days if audit else None,
                }
            )

        oldest = min(audited, key=lambda item: item["date"]) if audited else None

        return {
            "stores": stores,
            "area": self._floor_area(),
            # Null unless every store has one — see the docstring.
            "capacity_tonnes": (
                round(sum(capacities), 2) if len(capacities) == len(STORE_WAREHOUSES) else None
            ),
            "capacity_configured": len(capacities),
            "store_count": len(STORE_WAREHOUSES),
            # The store that has gone longest without a count.
            "last_audit_date": oldest["date"].isoformat() if oldest else None,
            "last_audit_warehouse": oldest["warehouse"] if oldest else "",
            "audit_days_ago": (self.today - oldest["date"]).days if oldest else None,
            "audit_configured": len(audited),
            "basis": (
                "Typed on the board's settings page. SAP holds no tonnage "
                "capacity, and a clean count writes no movement row to infer a "
                "date from. The date shown is the oldest of the configured "
                "stores."
            ),
        }

    def _floor_area(self) -> Dict[str, Any]:
        """The floor the stores have, and the stock standing on it.

        PIECES -> PALLETS -> SQUARE FEET, IN TWO MEASURED STEPS. SAP cannot
        bridge stock and floor: verified against all 878 packaging items in
        Oil, ``OITM`` holds no volume, no dimensions, and a gross weight on 155
        of them. The factory bridges it instead, and both steps are its own
        measurements rather than this board's arithmetic:

        1. Pieces per pallet, PER ITEM, from the factory's stacking sheet in
           ``plant_board/data/stacking.json``. Per item is the whole point --
           300 five-litre bottles fill a pallet and 60,000 caps fill one, so a
           blended rate would be dominated by whichever item is numerous.
        2. The floor one pallet stands on, from the settings page. 15 sq ft as
           measured here.

        Live SAP stock is what both are applied to; the stacking sheet's own
        quantity column is ignored, because it is a snapshot of one morning and
        several of its rows differ from live stock by orders of magnitude.

        WHAT IT CANNOT MEASURE, IT NAMES. An item with no pallet figure is
        counted in the pieces and left out of the floor, and the count of those
        rides on the response -- every one of them makes the stores read
        emptier than they are, which is the one direction this tile must not
        fail in. Until the pallet footprint is set, both halves are still
        reported in their own real units and the percentage reads as a rule: an
        invented percentage on a wall is worse than an honest gap, because
        somebody would plan a building against it.
        """
        rows = self.reader.packaging_stock(STORE_WAREHOUSES)
        per_pallet = pieces_per_pallet()

        held: Dict[str, Dict[str, Any]] = {}
        unmeasured_codes: set = set()
        unmeasured_pieces = 0.0
        for row in rows:
            slot = held.setdefault(
                row["warehouse"],
                {"pieces": 0.0, "value": 0.0, "items": 0, "unpriced_items": 0, "pallets": 0.0},
            )
            slot["pieces"] += row["pieces"]
            slot["value"] += row["value"]
            slot["items"] += 1
            slot["unpriced_items"] += 1 if row["unpriced"] else 0

            # Each item on its own pallet factor: 300 five-litre bottles fill
            # one, 60,000 caps fill one. A blended rate would be dominated by
            # whichever item happens to be numerous, which here is caps.
            factor = per_pallet.get(row["item_code"])
            if factor:
                slot["pallets"] += row["pieces"] / factor
            else:
                unmeasured_codes.add(row["item_code"])
                unmeasured_pieces += row["pieces"]

        blocks = [
            {
                "key": block["key"],
                "label": block["label"],
                "warehouses": list(block["warehouses"]),
                "sqft": block["sqft"],
            }
            for block in PACKAGING_FLOOR_BLOCKS
        ]

        pieces = sum(row["pieces"] for row in held.values())
        value = sum(row["value"] for row in held.values())
        unpriced = sum(row["unpriced_items"] for row in held.values())
        if unpriced:
            self._warnings.append(
                f"{unpriced} packaging items in the stores carry no purchase "
                "price, so the stock value understates what is held."
            )

        pallets = sum(row["pallets"] for row in held.values())

        # The measured default unless this company has said otherwise, and
        # tolerant of the table not being there at all: this app's settings are
        # deployed with a migration, and a board that dies before it runs would
        # take out a band that has nothing to do with the setting.
        sqft_per_pallet = DEFAULT_SQFT_PER_PALLET
        try:
            settings_row = PlantBoardSettings.objects.filter(
                company_code=self.company_code
            ).first()
        except Exception as exc:  # noqa: BLE001 - see above
            logger.warning("plant_board: space settings unreadable: %s", exc)
            settings_row = None
        if settings_row is not None:
            # An explicit null means somebody cleared it: they are saying the
            # figure is not known here, which is different from never having
            # been asked, so it is honoured rather than defaulted over.
            sqft_per_pallet = (
                float(settings_row.sqft_per_pallet)
                if settings_row.sqft_per_pallet is not None
                else None
            )
        occupied = None
        free = None
        occupied_pct = None
        if sqft_per_pallet:
            occupied = round(pallets * sqft_per_pallet, 1)
            # Floored at zero: a store packed past its measured area is a real
            # thing, and negative free space is arithmetic nobody can act on.
            # The percentage is left uncapped so an overfill is still visible.
            free = round(max(0.0, PACKAGING_FLOOR_SQFT - occupied), 1)
            occupied_pct = round(occupied / PACKAGING_FLOOR_SQFT * 100, 1)

        if unmeasured_codes:
            self._warnings.append(
                f"{len(unmeasured_codes)} packaging items have no pallet "
                "figure, so the floor in use is understated."
            )

        return {
            "sqft": PACKAGING_FLOOR_SQFT,
            "blocks": blocks,
            "store_count": len(STORE_WAREHOUSES),
            "held_pieces": round(pieces, 2),
            "held_value": round(value, 2),
            "held_items": sum(row["items"] for row in held.values()),
            "unpriced_items": unpriced,
            "pallets": round(pallets, 1),
            "sqft_per_pallet": sqft_per_pallet,
            "stacking_measured_on": measured_on(),
            "occupied_sqft": occupied,
            "free_sqft": free,
            # Stock the pallet figures cannot speak for. Named rather than
            # quietly excluded: every piece of it makes the floor read emptier
            # than it is, which is the one direction this tile must not fail in.
            "unmeasured_items": len(unmeasured_codes),
            "unmeasured_pieces": round(unmeasured_pieces, 2),
            "by_store": [
                {
                    "warehouse": code,
                    "pieces": round(held.get(code, {}).get("pieces", 0.0), 2),
                    "value": round(held.get(code, {}).get("value", 0.0), 2),
                    "items": held.get(code, {}).get("items", 0),
                    "pallets": round(held.get(code, {}).get("pallets", 0.0), 1),
                    "occupied_sqft": (
                        round(held.get(code, {}).get("pallets", 0.0) * sqft_per_pallet, 1)
                        if sqft_per_pallet
                        else None
                    ),
                }
                for code in STORE_WAREHOUSES
            ],
            # Null until somebody measures the factor. See the docstring.
            "occupied_pct": occupied_pct,
            "occupancy_blocked_on": (
                ""
                if sqft_per_pallet
                else (
                    "The floor one pallet stands on. SAP holds no volume, no "
                    "dimensions and a gross weight on under a fifth of the "
                    "range, so the stock and the floor cannot be put on one "
                    "scale without it. Set it on the board's settings page."
                )
            ),
        }

    def _pm_vehicles_today(self) -> Dict[str, Any]:
        """How many trucks brought packing material in today.

        There is no ``PACKING_MATERIAL`` gate entry type — the five are
        RAW_MATERIAL, DAILY_NEED, MAINTENANCE, CONSTRUCTION and FIXED_ASSET —
        so a packaging truck arrives as a raw-material entry against a purchase
        order. It is identified here by where its PO lines land: a packaging PO
        lands in a packaging store.

        Counted DISTINCT on the vehicle entry. One truck carrying three POs is
        one truck, and the tile says "vehicles".
        """
        entries = POItemReceipt.objects.filter(
            po_receipt__vehicle_entry__company__code=self.company_code,
            po_receipt__vehicle_entry__entry_time__date=self.today,
            warehouse_code__in=STORE_WAREHOUSES,
        )
        summary = entries.aggregate(
            vehicles=Count("po_receipt__vehicle_entry", distinct=True),
            pos=Count("po_receipt", distinct=True),
            lines=Count("id"),
            received=Sum("received_qty"),
            accepted=Sum("accepted_qty"),
            rejected=Sum("rejected_qty"),
        )

        received = _f(summary.get("received"))
        accepted = _f(summary.get("accepted"))
        rejected = _f(summary.get("rejected"))

        return {
            # Distinct on the vehicle entry: one truck carrying three orders is
            # one truck, and the tile says "vehicles".
            "count": int(summary.get("vehicles") or 0),
            "po_count": int(summary.get("pos") or 0),
            "line_count": int(summary.get("lines") or 0),
            "received_qty": received,
            # WHERE THESE THREE COME FROM, BECAUSE IT IS NOT ONE PLACE.
            #
            # `received_qty` is written at the GATE, when the truck is
            # unloaded. `accepted_qty` and `rejected_qty` are written much
            # later, by `grpo.services.post_grpo`, at the moment the goods
            # receipt is posted to SAP -- rejected being whatever of the
            # received quantity was not accepted.
            #
            # So the gap between them is not a quality question. It is stock
            # standing in the building that SAP does not have yet, because
            # nobody has posted its GRPO. On a board scoped to today that gap
            # is usually most of the day's intake, which is normal rather than
            # alarming, and the tile has to say which of the two it means.
            "accepted_qty": accepted,
            "rejected_qty": rejected,
            "awaiting_grpo_qty": round(max(0.0, received - accepted - rejected), 3),
        }

    def _blowing(self, window_from: date, window_to: date) -> Dict[str, Any]:
        """Bottles blown this month, what they cost, and both figures per day.

        THE SAME FIGURES THE BLOWING DASHBOARD SHOWS. Its monthly summary sums
        `total_counter_production`, `rejection_pcs` and `cost_summary__net_cost`
        over a calendar month with no status filter on the runs, and this reads
        exactly those three fields the same way -- so a reader who opens that
        page after this tile finds the same numbers.

        One figure is deliberately NOT the same. That page reports
        `Avg(per_bottle_cost)`: the unweighted mean of each run's own rate,
        which lets a short run count as heavily as a long one. The rate here is
        total cost over total bottles, which is the weighted figure and the only
        one that reconciles with the two totals beside it.

        Counted from the RUN rather than from its costing. `total_counter_production`
        is the machine's own counter and is recorded whether or not anybody has
        costed the run yet, so a day's output never disappears from the board
        because the paperwork is behind. The money comes from the cost summary
        and is therefore only as complete as the costing — which is why the
        uncosted run count rides alongside it rather than being swallowed.

        Both averages are over the days that actually BLEW, not over the days
        in the month. That is the same rule the Production band's average uses,
        and for the same reason: a Sunday is not a bad day, and dividing by it
        reports one.

        Cost is `net_cost` — after scrap recovery is credited back, and with
        resin included, which is what makes it comparable to the price of a
        bought-in bottle.
        """
        runs = BlowingRun.objects.filter(
            company__code=self.company_code,
            date__gte=window_from,
            date__lte=window_to,
        ).select_related("cost_summary")

        made = 0
        rejected = 0
        net = ZERO
        preform = ZERO
        costed_bottles = 0
        uncosted = 0
        run_count = 0
        active_days = set()
        by_day: Dict[date, int] = {}

        for run in runs:
            run_count += 1
            active_days.add(run.date)
            bottles = int(run.total_counter_production or 0)
            made += bottles
            by_day[run.date] = by_day.get(run.date, 0) + bottles
            rejected += int(run.rejection_pcs or 0)

            cost = getattr(run, "cost_summary", None)
            if cost is None:
                uncosted += 1
                continue
            net += cost.net_cost or ZERO
            preform += cost.preform_cost or ZERO
            costed_bottles += int(cost.good_bottles or 0)

        if uncosted:
            self._warnings.append(
                f"{uncosted} of {run_count} blowing runs this month have no cost row yet."
            )

        days = len(active_days)
        total_cost = _f(net)
        good = max(0, made - rejected)
        running = self._blowing_running()

        # Every day in the trailing window, so a day the machines stood idle
        # reads as a gap rather than as a day nobody asked about. The board
        # shows the last week of a month that may be much longer.
        trend: List[Dict[str, Any]] = []
        first = max(window_from, window_to - timedelta(days=TREND_DAYS - 1))
        day = first
        while day <= window_to:
            trend.append({"date": day.isoformat(), "bottles": by_day.get(day, 0)})
            day += timedelta(days=1)

        return {
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "daily": trend,
            "runs": run_count,
            "uncosted_runs": uncosted,
            "active_days": days,
            # Bottles off the machine counter, and the good ones after rejects.
            "bottles_made": made,
            "bottles_rejected": rejected,
            "bottles_good": good,
            "avg_bottles_per_day": round(made / days, 1) if days else None,
            # Net of scrap recovery, resin included.
            "cost": round(total_cost, 2),
            "preform_cost": _f(preform),
            "conversion_cost": round(max(0.0, total_cost - _f(preform)), 2),
            "avg_cost_per_day": round(total_cost / days, 2) if days else None,
            # Per bottle over the COSTED runs only, so an uncosted run cannot
            # drag the rate down by contributing bottles with no money.
            "cost_per_bottle": (
                round(total_cost / costed_bottles, 4) if costed_bottles else None
            ),
            **running,
            "basis": (
                "Bottles from the machine counter; cost is net of scrap with "
                "resin included. Averages are over the days that blew, not the "
                "days in the month."
            ),
        }

    def _blowing_running(self) -> Dict[str, Any]:
        """What the lines that are running right now have cost so far.

        RUNNING MEANS AN OPEN SEGMENT, NEVER THE RUN'S STATUS. A blowing run
        sits at `IN_PROGRESS` from the moment it is started until somebody
        completes it, including every hour the machine stands idle in between —
        the production board learned this the hard way, where seven runs
        reported IN_PROGRESS while only three had an open segment. A segment
        with no `end_time` is the only thing that means a machine is turning.

        Summed ACROSS RUNS, because a day frequently carries more than one: the
        two costs below are per run, so a single run's figures would report one
        line's spend as the floor's.

        The money is `preform_cost + blowing_cost` — resin plus everything the
        blowing itself costs, net of scrap recovery. That pair is the run's
        fully loaded cost, and it is the honest answer to "what has this cost
        so far" because it leaves nothing out.

        SO FAR means as of the last costing, not to the minute. The cost row is
        rewritten when a run is saved, updated or completed, so a run started an
        hour ago and not touched since carries the counter reading it was last
        saved with. It is a floor on the real figure, never an overstatement.
        """
        runs = (
            BlowingRun.objects.filter(
                company__code=self.company_code,
                segments__end_time__isnull=True,
                segments__is_active=True,
            )
            .select_related("cost_summary")
            .distinct()
        )

        preform = ZERO
        blowing = ZERO
        bottles = 0
        count = 0
        uncosted = 0
        started_earlier = 0

        for run in runs:
            count += 1
            bottles += int(run.total_counter_production or 0)
            if run.date < self.today:
                started_earlier += 1
            cost = getattr(run, "cost_summary", None)
            if cost is None:
                uncosted += 1
                continue
            preform += cost.preform_cost or ZERO
            blowing += cost.blowing_cost or ZERO

        return {
            "running_runs": count,
            "running_bottles": bottles,
            "running_preform_cost": _f(preform),
            "running_blowing_cost": _f(blowing),
            "running_cost": round(_f(preform) + _f(blowing), 2),
            "running_uncosted": uncosted,
            # A run still open from an earlier day is either a line that worked
            # through midnight or a segment nobody closed. Either way the
            # figure beside it is not "today", so the board can say so.
            "running_started_earlier": started_earlier,
        }

    # ------------------------------------------------------------------
    # Band 3: Production
    # ------------------------------------------------------------------

    def _production(self, plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Plan against output in CASES, the floor's stock, its age and its waste.

        Both halves are PIECES, and neither is money. The run-costing engine is
        not involved in this band at all, and no pack factor is applied on
        either side: SAP holds the plan in ``OITM.InvntryUom`` and posts the
        production receipt in the same unit, so the comparison is between two
        figures that are already alike. Cases are carried alongside for the
        floor, which speaks in them.
        """
        if not plan or not plan.get("abs_id"):
            raise ValueError("no plan to report production against")

        detail = self.plans.get_plan(plan["abs_id"], include_actuals=True)
        lines = detail.get("lines") or []

        # PIECES on both sides, which is the unit SAP already holds them in:
        # the plan is in `OITM.InvntryUom` (PCS for all but one SKU here) and a
        # production receipt is posted in the same unit. Comparing them needs no
        # conversion at all, which removes the division that the existing
        # plan-vs-production report gets wrong by the case factor.
        planned_qty = sum((line.get("planned_qty") or ZERO) for line in lines)
        produced_qty = sum((line.get("produced_qty") or ZERO) for line in lines)
        # Cases kept alongside: the floor speaks in them even though SAP does
        # not, and a reader who wants them should not need a second request.
        planned_cases = sum((line.get("planned_cases") or ZERO) for line in lines)
        produced_cases = sum((line.get("produced_cases") or ZERO) for line in lines)
        planned_litres = sum((line.get("planned_litres") or ZERO) for line in lines)
        produced_litres = sum((line.get("produced_litres") or ZERO) for line in lines)
        # Lines the tonnage cannot speak for. A litre volume exists in SAP only
        # where `U_IsLitre = 'Y'`, so an item without it is planned and produced
        # in pieces and contributes nothing to either ton figure. Counted rather
        # than hidden: the board shows tons, and a reader is entitled to know
        # how many SKUs those tons leave out.
        unweighed_lines = sum(
            1
            for line in lines
            if (line.get("planned_qty") or ZERO) > 0
            and not (line.get("planned_litres") or ZERO)
        )

        window_from, window_to = self._plan_window(plan)
        daily = self._daily_pieces(lines, window_from, window_to)
        active_days = [row for row in daily if row["qty"] > 0]

        floor = self._floor_stock()

        return {
            "planned_qty": _f(planned_qty),
            "produced_qty": _f(produced_qty),
            "planned_cases": _f(planned_cases),
            "produced_cases": _f(produced_cases),
            # Computed on PIECES, not cases. The two ratios are not the same
            # number: each is a sum across items with different pack factors,
            # so converting first re-weights the mix.
            "attainment_pct": (
                round(_f(produced_qty) / _f(planned_qty) * 100, 1)
                if _f(planned_qty) > 0
                else None
            ),
            "planned_tons": _tons(planned_litres),
            "produced_tons": _tons(produced_litres),
            # The same ratio on the tonnage, because the tile reads in tons and
            # a percentage beside figures it was not computed from is a lie the
            # reader cannot see. It is NOT the piece ratio: each is a sum over
            # items of different litres per piece, so the mix re-weights it.
            "attainment_tons_pct": (
                round(_tons(produced_litres) / _tons(planned_litres) * 100, 1)
                if _tons(planned_litres) > 0
                else None
            ),
            "unweighed_lines": unweighed_lines,
            # The average the business defined: output over the days that
            # actually produced, so a Sunday does not drag it down and it reads
            # as typical output on a working day.
            "avg_qty_per_active_day": (
                round(_f(produced_qty) / len(active_days), 1) if active_days else None
            ),
            "active_days": len(active_days),
            "elapsed_days": plan.get("days_elapsed"),
            # Today off the lines themselves, not off SAP's journal.
            "today": self._today_on_the_lines(),
            "daily": daily[-MAX_LISTED_ROWS:],
            "floor": floor,
            "wastage": self._wastage(window_from, window_to, produced_litres),
        }

    def _today_on_the_lines(self) -> Dict[str, Any]:
        """What the supervisor planned for today, and what the lines have packed.

        READ FROM PRODUCTION EXECUTION, NOT FROM SAP. Every other figure in this
        band comes from SAP's own journal, and deliberately so — but SAP holds no
        plan for a single day, only the month's, and it learns of output when the
        goods receipt is posted, which is after the shift. The supervisor's own
        register is the only place a day's intention exists, and it is where the
        floor records output as it happens. The two sources will not agree until
        the receipts are posted; that is expected, and is why this tile is
        labelled as the lines' own figure rather than presented as SAP's.

        MANY RUNS, ONE DAY. A line runs several times a day and several lines run
        at once, so both halves are a sum across every live run dated today —
        across lines, runs and shifts alike. Discarded runs are excluded by the
        default manager, so a thrown-away plan never inflates the target.

        DRAFTS COUNT AS PLANNED. A run is planned the evening before and sits in
        DRAFT until the line starts it, so a board that only counted started runs
        would read zero planned every morning — the one time of day the number
        matters most.

        PRODUCED BEFORE THE RUN IS CLOSED. ``total_production`` is typed at
        completion and is zero for the whole shift until then, so a run still on
        the floor falls back to the sum of its segments, which the floor updates
        as it goes. Same rule the production reconciliation uses.

        CASES ARE WHAT THE BOARD SHOWS, because cases are what the supervisor
        types: the planning dialog's quantity box is in them, so a wall figure in
        cases is one the floor can check against its own register with no
        arithmetic. The band's other tiles are in pieces, and that mismatch is
        deliberate — those read SAP, which holds pieces, and this reads the
        register, which holds cases.

        Pieces are carried alongside for anyone comparing the two, converted per
        run by its own ``pieces_per_case`` (the snapshot of SAP
        ``OITM.SalFactor2`` taken when the run was planned). NEVER by one shared
        factor: a normal day runs 4, 10 and 20 side by side, so 4,800 cases
        across five runs was 60,000 pieces and no single multiplier reaches that.
        A run whose SKU never resolved carries no factor, so it is left out of
        the piece totals and counted in ``unconverted_runs`` — adding a case
        count to a piece count is wrong by a factor of twenty and silently so.
        The case totals need no factor and are therefore always whole.
        """
        runs = list(
            ProductionRun.objects.filter(
                company__code=self.company_code, date=self.today
            ).values(
                "id", "line_id", "status", "required_qty",
                "total_production", "pieces_per_case",
            )
        )
        if not runs:
            return {
                "date": self.today.isoformat(),
                "planned_qty": 0.0,
                "produced_qty": 0.0,
                "planned_cases": 0.0,
                "produced_cases": 0.0,
                "attainment_pct": None,
                "runs": 0,
                "lines": 0,
                "completed_runs": 0,
                "unconverted_runs": 0,
            }

        # One query for every open run's segments, rather than one per run.
        segments = {
            row["production_run"]: row["cases"] or ZERO
            for row in ProductionSegment.objects.filter(
                production_run_id__in=[run["id"] for run in runs]
            )
            .values("production_run")
            .annotate(cases=Sum("produced_cases"))
        }

        planned_pieces = ZERO
        produced_pieces = ZERO
        planned_cases = ZERO
        produced_cases = ZERO
        completed = 0
        unconverted = 0

        for run in runs:
            if run["status"] == "COMPLETED":
                completed += 1

            planned = run["required_qty"] or ZERO
            made = run["total_production"] or ZERO
            if made <= ZERO:
                made = segments.get(run["id"], ZERO)

            planned_cases += planned
            produced_cases += made

            per_case = run["pieces_per_case"]
            if not per_case:
                # Only a run that actually carries a quantity is worth reporting
                # as unconvertible; an empty draft has nothing to lose.
                if planned > ZERO or made > ZERO:
                    unconverted += 1
                continue
            planned_pieces += planned * per_case
            produced_pieces += made * per_case

        return {
            "date": self.today.isoformat(),
            "planned_qty": _f(planned_pieces),
            "produced_qty": _f(produced_pieces),
            "planned_cases": _f(planned_cases),
            "produced_cases": _f(produced_cases),
            "attainment_pct": (
                round(_f(produced_pieces) / _f(planned_pieces) * 100, 1)
                if planned_pieces > ZERO
                else None
            ),
            "runs": len(runs),
            "lines": len({run["line_id"] for run in runs}),
            "completed_runs": completed,
            # Runs with no case factor. They cost the CASE totals nothing --
            # those need no conversion -- and are named only to qualify the
            # piece figures above, which are short by exactly these.
            "unconverted_runs": unconverted,
        }

    def _daily_pieces(self, lines, window_from: date, window_to: date) -> List[Dict[str, Any]]:
        """Pieces produced per posting date, for the band's trend and its average.

        Read from SAP's own movement journal — ``OINM`` TransType 59, the goods
        receipt from production, in the item's own inventory unit. NOT from
        ``ProductionRun.total_production``, which is in cases, stays zero until
        a run is completed, and disagrees with the sum of its own segments by
        up to 3.4x on live records.

        No pack-factor conversion happens here at all any more. The plan and the
        receipt are both in pieces, so the comparison the band draws is between
        two figures SAP itself holds in the same unit.
        """
        codes = [line.get("item_code") for line in lines if line.get("item_code")]
        if not codes:
            return []

        rows = self.plan_reader.get_daily_produced_quantities(
            codes, window_from, window_to
        )

        by_day: Dict[date, float] = {}
        for row in rows:
            day = _as_date(row.get("DocDate"))
            if day is None:
                continue
            by_day[day] = by_day.get(day, 0.0) + _f(row.get("ProducedQty"))

        # Every day in the window, so a gap reads as a day with no output rather
        # than as a day the board did not ask about.
        out: List[Dict[str, Any]] = []
        day = window_from
        while day <= window_to:
            out.append({"date": day.isoformat(), "qty": round(by_day.get(day, 0.0), 2)})
            day += timedelta(days=1)
        return out

    def _floor_stock(self) -> Dict[str, Any]:
        """What BH-PF holds, what it is worth, and how long it has stood there.

        Value comes from the occupancy endpoint the Production Control board
        already reads. The age split is this app's own read, because the batches
        endpoint answers one item at a time and a wall board cannot make two
        hundred calls a minute.

        Aged on when stock last LEFT, never on any movement. Items with no
        outbound movement in their history are counted as ``never_shipped``
        rather than folded into the oldest bucket: "has never left" and "left a
        long time ago" are different problems, and the first is the worse one.
        """
        occupancy = self.stock.get_warehouse_occupancy(PRODUCTION_FLOOR)
        rows = occupancy.get("data") or []
        meta = occupancy.get("meta") or {}

        last_out = self.reader.last_out_dates(PRODUCTION_FLOOR)

        def bucket() -> Dict[str, Any]:
            return {"value": 0.0, "pieces": 0.0, "litres": 0.0, "items": 0, "unweighed": 0}

        fresh = bucket()
        mid = bucket()
        old = bucket()
        never = bucket()
        litres = 0.0
        unweighed = 0

        for row in rows:
            pieces = _f(row.get("on_hand"))
            if pieces <= 0:
                continue
            value = _f(row.get("stock_value"))
            per_piece = _f(row.get("litres_per_piece"))
            row_litres = pieces * per_piece
            litres += row_litres
            # A SKU SAP holds no litre volume for contributes nothing to the
            # tonnage. Counted rather than ignored: every one of them makes the
            # tonnage an understatement, and a tile showing tonnes has to be
            # able to say by how many SKUs.
            if per_piece <= 0:
                unweighed += 1

            out_date = _as_date(last_out.get(row.get("item_code")))
            if out_date is None:
                slot = never
            else:
                days = (self.today - out_date).days
                if days < AGE_BUCKET_LOWER:
                    slot = fresh
                elif days <= AGE_BUCKET_UPPER:
                    slot = mid
                else:
                    slot = old

            slot["value"] += value
            slot["pieces"] += pieces
            slot["litres"] += row_litres
            slot["items"] += 1
            if per_piece <= 0:
                slot["unweighed"] += 1

        for slot in (fresh, mid, old, never):
            slot["value"] = round(slot["value"], 2)
            slot["pieces"] = round(slot["pieces"], 2)
            slot["tons"] = _tons(slot["litres"])
            slot["litres"] = round(slot["litres"], 3)

        return {
            "warehouse": PRODUCTION_FLOOR,
            "stock_value": _f(meta.get("total_value")),
            "total_pieces": _f(meta.get("total_on_hand")),
            "tons": _tons(litres),
            "item_count": int(meta.get("item_count") or 0),
            # How much of the tonnage rests on a fallback, so the tile can
            # disclose its own completeness rather than looking authoritative.
            "unconfigured_items": int(meta.get("unconfigured_items") or 0),
            # SKUs holding stock that SAP records no litre volume for, so the
            # tonnage above and in every bucket excludes them.
            "unweighed_items": unweighed,
            "age": {
                "fresh": fresh,
                f"d{AGE_BUCKET_LOWER}_{AGE_BUCKET_UPPER}": mid,
                f"d{AGE_BUCKET_UPPER}_plus": old,
                "never_shipped": never,
            },
            "age_basis": "Days since stock last left this warehouse, not since any movement.",
        }

    def _wastage(
        self, window_from: date, window_to: date, produced_litres
    ) -> Dict[str, Any]:
        """Oil waste as yield loss, packing waste as logged rows.

        The two halves are genuinely different measurements and the business
        chose them that way. Oil loss is what the floor cannot declare — litres
        issued against litres packed — while packaging waste is what somebody
        wrote down and four people signed off.

        The packaging figure is the app's own and is labelled as such: wastage is
        not posted to SAP, so ``BH-WST`` reads near zero and a comparison
        against it would fail every day for a reason that has nothing to do with
        the factory.

        DATED BY THE RUN, NOT BY WHEN IT WAS TYPED. A waste row is entered days
        after the shift it belongs to -- on live records only 8 of 795 rows were
        typed the same day, 426 landed the next day and the tail runs to eight
        days out. Keyed on ``created_at`` a day's waste is therefore not that
        day's waste at all: two shifts' worth lands on whichever morning someone
        did the paperwork. Keyed on the run's own date every row falls on the
        day the material was actually spoiled, which is the only version a
        per-day figure can mean. Standalone rows carry no run and keep their
        typed date, which is the best date they have.

        NOT ``ProductionMaterialUsage.wastage_qty``, which despite its name is
        not waste: it is ``opening + issued - closing``, and on live records
        ``issued`` and ``closing`` are zero on every line while ``opening`` holds
        the BOM requirement -- so the field equals the full material requirement
        of the run. Reading it as waste reports a day's entire consumption as
        spoilage, which on 11 Sep would have been 648,480 units against a real
        logged figure three orders of magnitude smaller.

        VALUED, BECAUSE UNITS DO NOT ADD UP. The register holds pieces, kilos
        and metres side by side (760 / 18 / 16 on live rows), so a single
        quantity total across them measures nothing -- 10,498 pieces plus 40
        metres is 10,538 of no unit at all. Money is the one scale every line
        shares, and it is also the scale the waste actually matters on: a
        wasted 5 L bottle at Rs 45.51 is not the same event as a wasted label
        at Rs 0.30. Quantities are kept alongside, each in its own unit, so the
        rupee figure can always be taken apart.

        PRICED FROM THE RUN'S OWN MATERIAL LINE. ``WasteLog`` carries no price,
        so each row is valued at the ``unit_price`` on the SAME run's
        ``ProductionMaterialUsage`` line for that material -- the SAP
        ``LastPurPrc`` snapshotted when that run started, which is the price
        that run was costed at. 298 of 300 live rows are covered that way.
        Where the run holds no price the most recent price for that material
        code stands in, and a row that can be valued at neither is counted in
        ``unpriced`` rather than being dropped or valued at zero: 793 of 795
        live rows price exactly, and the two that do not are named instead of
        quietly understating the day.
        """
        logs = list(
            WasteLog.objects.filter(company__code=self.company_code)
            .filter(
                Q(
                    production_run__date__gte=window_from,
                    production_run__date__lte=window_to,
                )
                | Q(
                    production_run__isnull=True,
                    created_at__date__gte=window_from,
                    created_at__date__lte=window_to,
                )
            )
            .values(
                "material_code",
                "wastage_qty",
                "wastage_approval_status",
                "uom",
                "production_run_id",
                "production_run__date",
                "created_at",
            )
        )

        kinds = self.reader.classify_items(
            sorted({row["material_code"] for row in logs if row["material_code"]})
        )
        exact_price, last_price = self._waste_prices(logs)

        pm_qty = 0.0
        pm_value = 0.0
        oil_logged_qty = 0.0
        oil_logged_value = 0.0
        unclassified = 0
        approved = 0
        standalone = 0
        unpriced = 0
        #: day -> kind -> {"uom": {...}, "value": float}, plus a row count.
        by_day: Dict[date, Dict[str, Any]] = {}

        for row in logs:
            qty = _f(row["wastage_qty"])
            if row["wastage_approval_status"] == "FULLY_APPROVED":
                approved += 1

            day = row["production_run__date"]
            if day is None:
                standalone += 1
                day = timezone.localtime(row["created_at"]).date()

            price = exact_price.get(
                (row["production_run_id"], row["material_code"])
            ) or last_price.get(row["material_code"])
            value = qty * _f(price) if price else 0.0

            kind = kinds.get(row["material_code"])
            if kind == "PACKAGING":
                pm_qty += qty
                pm_value += value
                bucket = "pm"
            elif kind == "RAW":
                oil_logged_qty += qty
                oil_logged_value += value
                bucket = "rm"
            else:
                unclassified += 1
                bucket = "other"

            slot = by_day.setdefault(
                day,
                {
                    "pm": {}, "rm": {}, "other": {},
                    "value": {"pm": 0.0, "rm": 0.0, "other": 0.0},
                    "logs": 0, "unpriced": 0,
                },
            )
            slot["logs"] += 1
            uom = (row["uom"] or "").strip().upper() or "-"
            slot[bucket][uom] = slot[bucket].get(uom, 0.0) + qty
            slot["value"][bucket] += value
            if not price:
                unpriced += 1
                slot["unpriced"] += 1

        logged_daily = self._waste_daily(by_day, window_from, window_to)
        latest = max((day for day, slot in by_day.items() if slot["logs"]), default=None)

        yields = self.reader.oil_yield(window_from, window_to)
        issued = yields["issued_litres"]
        packed = yields["packed_litres"]
        loss = issued - packed

        if yields["assumed_litre_uom_lines"]:
            self._warnings.append(
                f"{yields['assumed_litre_uom_lines']} oil issue lines are not flagged "
                "as litre items in SAP; their raw quantity was taken as litres."
            )

        return {
            # OIL: yield loss, with both components exposed. A lone "waste %"
            # cannot be argued with; issued against packed shows which half moved.
            "oil_issued_litres": round(issued, 2),
            "oil_packed_litres": round(packed, 2),
            "oil_loss_litres": round(loss, 2),
            "oil_loss_tons": _tons(loss),
            "oil_loss_pct": round(loss / issued * 100, 2) if issued > 0 else None,
            "oil_basis": (
                "Litres issued out of the oil item groups (TransType 60) against "
                "litres received as finished goods (TransType 59), over the plan "
                "period. A yield figure, not a declared one."
            ),
            "oil_assumed_litre_lines": yields["assumed_litre_uom_lines"],
            # PM: what was logged and signed for.
            "pm_logged_value": round(pm_value, 2),
            "rm_logged_value": round(oil_logged_value, 2),
            "logged_unpriced_count": unpriced,
            "pm_logged_qty": round(pm_qty, 3),
            "pm_log_count": len(logs),
            "pm_approved_count": approved,
            "pm_unclassified_count": unclassified,
            "oil_logged_qty": round(oil_logged_qty, 3),
            "pm_basis": (
                "FactoryFlow waste logs classified on the SAP item group. Not "
                "posted to SAP, so BH-WST will not agree."
            ),
            # The same register read day by day, on the run's date.
            "logged_daily": logged_daily,
            "logged_today": self._waste_day(by_day.get(self.today), self.today),
            # The most recent day anything WAS logged against, which is the day
            # the board headlines. Waste is typed in arrears -- on live records
            # only 8 of 795 rows were entered the same day -- so today is almost
            # always empty, and a wall that headlined it would report a clean
            # shift every evening on the strength of missing paperwork. The day
            # is named and its age given, so the figure can never be mistaken
            # for today's.
            "logged_latest": (
                self._waste_day(by_day.get(latest), latest) if latest else None
            ),
            "logged_latest_date": latest.isoformat() if latest else None,
            "logged_days_behind": (self.today - latest).days if latest else None,
            "logged_standalone_count": standalone,
            "logged_basis": (
                "FactoryFlow waste logs, dated by the production run they belong "
                "to rather than by when they were typed, and valued at the "
                "run's own snapshotted purchase price because pieces, kilos and "
                "metres do not add up."
            ),
        }

    def _waste_prices(self, logs: List[Dict[str, Any]]):
        """Two price lookups for the materials that appear in the waste rows.

        The first is keyed on (run, material) and is the one that should hit:
        it is the price the run itself was costed at, snapshotted from SAP
        ``LastPurPrc`` when that run started, so the waste is valued at the same
        rate as the material it came out of. The second is the newest price seen
        for that code anywhere, which stands in for a run whose line carries no
        price at all.

        One query for both, ordered oldest-first so the last write into
        ``last_price`` is the newest one.
        """
        codes = sorted({row["material_code"] for row in logs if row["material_code"]})
        if not codes:
            return {}, {}

        exact: Dict[Any, Any] = {}
        last: Dict[str, Any] = {}
        for line in (
            ProductionMaterialUsage.objects.filter(
                production_run__company__code=self.company_code,
                material_code__in=codes,
            )
            .exclude(unit_price=None)
            .exclude(unit_price=0)
            .order_by("production_run__date")
            .values("production_run_id", "material_code", "unit_price")
        ):
            exact[(line["production_run_id"], line["material_code"])] = line["unit_price"]
            last[line["material_code"]] = line["unit_price"]
        return exact, last

    def _waste_day(self, slot: Optional[Dict[str, Any]], day: date) -> Dict[str, Any]:
        """One day of the waste register: what it cost, and what it was.

        The money is the comparable figure and the one the board shows. The
        quantities ride alongside, each in its own unit and never summed across
        units, so the rupees can always be taken apart into the things that
        were actually spoiled.
        """
        slot = slot or {
            "pm": {}, "rm": {}, "other": {},
            "value": {"pm": 0.0, "rm": 0.0, "other": 0.0},
            "logs": 0, "unpriced": 0,
        }
        value = slot["value"]

        def split(kinds: Dict[str, float], headline_uom: str):
            head = round(kinds.get(headline_uom, 0.0), 3)
            rest = [
                {"uom": uom, "qty": round(qty, 3)}
                for uom, qty in sorted(kinds.items())
                if uom != headline_uom and round(qty, 3)
            ]
            return head, rest

        pm_pieces, pm_other = split(slot["pm"], "PCS")
        rm_litres, rm_other = split(slot["rm"], "LTR")

        return {
            "date": day.isoformat(),
            "logs": slot["logs"],
            # What the day's waste cost, at the run's own material price.
            "pm_value": round(value["pm"], 2),
            "rm_value": round(value["rm"], 2),
            "other_value": round(value["other"], 2),
            "total_value": round(value["pm"] + value["rm"] + value["other"], 2),
            # Rows that could be priced at neither rate. Named, not dropped:
            # the money above is short by exactly these.
            "unpriced": slot["unpriced"],
            # Packing material, almost all of which is counted in pieces.
            "pm_pieces": pm_pieces,
            "pm_other": pm_other,
            # Raw material, which is oil and therefore litres.
            "rm_litres": rm_litres,
            "rm_other": rm_other,
        }

    def _waste_daily(
        self, by_day: Dict[date, Dict[str, Any]], window_from: date, window_to: date
    ) -> List[Dict[str, Any]]:
        """The trailing week, every day present.

        A day nobody logged against is a zero rather than a missing entry, for
        the same reason the blowing trend fills its gaps: a wall reads the shape
        of a week, and a week with days removed has the wrong shape.
        """
        first = max(window_from, window_to - timedelta(days=TREND_DAYS - 1))
        out: List[Dict[str, Any]] = []
        day = first
        while day <= window_to:
            out.append(self._waste_day(by_day.get(day), day))
            day += timedelta(days=1)
        return out

    # ------------------------------------------------------------------
    # Band 4: Shifting
    # ------------------------------------------------------------------

    def _shifting(self) -> Dict[str, Any]:
        """What the keeper declared would leave the floor today, and what did.

        TWO REGISTERS, ONE PAIR OF ROWS, AND THEY ARE NEVER NETTED.

        * **Allocated** is ``warehouse.PFStockMovement`` -- the Godown Stock
          Movements page. It is a DECLARATION: the keeper types what he is
          sending and where, often the evening before. Nothing else in the app
          records that decision, which is the whole reason the page exists.
        * **Shipped** is the BST register -- boxes physically scanned onto a
          transfer and that transfer dispatched. It is the fact.

        A declaration and a posting answer different questions and the gap
        between them is usually just the hours in between, so the band shows
        both down the same three routes and subtracts neither from the other.
        A reader compares them by eye; a variance would invent a problem on
        every morning of every day.

        SAP'S OWN JOURNAL IS DELIBERATELY NOT HERE. It learns of a transfer when
        the document is posted, which is after the fact and often the next
        morning -- too late for a board whose whole claim is "today".
        """
        return {
            "allocated": self._declared(),
            "shipped": self._bst_shipped(),
            "basis": (
                "Allocated is what the godown keeper declared on the Godown "
                "Stock Movements page; shipped is what the BST register says "
                "was scanned and dispatched. Retracted declarations and "
                "cancelled transfers are excluded from each. Tonnage is "
                f"{LITRES_PER_TON} L = 1 t."
            ),
        }

    def _declared(self) -> Dict[str, Any]:
        """Today's declared consignments off the floor, by destination.

        SCOPED ON THE SOURCE COMPANY, NOT THE DESTINATION. The Gupta godown the
        floor ships into belongs to Mart while the floor is Oil, which is why
        the register stores a destination company beside the warehouse code.
        Filtering on the destination would drop every Gupta-bound load. That
        cross-company fact is also why Gupta is no longer a godown row of its
        own: a load into it IS the sale to Mart, and the Dispatch row is where
        a sale belongs.

        NO SAP CALL AT ALL. The register snapshots ``SalPackUn`` and
        ``SalFactor2`` onto each line as it is typed, precisely so a declaration
        cannot be silently restated by an item master that moved on. That makes
        this the one figure on the board that is immune to a HANA outage even in
        its unit.

        RETRACTED DECLARATIONS ARE EXCLUDED, AND COUNTED. The register
        deactivates rather than deletes, because a withdrawn declaration is
        itself a fact: it is the difference between a keeper who planned nothing
        and one who changed his mind.
        """
        movements = PFStockMovement.objects.filter(
            company__code=self.company_code,
            from_warehouse=PRODUCTION_FLOOR,
            movement_date=self.today,
        ).prefetch_related("lines")

        rows: List[Dict[str, Any]] = []
        live = 0
        retracted = 0
        retracted_pieces = 0.0

        for movement in movements:
            route = (
                SHIFTING_DISPATCH
                if movement.is_dispatch
                else (movement.to_warehouse or "").strip() or SHIFTING_ELSEWHERE
            )
            if not movement.is_active:
                retracted += 1
                retracted_pieces += sum(int(line.pieces or 0) for line in movement.lines.all())
                continue
            live += 1
            for line in movement.lines.all():
                pieces = int(line.pieces or 0)
                rows.append(
                    {
                        "route": route,
                        "pieces": float(pieces),
                        # None, not zero: a carton is not zero litres, it is not
                        # measured in litres. The register is careful about this
                        # and the board must not undo it.
                        "litres": None if line.litres is None else float(line.litres),
                        "boxes": line.full_boxes or 0,
                        "item_code": line.item_code,
                        "movement": movement.pk,
                    }
                )

        totals = self._fold_routes(rows, weighed=True, routes=SHIFTING_DECLARED_ROUTES)
        totals["transfers"] = live
        # Only this register can report these, because only this register keeps
        # what was withdrawn.
        totals["retracted_movements"] = retracted
        totals["retracted_pieces"] = round(retracted_pieces, 2)
        return totals

    def _bst_scans(self):
        """Every box scan that could belong to this band, before the date test.

        Scoped on the SOURCE warehouse and the SOURCE company. BH-BT ships to
        Mart as well, and Mart runs its own BSTs, so without both filters this
        band would report somebody else's day.
        """
        return (
            BSTBoxScan.objects.filter(
                transfer__company__code=self.company_code,
                transfer__sap_from_warehouse=PRODUCTION_FLOOR,
            )
            .exclude(transfer__status=BSTTransferStatus.CANCELLED)
        )

    def _bst_shipped(self) -> Dict[str, Any]:
        """Boxes on a transfer dispatched today, by where they went.

        Dated on the DISPATCH stamp rather than on the scan, so a shipment
        loaded last night and sent this morning is today's. The two tiles
        therefore need not agree, and the day they do is the day nothing was
        left standing.

        Rejected boxes are counted in, and disclosed separately. A box the
        destination refused did physically leave this floor; whether it stays
        gone is the receiving godown's question, not this band's.
        """
        scans = self._bst_scans().filter(transfer__dispatched_at__date=self.today)
        totals = self._bst_by_route(scans)
        rejected = self._bst_by_route(
            scans.filter(receive_status=BSTReceiveStatus.REJECTED)
        )
        totals["rejected_pieces"] = rejected["total_pieces"]
        totals["rejected_tons"] = rejected["total_tons"]
        return totals

    def _bst_by_route(self, scans) -> Dict[str, Any]:
        """Fold box scans into the band's routes, in pieces and tonnes.

        Grouped in the DATABASE by route and item code, not in Python over the
        rows: a busy day is several thousand boxes and this board re-reads every
        minute. The litre lookup is then one SAP round trip for the distinct
        item codes, whatever the box count.

        TONNAGE IS NOT DERIVED FROM THE BOX. A scan stores pieces -- a "5 LTR 4
        PCS" box carries quantity 4 -- so litres come from the item master,
        gated on ``U_IsLitre``. Unlike the declaration register, BST snapshots no
        volume of its own, which is why this half needs SAP and the other does
        not.
        """
        grouped = list(
            scans.values(
                "transfer__sap_to_warehouse",
                "transfer__source_type",
                "item_code",
            ).annotate(pieces=Sum("quantity"), boxes=Count("id"))
        )

        # THE BAND MUST SURVIVE A HANA OUTAGE, BECAUSE ITS REGISTER DOES.
        # Every box scan here is Postgres; only the litres are SAP's. If SAP
        # cannot answer, the tonnage is withheld -- reported as null, never as
        # zero -- and the pieces are still read, which is what the floor counted
        # anyway. Losing a unit is a smaller loss than losing the band.
        if self._sap_down:
            # Already established this refresh. Asking again costs fifteen
            # seconds and tells nobody anything new.
            litres_per_piece, weighed = {}, False
        else:
            try:
                litres_per_piece = self.reader.litres_per_piece(
                    [row["item_code"] for row in grouped]
                )
                weighed = True
            except Exception as exc:  # noqa: BLE001 - see above
                if _is_sap_unreachable(exc):
                    self._sap_down = True
                logger.warning("plant_board: shifting tonnage unavailable: %s", exc)
                litres_per_piece, weighed = {}, False

        rows = []
        for row in grouped:
            pieces = _f(row["pieces"])
            per_piece = litres_per_piece.get(row["item_code"], 0.0)
            rows.append(
                {
                    "route": self._bst_route(
                        row["transfer__sap_to_warehouse"], row["transfer__source_type"]
                    ),
                    "pieces": pieces,
                    "litres": (pieces * per_piece) if per_piece else None,
                    "boxes": int(row["boxes"] or 0),
                    "item_code": row["item_code"],
                }
            )

        totals = self._fold_routes(rows, weighed=weighed)
        totals["transfers"] = scans.values("transfer_id").distinct().count()
        return totals

    def _fold_routes(self, rows, weighed: bool = True, routes=None) -> Dict[str, Any]:
        """Put line-level rows onto the band's routes, in a fixed order.

        Shared by both halves so the two tiles cannot drift apart in shape.
        Each row carries ``route``, ``pieces``, ``litres`` (None where the item
        has no litre volume), ``boxes`` and ``item_code``.

        THE NAMED ROUTES ARE ALWAYS PRESENT, AT ZERO IF NEED BE. The wall reads
        the same rows down both tiles, so a row that vanishes on a quiet day
        shuffles the ones below it and the comparison the band exists for stops
        working.

        WHICH ROUTES ARE NAMED DIFFERS BY HALF, AND ON PURPOSE. Both tiles carry
        the bottling floor; only the shipped half carries a dispatch column,
        because a sale off this floor is raised as an invoice and reaches the
        board through BST. Anything not named -- a dispatch the keeper did
        declare, a godown nobody expected, a load still booked to the retired
        ``GP-FG`` code -- folds into one Elsewhere row that appears only when it
        carries something and prints the codes it folded, so nothing is dropped
        and no row stands at zero all year teaching a reader to skip it.
        """
        named = list(routes or SHIFTING_ROUTES)
        def blank(route: str) -> Dict[str, Any]:
            return {
                "route": route,
                "name": SHIFTING_ROUTE_NAMES.get(route, route),
                "is_dispatch": route == SHIFTING_DISPATCH,
                "pieces": 0.0,
                "litres": 0.0,
                "boxes": 0,
                "items": set(),
            }

        buckets: Dict[str, Dict[str, Any]] = {route: blank(route) for route in named}
        unweighed: set = set()

        for row in rows:
            slot = buckets.setdefault(row["route"], blank(row["route"]))
            pieces = _f(row["pieces"])
            slot["pieces"] += pieces
            slot["boxes"] += int(row.get("boxes") or 0)
            slot["items"].add(row["item_code"])
            if row["litres"] is None:
                if weighed and pieces > 0:
                    unweighed.add(row["item_code"])
            else:
                slot["litres"] += float(row["litres"])

        ordered = [buckets[route] for route in named]
        extra = [
            slot
            for key, slot in buckets.items()
            if key not in named and slot["pieces"] > 0
        ]
        if extra:
            ordered.append(
                {
                    "route": SHIFTING_ELSEWHERE,
                    "name": SHIFTING_ROUTE_NAMES[SHIFTING_ELSEWHERE],
                    "is_dispatch": False,
                    "pieces": sum(slot["pieces"] for slot in extra),
                    "litres": sum(slot["litres"] for slot in extra),
                    "boxes": sum(slot["boxes"] for slot in extra),
                    "items": {code for slot in extra for code in slot["items"]},
                    "codes": sorted({slot["route"] for slot in extra}),
                }
            )

        out = []
        for slot in ordered:
            out.append(
                {
                    **{k: v for k, v in slot.items() if k != "items"},
                    "pieces": round(slot["pieces"], 2),
                    "litres": round(slot["litres"], 3) if weighed else None,
                    "tons": _tons(slot["litres"]) if weighed else None,
                    "item_count": len(slot["items"]),
                }
            )

        total_litres = sum(slot["litres"] for slot in ordered)
        return {
            "boxes": sum(row["boxes"] for row in out),
            "total_pieces": round(sum(slot["pieces"] for slot in ordered), 2),
            "total_tons": _tons(total_litres) if weighed else None,
            # False only when SAP could not be reached. The tile then leads with
            # pieces and says why, rather than printing a zero tonnage that
            # looks like a quiet day.
            "tonnage_available": weighed,
            "routes": out,
            # SKUs the tonnage cannot speak for, so a reader knows the tons are
            # a floor rather than the whole truth.
            "unweighed_items": len(unweighed),
        }

    @staticmethod
    def _bst_route(to_warehouse: str, source_type: str) -> str:
        """Which of the band's three columns one transfer belongs in.

        An INVOICE-sourced BST is a cross-company sale, so it has no
        destination warehouse and never will: that stock left the company, and
        Dispatch is exactly what that means. A stock transfer carries its
        destination warehouse, which is the column.
        """
        if source_type == BSTSourceType.INVOICE:
            return SHIFTING_DISPATCH
        return (to_warehouse or "").strip() or SHIFTING_ELSEWHERE
