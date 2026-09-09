"""Is tomorrow's production plan actually runnable?

The Start Production Run screen used to be a same-day form: the supervisor stood
at the line, typed what was already happening, and pressed Start. It is now
filled the evening before, which changes the question it has to answer. Nobody
plans a run to find out at 6 a.m. that the caps are in another warehouse or that
the second shift already claimed the same oil.

So this module answers three things about a proposed plan, before it is saved:

**How much does it need?** The BOM's `ITT1."Quantity"` is authored **per box**
on this data — `FG0000121 CANOLA OIL 1 LTR 20 PCS` lists 20 litres of oil, 20
bottles, 20 caps and *one* carton — so the requirement is that quantity times the
number of boxes planned, full stop. `OITT."Qauntity"` (the BOM's base quantity)
is deliberately **not** divided by: it is inconsistent master data, set to the
piece count on some items (4 on the 5 LTR 4 PCS, 20 on the 1 LTR 20 PCS) and to
1 on others whose children are per-box all the same. Dividing by it yields a
*per-piece* rate, and multiplying that by a *case* count understates every line
by the pieces-per-box — 200 litres of oil where 4,000 are needed.

**Is the material there?** Two different answers, because raw material and
packing material are counted by different people.

*Packing material* is priced against real SAP stock — `OITW` on-hand and
committed — scoped the way the Stock Benchmark and Planning & Purchase screens
scope it (packaging from the packaging stores, wastage never counted). A number
here and a number there must not disagree, so the stock read is the *same*
reader those screens use, not a second query with its own opinion.

*Raw material* is not read from SAP at all. It comes from the Raw Material
register — `warehouse.RawMaterialStock` — where the store keeper types what is
actually in the tank. SAP and the floor diverge badly on bulk oil (SAP carried
143.846 litres of `RM0000002` on a day the keeper registered 32,000), and for
planning tomorrow's run it is the keeper's figure that decides whether the line
can run. An item with no register row reads **zero**, not "unknown": nobody has
said it is there, so the plan may not assume it. SAP's figure still travels on
the row, quietly, so a register that has gone stale is visible rather than
silently authoritative.

**Is another plan already spending it?** SAP's `IsCommited` knows about SAP
documents. It does not know that the supervisor planned two runs on this app
half an hour apart that both draw on the same 40,000 caps. That contention lives
in this database, so it is computed here: every other planned or running run's
outstanding requirement for each component, netted of what the warehouse has
already issued to it — issued material has physically left the store and is
already out of `OnHand`, so counting it again would invent a shortage.

**Does the plan collide with the rest of the day?** One line cannot run two SKUs
at once, and the same SKU planned twice on one date is almost always a typo.
Machines are not checked: every line here permanently carries the machines it
needs, so "which machines" is a property of the line and never a choice a plan
makes.

Nothing here blocks a save. A supervisor planning against a GRN that lands at
5 a.m. is doing their job, and a screen that refuses them sends them back to
paper. Every finding comes back as a warning with the evidence attached — the
competing run is named, the shortfall is quantified, the open purchase order that
covers it is shown — and `create_run` only insists on a written reason.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence

from django.db.models import Q, Sum
from django.utils import timezone

from warehouse.services import approval_scope

from ..models import (
    ProductionLine,
    ProductionMaterialUsage,
    ProductionRun,
    RunStatus,
)

# Where the RM figure comes from. `REGISTER` is the store keeper's own count;
# `SAP` is `OITW`. Never mix them in one column without saying which is which.
SOURCE_REGISTER = 'REGISTER'
SOURCE_SAP = 'SAP'

logger = logging.getLogger(__name__)

ZERO = Decimal(0)

# A component's readiness, worst last. The screen sorts on this.
STATUS_OK = 'OK'
STATUS_UNKNOWN = 'UNKNOWN'
STATUS_NO_STOCK_RECORD = 'NO_STOCK_RECORD'
STATUS_TIGHT = 'TIGHT'
STATUS_CONTESTED = 'CONTESTED'
STATUS_SHORT = 'SHORT'

# `UNKNOWN` sits just above `OK` deliberately. It means the stock read failed,
# which is not evidence of a shortage — an unreachable HANA must never present
# itself as an empty warehouse.
STATUS_SEVERITY = {
    STATUS_OK: 0,
    STATUS_UNKNOWN: 1,
    STATUS_TIGHT: 2,
    STATUS_CONTESTED: 3,
    STATUS_NO_STOCK_RECORD: 4,
    STATUS_SHORT: 5,
}

# Conflict kinds.
CONFLICT_LINE_BUSY = 'LINE_BUSY'
CONFLICT_DUPLICATE_SKU = 'DUPLICATE_SKU'
CONFLICT_MATERIAL_CONTENTION = 'MATERIAL_CONTENTION'

# A run is a claim on the factory while it is planned or running. COMPLETED runs
# have already consumed what they consumed and are out of the picture.
ACTIVE_RUN_STATUSES = (RunStatus.DRAFT, RunStatus.IN_PROGRESS)


def _dec(value, default=ZERO) -> Decimal:
    if value is None or value == '':
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _num(value) -> Optional[float]:
    """Decimals go over the wire as floats — the client only formats them."""
    if value is None:
        return None
    return float(value)


def _as_date(value) -> date_cls:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_cls):
        return value
    if value:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    return timezone.localdate()


def compute_planned_window(
    planned_start_at: Optional[datetime],
    required_qty,
    pieces_per_case,
    rated_speed,
) -> Dict[str, Any]:
    """How long the plan should take, from the speed the line is rated at.

    `rated_speed` is bottles per hour and production is entered in cases, so the
    case count has to be converted before dividing — the conversion the whole
    module does through `pieces_per_case` (SAP `OITM.SalFactor2`). Any of the
    three inputs missing means the finish time is genuinely unknown, and the
    caller is told which one is missing rather than being handed a made-up
    window; a plan whose end time is a guess is worse than one with no end time,
    because the clash check would then trust it.
    """
    qty = _dec(required_qty)
    per_case = _dec(pieces_per_case)
    speed = _dec(rated_speed)

    missing: List[str] = []
    if qty <= ZERO:
        missing.append('required quantity')
    if per_case <= ZERO:
        missing.append('bottles per case')
    if speed <= ZERO:
        missing.append('rated speed')

    bottles = qty * per_case if qty > ZERO and per_case > ZERO else None

    if missing:
        return {
            'planned_end_at': None,
            'duration_minutes': None,
            'bottles': _num(bottles),
            'derived': False,
            'undecidable_because': missing,
        }

    minutes = int((bottles / speed * Decimal(60)).to_integral_value(rounding='ROUND_CEILING'))
    return {
        'planned_end_at': (
            planned_start_at + timedelta(minutes=minutes) if planned_start_at else None
        ),
        'duration_minutes': minutes,
        'bottles': _num(bottles),
        'derived': True,
        'undecidable_because': [],
    }


def scope_raw() -> str:
    """The material-type token planning_purchase uses for raw material."""
    from planning_purchase.services import warehouse_scope as scope
    return scope.RAW


def _windows_overlap(a_start, a_end, b_start, b_end) -> bool:
    """Half-open overlap, so a plan starting exactly when another ends is fine.

    A back-to-back changeover is the normal way a line is scheduled, and calling
    it a clash would make the warning worthless.
    """
    if not (a_start and a_end and b_start and b_end):
        return False
    return a_start < b_end and b_start < a_end


class ProductionPlanCheckService:
    """Material readiness and clash detection for a proposed production run.

    The SAP readers are injectable so the checks can be tested without HANA;
    both are built lazily, because a plan whose SAP side fails must still come
    back with its database-side clashes rather than a 500.
    """

    def __init__(self, company_code: str, plan_reader=None, item_reader=None):
        self.company_code = company_code
        self._company = None
        self._plan_reader = plan_reader
        self._item_reader = item_reader

    # ------------------------------------------------------------------
    # Lazy dependencies
    # ------------------------------------------------------------------

    @property
    def company(self):
        if self._company is None:
            from company.models import Company
            self._company = Company.objects.get(code=self.company_code)
        return self._company

    @property
    def plan_reader(self):
        """Planning & Purchase's HANA reader — the BOM and stock source of truth."""
        if self._plan_reader is None:
            from planning_purchase.hana_reader import HanaProductionPlanReader
            from sap_client.context import CompanyContext
            self._plan_reader = HanaProductionPlanReader(CompanyContext(self.company_code))
        return self._plan_reader

    @property
    def item_reader(self):
        if self._item_reader is None:
            from .sap_reader import ProductionOrderReader
            self._item_reader = ProductionOrderReader(self.company_code)
        return self._item_reader

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def check(
        self,
        *,
        line_id: Optional[int] = None,
        item_code: str = '',
        required_qty=None,
        date=None,
        planned_start_at: Optional[datetime] = None,
        planned_end_at: Optional[datetime] = None,
        planned_end_is_manual: bool = False,
        rated_speed=None,
        pieces_per_case=None,
        exclude_run_id: Optional[int] = None,
        stock_basis: Optional[str] = None,
        requirement_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from planning_purchase.services.producible import BASIS_ON_HAND

        plan_date = _as_date(date)
        item_code = (item_code or '').strip()
        basis = stock_basis or BASIS_ON_HAND

        line = (
            ProductionLine.objects.filter(id=line_id, company=self.company).first()
            if line_id else None
        )

        timing = self._timing(
            planned_start_at=planned_start_at,
            planned_end_at=planned_end_at,
            planned_end_is_manual=planned_end_is_manual,
            required_qty=required_qty,
            pieces_per_case=pieces_per_case,
            rated_speed=rated_speed,
            item_code=item_code,
        )

        competitors = self._competing_runs(plan_date, exclude_run_id)

        materials = self._materials(
            item_code=item_code,
            required_qty=required_qty,
            competitors=competitors,
            basis=basis,
            requirement_override=requirement_override,
        )

        conflicts = self._conflicts(
            line=line,
            item_code=item_code,
            plan_date=plan_date,
            window_start=timing['planned_start_at'],
            window_end=timing['planned_end_at'],
            competitors=competitors,
            material_rows=materials['rows'],
        )

        return {
            'timing': timing,
            'materials': materials,
            'conflicts': conflicts,
            'blocking': {
                # Not "blocking" in the sense of refusing the save — it is what
                # the supervisor has to write a reason for.
                'has_shortage': materials['summary']['short_lines'] > 0,
                'has_contention': materials['summary']['contested_lines'] > 0,
                'has_conflicts': len(conflicts) > 0,
                'requires_remark': (
                    materials['summary']['short_lines'] > 0
                    or materials['summary']['contested_lines'] > 0
                    or len(conflicts) > 0
                ),
            },
            'meta': {
                'company_code': self.company_code,
                'date': plan_date.isoformat(),
                'line_id': line.id if line else None,
                'line_name': line.name if line else '',
                'item_code': item_code,
                'stock_basis': basis,
                'checked_at': timezone.now().isoformat(),
            },
        }

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    def _timing(
        self,
        *,
        planned_start_at,
        planned_end_at,
        planned_end_is_manual,
        required_qty,
        pieces_per_case,
        rated_speed,
        item_code,
    ) -> Dict[str, Any]:
        per_case = _dec(pieces_per_case)
        if per_case <= ZERO and item_code:
            per_case = _dec(self._resolve_pieces_per_case(item_code))

        window = compute_planned_window(
            planned_start_at, required_qty, per_case, rated_speed
        )

        derived_end = window['planned_end_at']
        # A finish time the supervisor typed wins over the derived one, but the
        # derived one still travels so the screen can show the gap between what
        # the line is rated to do and what was promised.
        effective_end = planned_end_at if (planned_end_is_manual and planned_end_at) else derived_end

        manual_minutes = None
        if planned_start_at and planned_end_at:
            manual_minutes = int((planned_end_at - planned_start_at).total_seconds() // 60)

        return {
            'planned_start_at': planned_start_at,
            'planned_end_at': effective_end,
            'derived_end_at': derived_end,
            'planned_end_is_manual': bool(planned_end_is_manual and planned_end_at),
            'duration_minutes': (
                manual_minutes if (planned_end_is_manual and manual_minutes is not None)
                else window['duration_minutes']
            ),
            'derived_duration_minutes': window['duration_minutes'],
            'bottles': window['bottles'],
            'pieces_per_case': _num(per_case) if per_case > ZERO else None,
            'rated_speed': _num(_dec(rated_speed)) if _dec(rated_speed) > ZERO else None,
            'undecidable_because': window['undecidable_because'],
        }

    def _resolve_pieces_per_case(self, item_code: str):
        try:
            return self.item_reader.get_pieces_per_case_map([item_code]).get(item_code)
        except Exception as e:  # noqa: BLE001 — SAP is optional for the timing hint
            logger.info("plan check: pieces_per_case lookup failed for %s: %s", item_code, e)
            return None

    # ------------------------------------------------------------------
    # Competing runs (this database, not SAP)
    # ------------------------------------------------------------------

    def _competing_runs(
        self, plan_date: date_cls, exclude_run_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        """Every run that still has a claim on the factory on the planned day.

        Two groups, and both matter. Runs planned for the same date are the
        obvious ones. A run that is IN_PROGRESS on any date is included too: it
        is on a line right now and still drawing material, and a run left open
        from yesterday evening is exactly the thing an early-morning plan trips
        over.
        """
        runs = (
            ProductionRun.objects
            .filter(company=self.company, status__in=ACTIVE_RUN_STATUSES)
            .filter(Q(date=plan_date) | Q(status=RunStatus.IN_PROGRESS))
            .select_related('line')
        )
        if exclude_run_id:
            runs = runs.exclude(pk=exclude_run_id)
        runs = list(runs)
        if not runs:
            return []

        demand = self._outstanding_demand(runs)

        out: List[Dict[str, Any]] = []
        for run in runs:
            out.append({
                'run': run,
                'run_id': run.id,
                'run_number': run.run_number,
                'date': run.date,
                'status': run.status,
                'line_id': run.line_id,
                'line_name': run.line.name if run.line_id else '',
                'product': run.product,
                'item_code': run.item_code,
                'required_qty': run.required_qty,
                'planned_start_at': run.planned_start_at,
                'planned_end_at': run.planned_end_at,
                'demand': demand.get(run.id, {}),
            })
        return out

    def _outstanding_demand(self, runs: Sequence[ProductionRun]) -> Dict[int, Dict[str, Decimal]]:
        """What each run still has to draw from the warehouse, per component.

        The run's own material lines say what it needs (`opening_qty` — the BOM
        scaled to its quantity when it was planned). The warehouse's BOM request
        lines say how much of that has already been handed over. Only the
        difference is a live claim on stock: once material is issued it is out of
        `OITW."OnHand"`, so counting the issued part again would double-charge
        the shortage and cry wolf on a plan that is actually fine.

        Rejected requests are ignored — nothing was issued against them.
        """
        run_ids = [r.id for r in runs]
        if not run_ids:
            return {}

        needed: Dict[int, Dict[str, Decimal]] = {}
        usage_rows = (
            ProductionMaterialUsage.objects
            .filter(production_run_id__in=run_ids)
            .exclude(material_code='')
            .values('production_run_id', 'material_code')
            .annotate(total=Sum('opening_qty'))
        )
        for row in usage_rows:
            needed.setdefault(row['production_run_id'], {})
            code = row['material_code']
            needed[row['production_run_id']][code] = (
                needed[row['production_run_id']].get(code, ZERO) + _dec(row['total'])
            )

        issued: Dict[int, Dict[str, Decimal]] = {}
        try:
            from warehouse.models import BOMRequestLine, BOMRequestStatus

            issued_rows = (
                BOMRequestLine.objects
                .filter(bom_request__production_run_id__in=run_ids)
                .exclude(bom_request__status=BOMRequestStatus.REJECTED)
                .values('bom_request__production_run_id', 'item_code')
                .annotate(total=Sum('issued_qty'))
            )
            for row in issued_rows:
                run_id = row['bom_request__production_run_id']
                issued.setdefault(run_id, {})
                code = row['item_code']
                issued[run_id][code] = issued[run_id].get(code, ZERO) + _dec(row['total'])
        except Exception as e:  # noqa: BLE001 — never let the warehouse side sink the check
            logger.warning("plan check: could not read issued quantities: %s", e)

        out: Dict[int, Dict[str, Decimal]] = {}
        for run_id, codes in needed.items():
            for code, planned in codes.items():
                remaining = planned - issued.get(run_id, {}).get(code, ZERO)
                if remaining > ZERO:
                    out.setdefault(run_id, {})[code] = remaining
        return out

    # ------------------------------------------------------------------
    # Material readiness
    # ------------------------------------------------------------------

    def _materials(
        self,
        *,
        item_code: str,
        required_qty,
        competitors: Sequence[Dict[str, Any]],
        basis: str,
        requirement_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Readiness per BOM component.

        The requirement is the BOM's per-box quantity times the box count.

        `requirement_override` carries the quantities the supervisor actually
        typed on the screen, keyed by item code. The BOM figure is the starting
        point, but a supervisor who knows this run needs an extra roll of film
        is allowed to say so — and then the shortage has to be judged against
        the figure they will really draw, not the textbook one. The BOM figure
        stays on the row as `bom_required_qty` so the deviation is visible.
        """
        empty_summary = {
            'total_lines': 0, 'ok_lines': 0, 'tight_lines': 0,
            'contested_lines': 0, 'short_lines': 0, 'no_record_lines': 0,
            'status': STATUS_OK, 'approval_lines': 0, 'register_missing_lines': 0,
        }
        if not item_code:
            return {
                'rows': [], 'summary': empty_summary, 'unusable': [],
                'resource_lines': [], 'available': True, 'error': '',
                'warehouses': [], 'warehouse_scope': {}, 'basis': basis,
            }

        try:
            recipe, unusable, resources = self._recipe(item_code)
        except Exception as e:  # noqa: BLE001 — SAP down must not hide the clash check
            logger.warning("plan check: BOM read failed for %s: %s", item_code, e)
            return {
                'rows': [], 'summary': empty_summary, 'unusable': [],
                'resource_lines': [], 'available': False,
                'error': f"Could not read the BOM from SAP: {e}",
                'warehouses': [], 'warehouse_scope': {}, 'basis': basis,
            }

        codes = [c['item_code'] for c in recipe]
        material_types = {c['item_code']: c['material_type'] for c in recipe}

        try:
            stock, warehouses, warehouse_scope = self._stock(codes, material_types, basis)
            stock_error = ''
        except Exception as e:  # noqa: BLE001
            logger.warning("plan check: stock read failed: %s", e)
            stock, warehouses, stock_error = {}, [], f"Could not read stock from SAP: {e}"
            warehouse_scope = {}

        on_order = self._on_order(codes)
        other_demand = self._demand_by_code(competitors, codes)
        register = self._register_stock(
            [c['item_code'] for c in recipe if c['material_type'] == scope_raw()]
        )

        qty = _dec(required_qty)
        overrides = {
            str(code): _dec(value)
            for code, value in (requirement_override or {}).items()
            if code and value not in (None, '')
        }

        rows: List[Dict[str, Any]] = []
        for comp in recipe:
            code = comp['item_code']
            from_bom = comp['qty_per_case'] * qty
            overridden = code in overrides
            required = overrides[code] if overridden else from_bom
            entry = stock.get(code)
            is_raw = comp['material_type'] == scope_raw()
            reg = register.get(code.strip().upper()) if is_raw else None

            sap_on_hand = entry['on_hand'] if entry else ZERO
            sap_committed = entry['committed'] if entry else ZERO

            if is_raw:
                # The register is the answer, and a missing row is a zero.
                source = SOURCE_REGISTER
                on_hand = reg['qty'] if reg else ZERO
                # The register states a quantity, not a reservation — there is
                # no SAP-style committed figure to net off, and inventing one
                # from `OITW` would mix two people's arithmetic in one column.
                committed = None
                free = None
                holdings = reg['warehouses'] if reg else []
            else:
                source = SOURCE_SAP
                on_hand = sap_on_hand
                committed = sap_committed
                free = on_hand - committed
                holdings = (entry or {}).get('warehouses', [])

            claimed = other_demand.get(code, {}).get('qty', ZERO)

            # Shortfall is judged on physical stock: the material is in the
            # building, so the line can physically run it. `free` travels beside
            # it so an over-commitment stays visible instead of being silently
            # chosen for the supervisor.
            shortfall = max(ZERO, required - on_hand)
            after_others = on_hand - claimed - required

            if stock_error and not is_raw:
                # The stock read failed. Every figure below it is unknown, and
                # saying "short" here would turn a HANA outage into a fake
                # material crisis — and would demand a written override for it.
                status = STATUS_UNKNOWN
                on_hand = committed = free = shortfall = None
                after_others = None
            elif is_raw and reg is None and required > ZERO:
                # Not on the register at all — nobody has said this oil is
                # anywhere, which is a different fix from "the tank is low".
                status = STATUS_NO_STOCK_RECORD
            elif entry is None and not is_raw and required > ZERO:
                status = STATUS_NO_STOCK_RECORD
            elif shortfall > ZERO:
                status = STATUS_SHORT
            elif claimed > ZERO and after_others < ZERO:
                status = STATUS_CONTESTED
            elif not is_raw and required > ZERO and free < required:
                status = STATUS_TIGHT
            else:
                status = STATUS_OK

            approval = approval_scope.line_approval(
                comp['material_type'],
                required,
                approval_scope.production_consumption_qty(
                    (entry or {}).get('warehouses', [])
                ),
            )

            arriving = on_order.get(code) or {}
            rows.append({
                'item_code': code,
                'item_name': comp['item_name'],
                'uom': comp['uom'] or (entry or {}).get('uom', ''),
                'material_type': comp['material_type'],
                'item_group': comp['item_group'],
                'issue_warehouse': comp['issue_warehouse'],
                'searched_warehouses': warehouse_scope.get(comp['material_type'], []),
                'has_own_bom': comp['has_own_bom'],
                'qty_per_case': _num(comp['qty_per_case']),
                'bom_base_qty': _num(comp['bom_base_qty']),
                'required_qty': _num(required),
                'bom_required_qty': _num(from_bom),
                'required_is_overridden': overridden and required != from_bom,
                'on_hand': _num(on_hand),
                'committed': _num(committed),
                'free': _num(free),
                'other_plan_demand': _num(claimed),
                'available_after_other_plans': (
                    _num(on_hand - claimed) if on_hand is not None else None
                ),
                'balance_after_this_plan': _num(after_others),
                'shortfall': _num(shortfall),
                'status': status,
                'stock_source': source,
                'register_missing': bool(is_raw and reg is None),
                'register_as_of': (reg or {}).get('as_of_date'),
                # SAP's own numbers travel even on a register-sourced row, so a
                # register nobody has updated in a month is visible.
                'sap_on_hand': _num(sap_on_hand) if entry else None,
                'sap_free': _num(sap_on_hand - sap_committed) if entry else None,
                'approval_required': approval['required'],
                'approval_qty': _num(approval['qty']),
                'approval_reason': approval['reason'],
                'qty_at_production_consumption': _num(
                    approval['from_production_consumption']
                ),
                'warehouses': [
                    {
                        'warehouse': w['warehouse'],
                        'on_hand': _num(w['on_hand']),
                        'committed': _num(w['committed']),
                        'as_of_date': w.get('as_of_date'),
                    }
                    for w in holdings
                ],
                'competing_runs': other_demand.get(code, {}).get('runs', []),
                'on_order_qty': _num(_dec(arriving.get('qty'))) if arriving else None,
                'on_order_earliest_due': arriving.get('earliest_due') if arriving else None,
                'days_since_last_consumption': (entry or {}).get('days_since_last_consumption'),
            })

        rows.sort(key=lambda r: (-STATUS_SEVERITY[r['status']], r['item_code']))

        summary = {
            'total_lines': len(rows),
            'ok_lines': sum(1 for r in rows if r['status'] == STATUS_OK),
            'tight_lines': sum(1 for r in rows if r['status'] == STATUS_TIGHT),
            'contested_lines': sum(1 for r in rows if r['status'] == STATUS_CONTESTED),
            'short_lines': sum(
                1 for r in rows if r['status'] in (STATUS_SHORT, STATUS_NO_STOCK_RECORD)
            ),
            'no_record_lines': sum(1 for r in rows if r['status'] == STATUS_NO_STOCK_RECORD),
            'status': max(
                (r['status'] for r in rows), key=lambda s: STATUS_SEVERITY[s], default=STATUS_OK
            ),
            'approval_lines': sum(1 for r in rows if r['approval_required']),
            'register_missing_lines': sum(1 for r in rows if r['register_missing']),
        }

        return {
            'rows': rows,
            'summary': summary,
            'unusable': unusable,
            'resource_lines': resources,
            'available': not stock_error,
            'error': stock_error,
            'warehouses': warehouses,
            'warehouse_scope': warehouse_scope,
            'basis': basis,
        }

    def _recipe(self, item_code: str):
        """Single-level BOM for one finished good, **per box**.

        `BomQty` is `ITT1."Quantity"` exactly as authored, and on this data that
        is the quantity for one box: `FG0000118 CANOLA OIL 5 LTR 4 PCS` lists 20
        litres of oil, 4 HDPE bottles, 4 caps and 1 carton. Since the planning
        screen's quantity is a box count, multiplying the two is the whole
        calculation.

        The reader also offers `QtyPerUnit` (`ITT1."Quantity"` / the BOM's base
        quantity). That is a per-*piece* rate and is the right figure for
        Planning & Purchase, whose plan lines are in pieces — but it is the wrong
        one here, and the base quantity it divides by is not trustworthy anyway:
        4 on the 5 LTR 4 PCS, 20 on the 1 LTR 20 PCS, and 1 on `FG0000013
        REFINED OIL 1000 MLS` whose children are per-box just the same.

        Resource lines (conversion cost, `LineType` 290) are separated out rather
        than dropped: they are not stock and have no availability, but a screen
        that silently loses two of a BOM's eight lines looks broken.
        """
        from planning_purchase.hana_reader import BOM_LINE_TYPE_ITEM, classify_material

        rows = self.plan_reader.get_bom_components([item_code])
        recipe: List[Dict[str, Any]] = []
        unusable: List[Dict[str, Any]] = []
        resources: List[Dict[str, Any]] = []

        for row in rows:
            name = row.get('ComponentName') or ''
            code = row.get('ComponentCode') or ''
            if int(row.get('LineType') or 0) != BOM_LINE_TYPE_ITEM:
                resources.append({'item_code': code, 'item_name': name})
                continue

            per_box = row.get('BomQty')
            if per_box is None:
                # A component line with no quantity at all is corrupt master
                # data. Say so rather than quietly requiring nothing of it.
                unusable.append({
                    'item_code': code,
                    'item_name': name,
                    'reason': 'This BOM line has no quantity in SAP — '
                              'its requirement cannot be worked out.',
                })
                continue

            recipe.append({
                'item_code': code,
                'item_name': name,
                'qty_per_case': _dec(per_box),
                'bom_base_qty': _dec(row.get('BomBaseQty')),
                'uom': row.get('Uom') or '',
                'item_group': row.get('ItemGroup') or '',
                'material_type': classify_material(row.get('ItemGroup')),
                'issue_warehouse': row.get('IssueWarehouse') or '',
                'has_own_bom': bool(row.get('HasOwnBom')),
            })
        return recipe, unusable, resources

    def _stock(self, codes: Sequence[str], material_types: Dict[str, str], basis: str):
        """On-hand and committed per component, scoped per kind of material.

        Deliberately the same scope Planning & Purchase uses: raw material from
        the oil stores, packaging from the packaging stores, and `BH-WST` — scrap
        and rejected material — never counted as runnable stock.
        """
        from planning_purchase.services import warehouse_scope as scope
        from planning_purchase.services.producible import EXCLUDED_WAREHOUSES

        warehouses = scope.all_scoped_warehouses()
        by_type = scope.scope_by_material_type()
        rows = self.plan_reader.get_item_stock(codes, warehouses) if codes else []

        by_code: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            whs = row.get('WhsCode') or ''
            if whs in EXCLUDED_WAREHOUSES:
                continue
            code = row.get('ItemCode') or ''
            if not scope.counts(material_types.get(code, scope.OTHER), whs, None):
                continue
            entry = by_code.setdefault(code, {
                'on_hand': ZERO, 'committed': ZERO, 'warehouses': [],
                'uom': row.get('Uom') or '',
                'days_since_last_consumption': row.get('DaysSinceLastConsumption'),
            })
            on_hand = _dec(row.get('OnHand'))
            entry['on_hand'] += on_hand
            entry['committed'] += _dec(row.get('Committed'))
            if on_hand != ZERO:
                entry['warehouses'].append({
                    'warehouse': whs,
                    'on_hand': on_hand,
                    'committed': _dec(row.get('Committed')),
                })

        for entry in by_code.values():
            entry['warehouses'].sort(key=lambda w: -w['on_hand'])
        return by_code, warehouses, by_type

    def _register_stock(self, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """What the store keeper says is on the floor, per raw-material item.

        Every active register row for the item counts, whichever warehouse it
        was entered under. The register is hand-curated — a keeper who typed a
        quantity against a store meant it — so filtering it through a warehouse
        scope would silently zero a figure somebody deliberately recorded.

        A code absent from the register is absent from this map, and the caller
        turns that into a zero with a distinct status. That distinction matters:
        "the keeper says none" and "nobody has said anything" both stop the run,
        but only one of them is fixed by going to look in the tank.
        """
        if not codes:
            return {}

        from warehouse.models_rm_stock import RawMaterialStock

        wanted = {str(code).strip().upper() for code in codes if code}
        rows = RawMaterialStock.objects.filter(
            company=self.company, is_active=True, item_code__in=wanted,
        ).order_by('warehouse_code')

        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            entry = out.setdefault(row.item_code, {
                'qty': ZERO, 'warehouses': [], 'uom': row.uom or '',
                'as_of_date': None,
            })
            entry['qty'] += _dec(row.qty)
            entry['warehouses'].append({
                'warehouse': row.warehouse_code,
                'on_hand': _dec(row.qty),
                'committed': None,
                'as_of_date': row.as_of_date.isoformat() if row.as_of_date else None,
            })
            # The oldest count in the set is the one that dates the figure: a
            # total is only as fresh as its stalest part.
            if row.as_of_date and (
                entry['as_of_date'] is None or row.as_of_date.isoformat() < entry['as_of_date']
            ):
                entry['as_of_date'] = row.as_of_date.isoformat()

        for entry in out.values():
            entry['warehouses'].sort(key=lambda w: -w['on_hand'])
        return out

    def _on_order(self, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Quantity already on open purchase orders, with the nearest due date.

        A shortfall that a PO covers by tomorrow morning is a different decision
        from one with nothing behind it, so the plan says which it is.
        """
        if not codes:
            return {}
        try:
            rows = self.plan_reader.get_open_purchase_qty(codes)
        except Exception as e:  # noqa: BLE001 — a nice-to-have, never a blocker
            logger.info("plan check: open purchase read failed: %s", e)
            return {}

        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            due = row.get('EarliestDue')
            out[row.get('ItemCode')] = {
                'qty': _dec(row.get('OpenQty')),
                'earliest_due': due.isoformat() if hasattr(due, 'isoformat') else (due or None),
            }
        return out

    def _demand_by_code(
        self, competitors: Sequence[Dict[str, Any]], codes: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]:
        wanted = set(codes)
        out: Dict[str, Dict[str, Any]] = {}
        for comp in competitors:
            for code, qty in (comp['demand'] or {}).items():
                if code not in wanted:
                    continue
                entry = out.setdefault(code, {'qty': ZERO, 'runs': []})
                entry['qty'] += qty
                entry['runs'].append({
                    'run_id': comp['run_id'],
                    'run_number': comp['run_number'],
                    'date': comp['date'].isoformat() if comp['date'] else None,
                    'status': comp['status'],
                    'line_name': comp['line_name'],
                    'product': comp['product'],
                    'qty': _num(qty),
                    'planned_start_at': (
                        comp['planned_start_at'].isoformat()
                        if comp['planned_start_at'] else None
                    ),
                })
        for entry in out.values():
            entry['runs'].sort(key=lambda r: -(r['qty'] or 0))
        return out

    # ------------------------------------------------------------------
    # Clashes
    # ------------------------------------------------------------------

    def _conflicts(
        self,
        *,
        line: Optional[ProductionLine],
        item_code: str,
        plan_date: date_cls,
        window_start,
        window_end,
        competitors: Sequence[Dict[str, Any]],
        material_rows: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        conflicts: List[Dict[str, Any]] = []
        own_window_known = bool(window_start and window_end)

        for comp in competitors:
            overlap = _windows_overlap(
                window_start, window_end, comp['planned_start_at'], comp['planned_end_at']
            )
            # Overlap can only be proved when BOTH windows are known. When
            # either side has no times — this plan's start not typed yet, or the
            # other run planned without one — the two cannot be compared, and
            # that is reported rather than hidden: a line with something else on
            # it is the supervisor's problem either way, and silence would read
            # as "the line is free".
            window_unknown = not (
                own_window_known and comp['planned_start_at'] and comp['planned_end_at']
            )
            same_day = comp['date'] == plan_date
            running = comp['status'] == RunStatus.IN_PROGRESS

            if line and comp['line_id'] == line.id and (overlap or window_unknown or running):
                conflicts.append({
                    'type': CONFLICT_LINE_BUSY,
                    'severity': 'WARNING',
                    'run_id': comp['run_id'],
                    'run_number': comp['run_number'],
                    'run_status': comp['status'],
                    'window_overlap': overlap,
                    'window_unknown': window_unknown and not overlap,
                    'message': self._line_busy_message(line, comp, overlap, running),
                    'detail': {
                        'line_name': line.name,
                        'product': comp['product'],
                        'planned_start_at': (
                            comp['planned_start_at'].isoformat()
                            if comp['planned_start_at'] else None
                        ),
                        'planned_end_at': (
                            comp['planned_end_at'].isoformat()
                            if comp['planned_end_at'] else None
                        ),
                    },
                })

            if item_code and comp['item_code'] == item_code and same_day:
                conflicts.append({
                    'type': CONFLICT_DUPLICATE_SKU,
                    'severity': 'WARNING',
                    'run_id': comp['run_id'],
                    'run_number': comp['run_number'],
                    'run_status': comp['status'],
                    'message': (
                        f"{item_code} is already planned on {plan_date.isoformat()} as "
                        f"run #{comp['run_number']} on {comp['line_name'] or 'no line'} "
                        f"({comp['required_qty'] or 0} cases)."
                    ),
                    'detail': {
                        'product': comp['product'],
                        'required_qty': _num(comp['required_qty']),
                    },
                })

        for row in material_rows:
            if not row['competing_runs']:
                continue
            if row['status'] not in (STATUS_CONTESTED, STATUS_SHORT):
                continue
            names = ', '.join(f"#{r['run_number']}" for r in row['competing_runs'][:3])
            more = len(row['competing_runs']) - 3
            if more > 0:
                names += f" and {more} more"
            conflicts.append({
                'type': CONFLICT_MATERIAL_CONTENTION,
                'severity': 'WARNING',
                'item_code': row['item_code'],
                'item_name': row['item_name'],
                'message': (
                    f"{row['item_code']} — {row['item_name']}: this plan needs "
                    f"{row['required_qty']:,.3f} {row['uom']}, other plans ({names}) still "
                    f"need {row['other_plan_demand']:,.3f}, and only {row['on_hand']:,.3f} "
                    f"is in stock."
                ),
                'detail': {
                    'required_qty': row['required_qty'],
                    'other_plan_demand': row['other_plan_demand'],
                    'on_hand': row['on_hand'],
                    'balance_after_this_plan': row['balance_after_this_plan'],
                    'competing_runs': row['competing_runs'],
                },
            })

        return conflicts

    def _line_busy_message(self, line, comp, overlap: bool, running: bool) -> str:
        if running:
            return (
                f"{line.name} still has run #{comp['run_number']} open "
                f"({comp['product'] or comp['item_code'] or 'no product'}), started "
                f"{comp['date'].isoformat()} and not yet completed."
            )
        if overlap:
            start = timezone.localtime(comp['planned_start_at']).strftime('%H:%M')
            end = timezone.localtime(comp['planned_end_at']).strftime('%H:%M')
            return (
                f"{line.name} is already booked {start}–{end} by run "
                f"#{comp['run_number']} ({comp['product'] or comp['item_code']})."
            )
        return (
            f"{line.name} already has run #{comp['run_number']} planned on "
            f"{comp['date'].isoformat()}, and the two windows cannot be compared "
            f"because one of them has no start and finish time."
        )
