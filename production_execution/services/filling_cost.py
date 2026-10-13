"""What a new filling cost sheet opens with: Beverages' sheet, worked out.

The factory writes the sheet by hand, a shift at a time. This fills it in
from what the app already records, head by head, so the sheet is checked
rather than typed:

========================  ==================================================
Cases                     the shift's production-run cases
Electricity               Beverages' Electricity++ cost for the day, less
                          the ETP meter (effluent treatment, not filling)
                          and the KWH main; a shift takes each meter's part
                          by that meter's own day and night readings, or by
                          its share of the day's cases where the meter was
                          read only once
Fixed Manpower,           the Cost Master's monthly rate over 26 working days,
Maintenance, Lab, Misc,   by the shift's share of the day's running hours
Ground Water Extraction,
AMC, Servicing Cost
Batch Coding              bottles (cases x bottles per case) x rate a bottle
Lubrication               litres (bottles x litres a bottle) x rate a litre
Wastage                   what SAP's books moved into BH-WST for the day: the
                          store's stock transfers from BH-PP, matched to the
                          day and shift by their remarks; until the store has
                          posted them, the waste logged on the runs x the
                          material's SAP price
Scrap Recurring           minus the same wastage in kilos x each kind of
                          scrap's latest price a kg (``ScrapKind``)
========================  ==================================================

**Wastage in SAP.** The floor's waste is moved in SAP from the production
floor (BH-PP) into the wastage warehouse (BH-WST), one transfer per line and
shift, posted days later. SAP's own date is therefore the posting, not the
production: the store writes the day and shift in the remarks — "wasatge
28-09-2026 Day" — and that is what a transfer is matched on. A transfer whose
remarks name no shift is the whole day's. A shift's sheet takes its shift's
transfers and its share of the day's cases of an untagged one; a line's sheet
takes its share of the cases, as SAP does not say which line a transfer is.

**Which shift.** Day is 07:00-19:00 and Night 19:00-07:00, a night belonging
to the date it starts (as ``labour_count``'s shifts are). A run's cases go to
the shift each of its running segments *started* in; a completed run's
entered Total Production is spread over its segments in the same proportion.
A run with no segments counts for the shift it was planned to start in.

**Why shares.** Electricity++ allocates a meter's units per *day*, day and
night readings together, and a month's salary is not earned per case, so a
shift takes a share of the day: electricity by what each meter's own day and
night readings measured (by cases where a meter had only one reading), the
monthly heads by the hours the lines ran. When only one shift ran, it
takes the whole day. The shares are of the *company's* day, so a line's
sheets add up with every other line's to one day.

Nothing here is saved: the page shows these as a new sheet's starting values,
and what is saved is what the person entering it confirms.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional

from django.db.models import Q
from django.utils import timezone

from cost_master.codes import (
    FILLING_COST_HEAD_ALIASES, FILLING_COST_TYPES, FILLING_COST_WORKING_DAYS,
)
from cost_master.models import CostBasis
from cost_master.services import resolve_rates_bulk
from maintenance.electricity.run_hours import MAX_SEGMENT_HOURS, capped, merge

from ..models import (
    FillingCostShift, ProductionMaterialUsage, ProductionRun, ProductionSegment,
    RunStatus, ScrapKind, WasteLog,
)

ZERO = Decimal('0')
ONE = Decimal('1')
PAISE = Decimal('0.01')

DAY_STARTS = time(7)
NIGHT_STARTS = time(19)

#: Units a quantity is in kilograms, or in grams, under.
KG_UOMS = {'KG', 'KGS', 'KILOGRAM', 'KILOGRAMS'}
GRAM_UOMS = {'G', 'GM', 'GMS', 'GRAM', 'GRAMS'}

#: The heads worked out from production rather than resolved as a rate.
ELECTRICITY = 'Electricity'
# Meters Electricity++ charges Beverages for that are not filling cost: the
# ETP treats the plant's effluent whatever the lines fill, and KWH is the main
# supply meter, which the boards leave out too. Matched by name, ignoring case.
NOT_FILLING_METERS = ('ETP', 'KWH')
WASTAGE = 'Wastage'
SCRAP = 'Scrap Recurring'

#: Where the store moves the floor's waste in SAP, and from where.
WASTAGE_WAREHOUSE = 'BH-WST'
WASTAGE_FROM_WAREHOUSE = 'BH-PP'
#: How long after the day its waste may still be posted. The store has taken
#: up to eleven days; a month leaves room.
WASTAGE_POSTING_DAYS = 30

# 'wasatge 28-09-2026 Day', 'wastge 4/9/26', 'wastage 26.8.26': day first,
# any of - / . between, then a year — which is not trusted (see below).
_REMARK_DATE = re.compile(r'(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,5})(?!\d)')
_REMARK_DAY = re.compile(r'\bday\b', re.IGNORECASE)
_REMARK_NIGHT = re.compile(r'\bnight\b', re.IGNORECASE)


def shift_window(day, shift=''):
    """``[start, end)`` of a shift of ``day``; blank = both, 07:00 to 07:00."""
    tz = timezone.get_current_timezone()

    def at(on, clock):
        return timezone.make_aware(datetime.combine(on, clock), tz)

    next_day = day + timedelta(days=1)
    if shift == FillingCostShift.DAY:
        return at(day, DAY_STARTS), at(day, NIGHT_STARTS)
    if shift == FillingCostShift.NIGHT:
        return at(day, NIGHT_STARTS), at(next_day, DAY_STARTS)
    return at(day, DAY_STARTS), at(next_day, DAY_STARTS)


def _within(moment, window):
    return moment is not None and window[0] <= moment < window[1]


def _hours(intervals, window):
    """Hours of the merged ``intervals`` inside ``window``."""
    seconds = 0.0
    for start, end in merge(intervals):
        if end > window[0] and start < window[1]:
            seconds += (min(end, window[1]) - max(start, window[0])).total_seconds()
    return Decimal(str(round(seconds))) / Decimal('3600')


def _money(value):
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def _figure(value, places=0):
    """12,00,000 — the Indian grouping the sheet is written in."""
    value = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    whole, _, fraction = f"{abs(value):f}".partition('.')
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ','.join(([head] if head else []) + groups + [tail])
    sign = '-' if value < 0 else ''
    return f"{sign}{whole}.{fraction}" if fraction else f"{sign}{whole}"


def _rate(value):
    """A rate as it is written: 12,00,000 or 0.03, not 1200000.0000."""
    value = Decimal(value).normalize()
    places = max(0, -value.as_tuple().exponent)
    return _figure(value, places)


def _pct(share):
    return f"{(share * 100).quantize(ONE, rounding=ROUND_HALF_UP)}%"


@dataclass
class _Run:
    """One run's part in the shift: what it made, and what came of it."""
    run: ProductionRun
    fraction: Decimal       # of the run's cases that fall in the shift
    day_fraction: Decimal   # ... in the whole day
    total: Decimal          # the run's cases, all shifts


@dataclass
class Production:
    """What the shift's runs made, and the day it is a share of."""
    cases: Decimal = ZERO
    day_cases: Decimal = ZERO          # the company's, every line
    run_count: int = 0
    bottles: Decimal = ZERO
    litres: Decimal = ZERO
    hours: Decimal = ZERO
    day_hours: Decimal = ZERO          # the company's, every line
    wastage: Decimal = ZERO            # rupees
    wastage_logs: int = 0
    #: material code -> {'name', 'uom', 'qty'}: the waste logged, item by item.
    waste_items: Dict[str, dict] = field(default_factory=dict)
    #: (product, bottles a case, litres a bottle) -> cases: what was filled, SKU by SKU.
    skus: Dict[tuple, Decimal] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def case_share(self):
        """The shift's part of the day's cases; its hours' part if nothing was counted."""
        if self.day_cases > 0:
            return self.cases / self.day_cases
        return self.hour_share

    @property
    def hour_share(self):
        """The shift's part of the day's running hours; all of it if nothing ran."""
        if self.day_hours > 0:
            return self.hours / self.day_hours
        return ONE


def _add_waste(result, log, qty):
    item = result.waste_items.setdefault(log.material_code or log.material_name, {
        'name': log.material_name, 'uom': (log.uom or '').strip().upper(), 'qty': ZERO})
    item['qty'] += qty


def _runs(company, day, now):
    """Every live, started run of the company that ran in ``day``'s 24 hours."""
    whole = shift_window(day)
    # A segment counts from its start for at most the cap, so none that began
    # earlier than that can reach into the day.
    touching = ProductionSegment.objects.filter(
        production_run__company=company,
        production_run__is_deleted=False,
        start_time__gte=whole[0] - timedelta(hours=MAX_SEGMENT_HOURS),
        start_time__lt=whole[1],
    ).values('production_run_id')
    return list(
        ProductionRun.objects.filter(company=company)
        .exclude(status=RunStatus.DRAFT)
        .filter(Q(id__in=touching) | Q(date=day))
        .select_related('line')
        .prefetch_related('segments', 'material_usages', 'waste_logs')
    )


def _share_of(run, window, day):
    """The part of ``run``'s cases made in ``window``, and those cases in all."""
    segments = list(run.segments.all())
    by_segments = sum((s.produced_cases or ZERO for s in segments), ZERO)
    total = (run.total_production
             if run.status == RunStatus.COMPLETED and run.total_production
             else by_segments)
    if by_segments > 0:
        weights = [(s.start_time, s.produced_cases or ZERO) for s in segments]
    else:
        # Counts were never logged on the segments: spread the run over the
        # time it ran instead.
        now = timezone.now()
        weights = []
        for s in segments:
            span = capped(s.start_time, s.end_time, now)
            if span:
                weights.append((s.start_time, Decimal(str((span[1] - span[0]).total_seconds()))))
    whole = sum((w for _, w in weights), ZERO)
    if whole > 0:
        return sum((w for start, w in weights if _within(start, window)), ZERO) / whole, total
    # Nothing to weigh by: the run belongs to the shift it was meant to start in.
    anchor = run.planned_start_at or shift_window(day)[0]
    return (ONE if _within(anchor, window) else ZERO), total


def production(company, day, shift='', line=None, now=None):
    """What ``shift`` of ``day`` (on ``line``, or every line) produced."""
    now = now or timezone.now()
    window = shift_window(day, shift)
    whole = shift_window(day)
    result = Production()

    shift_runs: List[_Run] = []
    intervals: Dict[int, list] = defaultdict(list)
    for run in _runs(company, day, now):
        fraction, total = _share_of(run, window, day)
        day_fraction, _ = _share_of(run, whole, day)
        for segment in run.segments.all():
            span = capped(segment.start_time, segment.end_time, now)
            if span:
                intervals[run.line_id].append(span)
        result.day_cases += total * day_fraction
        if line is not None and run.line_id != line.id:
            continue
        if fraction > 0:
            shift_runs.append(_Run(run, fraction, day_fraction, total))

    # Two runs on one line at once did not make the line run twice as long.
    for line_id, spans in intervals.items():
        result.day_hours += _hours(spans, whole)
        if line is None or line_id == line.id:
            result.hours += _hours(spans, window)

    no_bottles, no_litres, no_price = [], [], set()
    for part in shift_runs:
        run = part.run
        cases = part.total * part.fraction
        result.cases += cases
        result.run_count += 1
        sku = ((run.product or run.line.name).strip(), run.pieces_per_case, run.litres_per_piece)
        result.skus[sku] = result.skus.get(sku, ZERO) + cases
        if run.pieces_per_case:
            bottles = cases * run.pieces_per_case
            result.bottles += bottles
            if run.litres_per_piece:
                result.litres += bottles * run.litres_per_piece
            else:
                no_litres.append(run)
        else:
            no_bottles.append(run)

        prices = {u.material_code: u.unit_price for u in run.material_usages.all()
                  if u.material_code and u.unit_price is not None}
        for log in run.waste_logs.all():
            qty = log.wastage_qty * part.fraction
            result.wastage_logs += 1
            _add_waste(result, log, qty)
            price = prices.get(log.material_code)
            if price is None:
                no_price.add(log.material_name)
            else:
                result.wastage += qty * price

    # Waste logged on its own, not against a run, belongs to the floor as a
    # whole and to the shift it was logged in.
    if line is None:
        standalone = WasteLog.objects.filter(
            company=company, production_run__isnull=True,
            created_at__gte=window[0], created_at__lt=window[1])
        for log in standalone:
            result.wastage_logs += 1
            _add_waste(result, log, log.wastage_qty)
            price = (ProductionMaterialUsage.objects
                     .filter(production_run__company=company,
                             material_code=log.material_code, unit_price__isnull=False)
                     .exclude(material_code='')
                     .order_by('-created_at').values_list('unit_price', flat=True).first())
            if price is None:
                no_price.add(log.material_name)
            else:
                result.wastage += log.wastage_qty * price

    def names(runs):
        return ', '.join(f"#{r.run_number} {r.product or r.line.name}".strip() for r in runs)

    if no_bottles:
        result.warnings.append(
            f"No bottles per case on run {names(no_bottles)}: its cases are "
            f"left out of Batch Coding and Lubrication.")
    if no_litres:
        result.warnings.append(
            f"No litres per bottle on run {names(no_litres)}: its bottles are "
            f"left out of Lubrication.")
    if no_price:
        result.warnings.append(
            "No SAP price for " + ', '.join(sorted(no_price)) +
            ": that waste is left out of Wastage.")
    return result


def sku_label(product, litres_per_piece):
    """'1000 ML' — how the sheet names a SKU: its bottle; the product when that is unknown."""
    if litres_per_piece:
        ml = (Decimal(litres_per_piece) * 1000).quantize(ONE, rounding=ROUND_HALF_UP)
        return f"{ml} ML"
    return product


def sku_rows(made):
    """What the runs filled, biggest first: SKU, box size and boxes."""
    return [
        {'product': product, 'sku': sku_label(product, litres), 'pieces_per_case': pieces,
         'litres_per_piece': str(litres) if litres is not None else None,
         'cases': str(_money(cases))}
        for (product, pieces, litres), cases in sorted(made.skus.items(), key=lambda kv: -kv[1])
        if cases > 0
    ]


def _electricity(company, day):
    """Beverages' filling electricity for ``day``:
    ``(total, {meter name: cost}, {left-out meter name: cost})``.

    The Electricity++ cost less the ``NOT_FILLING_METERS``. ``None`` when the
    allocation cannot be read.
    """
    from maintenance.electricity import service
    from maintenance.electricity.sources import company_party

    try:
        breakdown = service.company_breakdown(day, day)
    except Exception:  # noqa: BLE001 - the sheet still opens; electricity is typed in
        return None
    party = company_party(company.code)
    total = breakdown['by_party'].get(party, {}).get('cost', ZERO)
    not_filling = {name.casefold() for name in NOT_FILLING_METERS}
    by_meter, left_out = {}, {}
    for name, part in breakdown['by_meter'].get(party, {}).items():
        (left_out if name.strip().casefold() in not_filling else by_meter)[name] = part['cost']
    return total - sum(left_out.values(), ZERO), by_meter, left_out


def _meter_rounds(day):
    """``{meter name: {'DAY': units, 'NIGHT': units}}`` read on ``day``."""
    from maintenance.models import DailyElectricityReading

    rounds: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for name, shift, units in (DailyElectricityReading.objects
                               .filter(date=day, is_active=True)
                               .values_list('meter__name', 'shift', 'units_consumed')):
        rounds[name][shift] += units or ZERO
    return rounds


def _shift_electricity(by_meter, rounds, shift, case_share):
    """The shift's part of each meter's cost: ``(amount, by_readings, by_cases)``.

    A meter read by day and by night splits by what each round measured; one
    read only once gives the shift its share of the day's cases.
    """
    amount, by_readings, by_cases = ZERO, 0, 0
    for name, cost in by_meter.items():
        units = rounds.get(name, {})
        both = units.get('DAY', ZERO) + units.get('NIGHT', ZERO)
        if units.get('NIGHT', ZERO) > 0 and both > 0:
            amount += cost * units.get(shift, ZERO) / both
            by_readings += 1
        else:
            amount += cost * case_share
            by_cases += 1
    return amount, by_readings, by_cases


def wastage_remarks(remarks, posted=None):
    """``(day, shift)`` a wastage transfer's remarks name, or ``None``.

    The store writes them by hand — 'wasatge 28-09-2026 Day', 'wastge 4/9/26',
    'Wastage 26.8.26 NIght' — so the date is read day first whatever separates
    it, and the shift is 'DAY', 'NIGHT', or '' for the whole day (named
    neither, or both: 'Day night').

    The year is the one thing the store gets wrong ('23/7/24', '07-05-20226'
    on transfers posted in 2026). Waste is posted after the day it is from,
    so given the date it was ``posted`` the day is the latest one on or
    before it with that day and month, whatever year is written.
    """
    match = _REMARK_DATE.search(remarks or '')
    if match is None:
        return None
    dd, mm, yy = (int(part) for part in match.groups())
    years = ([posted.year, posted.year - 1] if posted is not None
             else [yy + 2000 if yy < 100 else yy])
    for year in years:
        try:
            named = date(year, mm, dd)
        except ValueError:
            continue
        if posted is None or named <= posted:
            break
    else:
        return None
    by_day, by_night = _REMARK_DAY.search(remarks), _REMARK_NIGHT.search(remarks)
    shift = '' if bool(by_day) == bool(by_night) else ('DAY' if by_day else 'NIGHT')
    return named, shift


def _sap_wastage(company, day):
    """The store's wastage transfers for ``day``: ``(transfers, unread)``.

    ``transfers`` are those whose remarks name ``day``, each with the
    ``shift`` they name; ``unread`` are transfers posted in the window whose
    remarks name no day at all, so cannot be placed. ``None`` when SAP cannot
    be read.
    """
    from sap_client.context import CompanyContext

    from .reconciliation_reader import ReconciliationReader

    try:
        posted = ReconciliationReader(CompanyContext(company.code)).wastage_transfers(
            day, day + timedelta(days=WASTAGE_POSTING_DAYS),
            WASTAGE_WAREHOUSE, WASTAGE_FROM_WAREHOUSE)
    except Exception:  # noqa: BLE001 - the sheet still opens on the waste logs
        return None
    transfers, unread = [], []
    for transfer in posted:
        named = wastage_remarks(transfer['remarks'], transfer['doc_date'])
        if named is None:
            unread.append(transfer)
        elif named[0] == day:
            transfers.append({**transfer, 'shift': named[1]})
    return transfers, unread


def _scrap(company, day, items, warnings):
    """Scrap Recurring off ``items`` (code -> {'name', 'uom', 'qty'}):
    ``(rupees, [(kind, kg, price a kg)], not scrap)``, or ``None`` when the
    company keeps no scrap rates.

    An item counts under the scrap kind that lists it, in kilos: as they are,
    from grams, or from pieces by the kind's weight a piece. A kind with no
    weight or no price is named in ``warnings`` and left out, not guessed.
    """
    kinds = list(ScrapKind.objects.filter(company=company, is_active=True)
                 .prefetch_related('prices'))
    if not kinds:
        return None
    by_code = {code: kind for kind in kinds for code in kind.item_codes}
    sold, not_scrap = {}, []
    no_weight, no_price = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for code, item in items.items():
        kind = by_code.get(code)
        if kind is None:
            not_scrap.append(item['name'])
            continue
        uom = (item['uom'] or '').upper()
        if uom in KG_UOMS:
            kg = item['qty']
        elif uom in GRAM_UOMS:
            kg = item['qty'] / 1000
        elif kind.grams_per_piece:
            kg = item['qty'] * kind.grams_per_piece / 1000
        else:
            no_weight[kind] += item['qty']
            continue
        price = kind.price_on(day)
        if price is None:
            no_price[kind] += kg
            continue
        part = sold.setdefault(kind, [ZERO, price.price_per_kg])
        part[0] += kg
    for kind, pieces in no_weight.items():
        warnings.append(
            f"{kind.name} has no weight a piece in the scrap rates: its "
            f"{_figure(pieces)} pieces are left out of {SCRAP}.")
    for kind, kg in no_price.items():
        warnings.append(
            f"{kind.name} has no price a kg in the scrap rates: its "
            f"{_figure(kg, 3)} kg are left out of {SCRAP}.")
    rupees = sum((kg * price for kg, price in sold.values()), ZERO)
    lines = [(kind, kg, price) for kind, (kg, price) in
             sorted(sold.items(), key=lambda kv: kv[0].sort_order)]
    return rupees, lines, sorted(set(not_scrap))


def _wastage_and_scrap(company, day, shift, line, now, made, add):
    """Wastage and Scrap Recurring: off SAP's wastage transfers where the store
    has posted the day, off the app's waste logs until it has."""
    scopes = {(shift, line.id if line else None): made}

    def made_in(window, of_line):
        key = (window, of_line.id if of_line else None)
        if key not in scopes:
            scopes[key] = production(company, day, window, of_line, now)
        return scopes[key]

    def share_of(posted_for):
        """This sheet's part of a transfer for ``posted_for`` ('' = the whole day)."""
        if shift and posted_for and posted_for != shift:
            return ZERO
        window = shift or posted_for
        if line is None and window == posted_for:
            return ONE
        part, whole = made_in(window, line), made_in(posted_for, None)
        if whole.cases > 0:
            return part.cases / whole.cases
        if whole.hours > 0:
            return part.hours / whole.hours
        return ONE if line is None else ZERO

    on_day = f"{day:%d %b}" + (f" {FillingCostShift(shift).name.title()}" if shift else '')
    sap = _sap_wastage(company, day)
    used, items, value = [], {}, ZERO
    if sap is None:
        made.warnings.append(
            "SAP could not be read: Wastage and Scrap Recurring are from the app's waste logs.")
    else:
        transfers, unread = sap
        for transfer in transfers:
            share = share_of(transfer['shift'])
            if share <= 0:
                continue
            amount = sum((Decimal(str(moved['value'])) for moved in transfer['lines']), ZERO) * share
            used.append((transfer, share, amount))
            value += amount
            for moved in transfer['lines']:
                item = items.setdefault(moved['item_code'], {
                    'name': moved['item_name'], 'uom': moved['uom'], 'qty': ZERO})
                item['qty'] += Decimal(str(moved['qty'])) * share
        if unread:
            made.warnings.append(
                "SAP wastage " + ('transfer ' if len(unread) == 1 else 'transfers ')
                + ', '.join(str(t['doc_num']) for t in unread)
                + " name no day in their remarks, so are not counted on any sheet.")
        if used and not shift:
            # A day posted for one shift only, while the other ran.
            posted = {t['shift'] for t, _, _ in used}
            if '' not in posted:
                for missing in {FillingCostShift.DAY, FillingCostShift.NIGHT} - posted:
                    if made_in(missing, line).cases > 0:
                        made.warnings.append(
                            f"SAP has no wastage transfer for {day:%d %b} "
                            f"{FillingCostShift(missing).name.title()} yet, though it ran: "
                            f"Wastage counts only what is posted.")

    if used:
        source = 'sap'
        said = ', '.join(
            f"{t['doc_num']} ({t['remarks']}) ₹{_figure(amount)}"
            + (f" = {_pct(share)} of ₹{_figure(amount / share)}" if share != ONE else '')
            for t, share, amount in used)
        add(WASTAGE, value,
            f"SAP: what the store moved from {WASTAGE_FROM_WAREHOUSE} to {WASTAGE_WAREHOUSE} "
            f"for {on_day}, at SAP's value — {said}", source)
    elif made.wastage_logs:
        source, items = 'waste_logs', made.waste_items
        add(WASTAGE, made.wastage,
            f"{made.wastage_logs} waste log {'entry' if made.wastage_logs == 1 else 'entries'}"
            f" × the material's SAP price on the run"
            + ('' if sap is None else
               f" — SAP has no wastage transfer for {on_day} yet"), source)
    else:
        return

    scrap = _scrap(company, day, items, made.warnings)
    if scrap is None:
        return
    rupees, sold, not_scrap = scrap
    if not sold:
        return
    kg = sum((part for _, part, _ in sold), ZERO)
    said = ', '.join(f"{kind.name} {_figure(part, 3)} kg × ₹{_rate(price)}"
                     for kind, part, price in sold)
    add(SCRAP, -rupees,
        f"{_figure(kg, 3)} kg of the wastage sold as scrap at the latest scrap rates, "
        f"deducted: {said}"
        + (f". Not scrap: {', '.join(not_scrap)}" if not_scrap else ''),
        source)


def defaults(company, day, shift='', line=None, now=None):
    """A new sheet's starting values: its cases, and an amount for each head."""
    made = production(company, day, shift, line, now)
    rates = resolve_rates_bulk(
        {head: meta[0] for head, meta in FILLING_COST_TYPES.items()},
        as_of=day, company_id=company.id,
        # A day before a rate was first entered still takes it, rather than
        # opening at nothing.
        fallback_earliest=True,
    )
    entries = []

    def add(head, amount, explain, source):
        entries.append({
            'head': head,
            'aliases': list(FILLING_COST_HEAD_ALIASES.get(head, ())),
            'amount': str(_money(amount)) if amount is not None else None,
            'explain': explain,
            'source': source,
        })

    whole_day = not shift and line is None
    case_part = '' if whole_day or made.case_share == ONE else \
        f" × {_pct(made.case_share)} of the day's cases"
    hour_part = '' if whole_day or made.hour_share == ONE else \
        f" × {_pct(made.hour_share)} of the day's running hours"

    electricity = _electricity(company, day)
    if electricity is None:
        made.warnings.append("Electricity++ could not be read: enter Electricity by hand.")
    elif electricity[0] > 0:
        total, by_meter, left_out = electricity
        said = f"Electricity++: ₹{_figure(total)} for {company.name} on {day:%d %b}"
        if left_out:
            said += " (less " + ', '.join(
                f"{name} ₹{_figure(cost)}" for name, cost in sorted(left_out.items())) + ")"
        # Each meter at Beverages' share — a shared meter such as the Lab at
        # its percentage — so the figure can be checked against Electricity++.
        meters = ', '.join(f"{name} ₹{_figure(cost)}" for name, cost in
                           sorted(by_meter.items(), key=lambda kv: -kv[1]) if cost)
        if not shift:
            share = ONE if line is None else made.case_share
            add(ELECTRICITY, total * share,
                said + case_part + (f". Meters: {meters}" if meters else ''), 'electricity')
        else:
            amount, by_readings, by_cases = _shift_electricity(
                by_meter or {'': total}, _meter_rounds(day), shift, made.case_share)
            how = []
            if by_readings:
                how.append(f"{by_readings} {'meter' if by_readings == 1 else 'meters'} "
                           f"by their {FillingCostShift(shift).name.lower()} readings")
            if by_cases:
                how.append(f"{by_cases} {'meter' if by_cases == 1 else 'meters'} by "
                           f"{_pct(made.case_share)} of the day's cases")
            add(ELECTRICITY, amount,
                f"{said}: " + ', '.join(how) + (f". Meters: {meters}" if meters else ''),
                'electricity')
    else:
        made.warnings.append(
            f"Electricity++ has no {company.name} units on {day:%d %b} yet: "
            f"enter Electricity by hand, or once the meters are read.")

    for head, (code, _, _, _) in FILLING_COST_TYPES.items():
        rate = rates[head]
        if rate is None:
            continue
        credit = rate.cost_type.is_credit
        sign = -ONE if credit else ONE
        value = rate.rate
        if rate.basis in (CostBasis.PER_MONTH, CostBasis.PER_DAY):
            daily = value / FILLING_COST_WORKING_DAYS if rate.basis == CostBasis.PER_MONTH else value
            share = ONE if whole_day else made.hour_share
            how = (f"₹{_rate(value)} a month ÷ {FILLING_COST_WORKING_DAYS} days"
                   if rate.basis == CostBasis.PER_MONTH else f"₹{_rate(value)} a day")
            add(head, sign * daily * share, how + hour_part, 'cost_master')
        elif rate.basis == CostBasis.PER_BOTTLE:
            add(head, sign * made.bottles * value,
                f"{_figure(made.bottles)} bottles × ₹{_rate(value)}", 'cost_master')
        elif rate.basis == CostBasis.PER_LITRE:
            note = f" ({rate.notes})" if rate.notes else ''
            add(head, sign * made.litres * value,
                f"{_figure(made.litres)} litres × ₹{_rate(value)} a litre{note}", 'cost_master')
        elif rate.basis == CostBasis.PER_CASE:
            add(head, sign * made.cases * value,
                f"{_figure(made.cases)} cases × ₹{_rate(value)}", 'cost_master')

    _wastage_and_scrap(company, day, shift, line, now, made, add)

    return {
        'date': day.isoformat(),
        'shift': shift,
        'produced_cases': str(_money(made.cases)),
        'run_count': made.run_count,
        'bottles': str(made.bottles.quantize(ONE, rounding=ROUND_HALF_UP)),
        'litres': str(made.litres.quantize(ONE, rounding=ROUND_HALF_UP)),
        'skus': sku_rows(made),
        'running_hours': str(made.hours.quantize(PAISE, rounding=ROUND_HALF_UP)),
        'entries': entries,
        'warnings': made.warnings,
    }
