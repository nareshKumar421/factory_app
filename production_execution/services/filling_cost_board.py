"""Beverages' filling cost board: the saved sheets, a month at a time and a day.

Read from the sheets as they were saved, not worked out again: the board
shows what the factory signed off, and a sheet corrected by hand shows
corrected.

**Which sheets make a day.** The floor-wide sheets (no line). A day kept by
shift is its Day and Night sheets added up; a day with no shift sheet is its
whole-day sheet. Both at once would count the day twice, so the shift sheets
win. A day saved only line by line, with no floor-wide sheet, is its lines
added up — each line by the same shift-wins rule — rather than a blank day;
once a floor-wide sheet exists the lines' sheets are not counted beside it.

**Bottles.** A sheet counts cases, and the rate a bottle is what the factory
compares SKUs by. The bottles come from the day's production runs: the
sheet's cases times the bottles a case the runs made (a day of 500 ML at 24
and 1 L at 12 packs in between). A day with no runs has no bottle figure
rather than a guessed one.
"""
import calendar
from collections import OrderedDict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from cost_master.codes import FILLING_COST_HEAD_ALIASES

from ..models import FillingCostSheet, FillingCostShift
from .filling_cost import production, sku_rows

ZERO = Decimal('0')
PAISE = Decimal('0.01')
FOUR = Decimal('0.0001')

# 'salary' -> 'Fixed Manpower': an older sheet's head adds into the new name.
_ALIAS = {old.casefold(): head
          for head, olds in FILLING_COST_HEAD_ALIASES.items() for old in olds}


def _head(name):
    return _ALIAS.get(name.strip().casefold(), name.strip())


def _q(value, places=PAISE):
    return str(value.quantize(places, rounding=ROUND_HALF_UP)) if value is not None else None


def _per(amount, count, places=PAISE):
    return _q(amount / count, places) if count else None


def month_bounds(month):
    """``(first, last)`` day of the month ``month`` (any day in it)."""
    first = month.replace(day=1)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _shift_wins(sheets):
    return [s for s in sheets if s.shift] or [s for s in sheets if not s.shift]


def _sheets(company, first, last):
    """``{day: [sheets that make it]}`` — see the module docstring."""
    floor, lines = {}, {}
    for sheet in (FillingCostSheet.objects
                  .filter(company=company, date__range=(first, last))
                  .prefetch_related('entries').order_by('date', 'shift', 'line_id')):
        if sheet.line_id is None:
            floor.setdefault(sheet.date, []).append(sheet)
        else:
            lines.setdefault(sheet.date, {}).setdefault(sheet.line_id, []).append(sheet)
    by_day = {day: _shift_wins(sheets) for day, sheets in floor.items()}
    for day, per_line in lines.items():
        if day not in by_day:
            by_day[day] = sorted(
                (s for sheets in per_line.values() for s in _shift_wins(sheets)),
                key=lambda s: (s.shift, s.line_id))
    return by_day


class _Tally:
    """Cases, bottles and money, and each head's money, added up."""

    def __init__(self):
        self.cases = ZERO
        self.bottles = ZERO
        self.bottled_cases = ZERO   # the cases that have a bottle figure
        self.bottled_total = ZERO   # ... and the money spent on them
        self.total = ZERO
        self.heads = OrderedDict()
        self.bottled_heads = {}     # each head's money on the cases with bottles

    def add_sheet(self, sheet, bottles_a_case):
        cases = sheet.cases
        total = ZERO
        for entry in sheet.entries.all():
            head = _head(entry.head)
            self.heads[head] = self.heads.get(head, ZERO) + entry.amount
            if bottles_a_case:
                self.bottled_heads[head] = self.bottled_heads.get(head, ZERO) + entry.amount
            total += entry.amount
        self.cases += cases
        self.total += total
        if bottles_a_case:
            self.bottles += cases * bottles_a_case
            self.bottled_cases += cases
            self.bottled_total += total

    def add(self, other):
        for name in ('cases', 'bottles', 'bottled_cases', 'bottled_total', 'total'):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for head, amount in other.heads.items():
            self.heads[head] = self.heads.get(head, ZERO) + amount
        for head, amount in other.bottled_heads.items():
            self.bottled_heads[head] = self.bottled_heads.get(head, ZERO) + amount

    def figures(self):
        # A bottle's rate is over the cases that have bottles, so a day with
        # no runs does not make every bottle look cheaper.
        return {
            'cases': _q(self.cases),
            'bottles': _q(self.bottles, Decimal('1')),
            'total': _q(self.total),
            'per_case': _per(self.total, self.cases),
            'per_bottle': _per(self.bottled_total, self.bottles, FOUR),
        }

    def head_rows(self, ordered=False):
        """Each head's figures, biggest first — or in the order the sheet has them."""
        items = self.heads.items() if ordered else sorted(self.heads.items(), key=lambda kv: -kv[1])
        return [
            {
                'head': head,
                'amount': _q(amount),
                'share': _per(amount * 100, self.total) if self.total else None,
                'per_case': _per(amount, self.cases),
                # A head only ever spent on days without runs has no bottles
                # to be over, which is not the same as costing nothing a bottle.
                'per_bottle': (_per(self.bottled_heads[head], self.bottles, FOUR)
                               if head in self.bottled_heads else None),
            }
            for head, amount in items
        ]


def _made(company, day, shift=''):
    """``(bottles a case, SKUs)`` of what ``day``'s (``shift``'s) runs filled."""
    made = production(company, day, shift)
    ratio = made.bottles / made.cases if made.cases > 0 and made.bottles > 0 else None
    return ratio, sku_rows(made)


def _bottles_a_case(company, day, shift=''):
    return _made(company, day, shift)[0]


def board(company, month, day):
    """The month ``month`` is in, day by day, and ``day`` in full."""
    first, last = month_bounds(month)
    by_day = _sheets(company, first, last)

    month_tally = _Tally()
    days = []
    for on in sorted(by_day):
        ratio = _bottles_a_case(company, on)
        tally = _Tally()
        for sheet in by_day[on]:
            tally.add_sheet(sheet, ratio)
        month_tally.add(tally)
        days.append({'date': on.isoformat(),
                     'kept_by': 'shift' if by_day[on][0].shift else 'day',
                     **tally.figures()})

    # The month before, for the comparison beside each rate. Cases and money
    # only: a rate a case needs no runs read.
    prev_first, prev_last = month_bounds(first - timedelta(days=1))
    prev = _Tally()
    prev_days = _sheets(company, prev_first, prev_last)
    for sheets in prev_days.values():
        for sheet in sheets:
            prev.add_sheet(sheet, None)

    # The day in full, from its own month's sheets or its own read.
    day_sheets = by_day.get(day) if first <= day <= last else _sheets(company, day, day).get(day)
    day_block = None
    if day_sheets:
        ratio, skus = _made(company, day)
        tally = _Tally()
        by_shift = OrderedDict()
        for sheet in day_sheets:
            tally.add_sheet(sheet, ratio)
            if sheet.shift:
                # Several lines' sheets for one shift read as that shift.
                by_shift.setdefault(sheet.shift, []).append(sheet)
        shifts = []
        for shift, sheets in by_shift.items():
            own_ratio, own_skus = _made(company, day, shift)
            own = _Tally()
            for sheet in sheets:
                own.add_sheet(sheet, own_ratio or ratio)
            shifts.append({'shift': shift,
                           'label': FillingCostShift(shift).label,
                           **own.figures(),
                           # In the sheet's own order, as the factory writes it.
                           'heads': own.head_rows(ordered=True),
                           'skus': own_skus})
        day_block = {
            'date': day.isoformat(),
            'kept_by': 'shift' if shifts else 'day',
            **tally.figures(),
            'heads': tally.head_rows(),
            'sheet_heads': tally.head_rows(ordered=True),
            'skus': skus,
            'shifts': shifts,
        }

    return {
        'month': first.strftime('%Y-%m'),
        'days_in_month': last.day,
        'days_entered': len(days),
        'totals': month_tally.figures(),
        'previous': {'month': prev_first.strftime('%Y-%m'),
                     'per_case': _per(prev.total, prev.cases),
                     'days_entered': len(prev_days)},
        'heads': month_tally.head_rows(),
        'days': days,
        'day': day_block,
        'selected_day': day.isoformat(),
    }


def yesterday(today=None):
    from django.utils import timezone

    return (today or timezone.localdate()) - timedelta(days=1)

