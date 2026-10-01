"""
The floor's shift sheet — the blowing operator's Excel — booked as completed runs.

Who it is for: the person who enters blowing runs is not at the machine for
every shift (the line runs through the night), so a shift they missed reaches
them as the sheet. Its layout, as the floor keeps it::

    30.09.2026
    sku             shift  total production  labour            total electricity  utility  wastage
                                             company  outside
    frystal 40 gms
                    night  27440             0        5        146.4              765.6    20

A date row, a header (labour split company / outside on the line under it), a
SKU row, then one row per shift under that SKU. A sheet can carry several dates
and several SKUs; each shift row becomes one run.

Three steps, so a wrong sheet is a wrong screen rather than wrong runs:

* ``parse_workbook`` reads the file into plain rows, matched to preform specs.
* ``plan_rows`` says what each row would become — its run number, its meter
  readings, its cost, and whether the app already has that shift. It writes
  nothing.
* ``apply_rows`` makes the same plan again under a lock on the machine and
  books it.

Rules the plan holds to, settled while this was a hand backfill:

* **A shift the floor already entered is skipped, not overwritten.** The floor
  enters a run as it happens, so its figures outrank the sheet's. The shift of
  a run in the app is read off how it actually ran (its first segment), never
  off its run number: run numbers are a per-date sequence, and a night shift
  is often run 1.
* **The sheet gives units, not meter readings.** A row starts where the run
  before it (in date and shift order) stopped, and stops ``units`` later, so
  the machine's meter chain stays unbroken.
* **A run is dated to its shift.** ``created_at`` is 09:00 IST for a day shift
  and 20:00 IST for a night shift, so the run sits where it happened rather
  than on the morning it was typed in.
"""
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ..models import BlowingMachine, BlowingRun, PreformSpec, RunStatus, WarehouseApprovalStatus
from .cost_calculator import compute_run_cost

IST = ZoneInfo('Asia/Kolkata')

DAY = 'DAY'
NIGHT = 'NIGHT'
SHIFT_LABEL = {DAY: 'day', NIGHT: 'night'}
_SHIFT_RANK = {DAY: 0, NIGHT: 1}
# When a run booked from the sheet is stamped as created.
_SHIFT_CREATED_AT = {DAY: time(9, 0), NIGHT: time(20, 0)}
# A run whose production started from 06:00 up to 18:00 IST ran the day shift.
_DAY_FROM_HOUR = 6
_NIGHT_FROM_HOUR = 18

# One upload is a few days of one machine; these only stop a wrong file.
MAX_ROWS = 300
MAX_SHEET_LINES = 5000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# The sheet does not count operators; one runs the machine every shift.
DEFAULT_OPERATORS = 1
# How far the app's next run may start from where a sheet row stops the meter
# before the row says so.
_METER_TOLERANCE = Decimal('1')
_MAX_UNITS = Decimal('1000000')
_FOUR_PLACES = Decimal('0.0001')


class SheetError(ValueError):
    """The upload is not a shift sheet this module can read."""


class ShiftSheetRefused(ValueError):
    """The rows cannot be booked as they stand; ``plan`` says why, row by row."""

    def __init__(self, message, plan):
        super().__init__(message)
        self.plan = plan


# ===========================================================================
# Reading the sheet
# ===========================================================================

_DMY = re.compile(r'(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)')
_YMD = re.compile(r'(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)')
_NUMBER = re.compile(r'\d+(?:\.\d+)?')


def _text(value) -> str:
    if value is None:
        return ''
    return ' '.join(str(value).split()).strip()


def _to_date(value):
    """A date from a date cell or text like ``30.09.2026`` / ``Date: 30-09-26``.

    Numbers are never dates here: a production figure must not turn into one.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _YMD.search(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
    else:
        match = _DMY.search(text)
        if not match:
            return None
        day, month, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _to_shift(value):
    text = _text(value).lower()
    if not text:
        return None
    if 'night' in text or text == 'n':
        return NIGHT
    if 'day' in text or text == 'd':
        return DAY
    return '?'


def _to_number(value):
    """``(Decimal or None, problem text or None)`` for a figure cell."""
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)), None
    text = _text(value).replace(',', '')
    if text in ('', '-', '—'):
        return None, None
    try:
        return Decimal(text), None
    except InvalidOperation:
        return None, f"'{_text(value)}' is not a number"


def _header_field(cell):
    """Which sheet column a header cell names, or None."""
    text = _text(cell).lower()
    if not text:
        return None
    if 'shift' in text:
        return 'shift'
    if 'production' in text or 'counter' in text:
        return 'production'
    if 'sku' in text or 'preform' in text or text in ('item', 'size', 'bottle'):
        return 'sku'
    if 'company' in text or text in ('own', 'own labour'):
        return 'own_labour'
    if 'outside' in text or 'contract' in text:
        return 'contract_labour'
    if 'labour' in text or 'labor' in text or 'manpower' in text:
        return 'labour'
    if 'utility' in text:
        return 'utility'
    if 'electric' in text or text in ('units', 'machine units'):
        return 'units'
    if 'wastage' in text or 'waste' in text or 'reject' in text:
        return 'wastage'
    if text == 'date':
        return 'date'
    return None


def _header_columns(row):
    """``{field: column index}`` when ``row`` is the sheet's header line, else None."""
    columns = {}
    for index, cell in enumerate(row):
        field = _header_field(cell)
        if field and field not in columns:
            columns[field] = index
    figures = {'production', 'units', 'utility', 'wastage'} & columns.keys()
    if 'shift' in columns and len(figures) >= 2:
        return columns
    return None


def _labour_columns(row, columns):
    """Fold the company / outside line under a merged ``labour`` header in."""
    found = {}
    for index, cell in enumerate(row):
        field = _header_field(cell)
        if field in ('own_labour', 'contract_labour'):
            found[field] = index
    if not found:
        return False
    columns.update(found)
    return True


def match_spec(sku_text, specs):
    """``(spec or None, note or None)`` for the sheet's SKU text.

    The floor writes the make and the gram weight — ``frystal 40 gms``,
    ``pioneer 49.5 gm`` — so a spec matches when its make appears in the text
    and its gram weight is one of the text's numbers. Anything other than
    exactly one match is left for the person to pick.
    """
    text = _text(sku_text).lower()
    if not text:
        return None, None
    numbers = {Decimal(n) for n in _NUMBER.findall(text)}
    hits = [
        spec for spec in specs
        if spec.make.strip() and spec.make.strip().lower() in text and spec.gram in numbers
    ]
    if len(hits) == 1:
        return hits[0], None
    if hits:
        return None, f"SKU '{_text(sku_text)}' matches {len(hits)} preform specs — pick one"
    return None, f"SKU '{_text(sku_text)}' matches no preform spec — pick one"


def _cell(row, columns, field):
    index = columns.get(field)
    if index is None or index >= len(row):
        return None
    return row[index]


def _is_date_line(row):
    """A line that only says which day the rows under it belong to."""
    filled = [cell for cell in row if _text(cell)]
    if not filled or len(filled) > 2:
        return None
    for cell in filled:
        found = _to_date(cell)
        if found:
            return found
    return None


def _read_lines(file_obj):
    """``[(sheet title, [(line number, [cell values])])]`` for every worksheet.

    Not read-only: read-only mode hands back values hidden under a merged
    cell. The floor reuses the file, so the merged date line ``A8:H8`` can
    still hold an old date in B8 that Excel no longer shows — the sheet must be
    read as the person sees it.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - openpyxl is a requirement
        raise SheetError('The server cannot read Excel files (openpyxl is missing).') from exc
    try:
        workbook = load_workbook(file_obj, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is the same answer
        raise SheetError('That file could not be opened as an Excel workbook (.xlsx).') from exc
    sheets = []
    for worksheet in workbook.worksheets:
        last = min(worksheet.max_row or 0, MAX_SHEET_LINES)
        lines = [
            (number, list(row))
            for number, row in enumerate(
                worksheet.iter_rows(max_row=last, values_only=True), start=1)
        ]
        sheets.append((worksheet.title, lines, (worksheet.max_row or 0) > MAX_SHEET_LINES))
    return sheets


def parse_workbook(file_obj, company):
    """Read every sheet of the workbook into rows for the shift-sheet page.

    Returns ``{'rows': [...], 'ignored': [...]}``. Each row carries the values
    as read plus ``notes`` for anything the person has to fix on screen — an
    unknown SKU, a shift that is neither day nor night. Lines that hold figures
    but no shift (a stray total, a scribbled meter digit) are not guessed at:
    they are listed in ``ignored`` with the reason.
    """
    specs = list(PreformSpec.objects.filter(company=company, is_active=True))
    rows, ignored = [], []
    for title, lines, cut_short in _read_lines(file_obj):
        if cut_short:
            ignored.append({
                'sheet': title, 'line': MAX_SHEET_LINES + 1,
                'reason': f'Only the first {MAX_SHEET_LINES} lines of this sheet were read.',
            })
        columns = None
        current_date = None
        current_sku = ''
        for position, (number, line) in enumerate(lines):
            if not any(_text(cell) for cell in line):
                continue
            found_date = _is_date_line(line)
            if found_date:
                current_date = found_date
                continue
            header = _header_columns(line)
            if header:
                columns = header
                # The company / outside split sits on the line under "labour".
                if position + 1 < len(lines):
                    _labour_columns(lines[position + 1][1], columns)
                if 'labour' in columns and 'contract_labour' not in columns:
                    columns['contract_labour'] = columns['labour']
                continue
            if columns is None:
                continue
            if _labour_columns(line, {}) and not _to_shift(_cell(line, columns, 'shift')):
                continue  # the company / outside sub-header itself

            row_date = _to_date(_cell(line, columns, 'date')) or current_date
            sku_cell = _text(_cell(line, columns, 'sku'))
            if sku_cell and _to_number(sku_cell)[0] is None:
                current_sku = sku_cell
            shift = _to_shift(_cell(line, columns, 'shift'))

            if shift is None:
                figures = [
                    _text(_cell(line, columns, field))
                    for field in ('production', 'own_labour', 'contract_labour',
                                  'units', 'utility', 'wastage')
                ]
                if any(figures) and not sku_cell:
                    shown = ', '.join(f for f in figures if f)
                    ignored.append({
                        'sheet': title, 'line': number,
                        'reason': f"Figures with no shift ({shown}) — left out.",
                    })
                continue

            figures = [
                _text(_cell(line, columns, field))
                for field in ('production', 'units', 'utility', 'wastage')
            ]
            if not any(figures):
                which = (SHIFT_LABEL.get(shift) or 'a').capitalize()
                ignored.append({
                    'sheet': title, 'line': number,
                    'reason': (f'{which} shift with no figures — left out. If it ran, '
                               f'the floor may have entered it in the app.'),
                })
                continue

            notes = []
            if shift == '?':
                notes.append(f"Shift '{_text(_cell(line, columns, 'shift'))}' is neither day "
                             f"nor night — pick one.")
                shift = None
            if row_date is None:
                notes.append('No date above this row — pick one.')
            spec, spec_note = match_spec(current_sku, specs)
            if spec_note:
                notes.append(spec_note)
            elif not current_sku:
                notes.append('No SKU above this row — pick the preform.')

            values = {}
            for field, key in (
                ('production', 'total_counter_production'),
                ('own_labour', 'own_labour_count'),
                ('contract_labour', 'contract_labour_count'),
                ('units', 'machine_units'),
                ('utility', 'utility_units'),
                ('wastage', 'rejection_pcs'),
            ):
                amount, problem = _to_number(_cell(line, columns, field))
                if problem:
                    notes.append(problem)
                values[key] = None if amount is None else f'{amount.normalize():f}'

            rows.append({
                'sheet': title,
                'line': number,
                'date': row_date.isoformat() if row_date else None,
                'shift': shift,
                'sku_text': current_sku,
                'preform_spec_id': spec.id if spec else None,
                **values,
                'notes': notes,
            })
            if len(rows) > MAX_ROWS:
                raise SheetError(f'The sheet has more than {MAX_ROWS} shift rows — split it up.')
    if not rows:
        raise SheetError(
            'No shift rows found. The sheet needs a header line with sku, shift, total '
            'production, labour, total electricity, utility and wastage, and a day or '
            'night row under each SKU.')
    return {'rows': rows, 'ignored': ignored}


# ===========================================================================
# Planning
# ===========================================================================

_SHIFT_IN_REMARKS = re.compile(r'\b(day|night) shift\b', re.IGNORECASE)


def run_shift(run):
    """The shift a run already in the app ran — DAY or NIGHT.

    A run booked from a sheet says so in its remarks. Otherwise it is read off
    when production started (its first segment), falling back to when the run
    was opened, which the floor does at the start of the shift.
    """
    match = _SHIFT_IN_REMARKS.search(run.remarks or '')
    if match:
        return DAY if match.group(1).lower() == 'day' else NIGHT
    moment = _run_started(run)
    if moment is None:
        return None
    hour = timezone.localtime(moment, IST).hour
    return DAY if _DAY_FROM_HOUR <= hour < _NIGHT_FROM_HOUR else NIGHT


def _run_started(run):
    starts = [segment.start_time for segment in run.segments.all() if segment.start_time]
    return min(starts) if starts else run.created_at


def _spec_label(spec):
    return f'{spec.make} {spec.gram.normalize():f}g'


def _as_int(raw, label, errors, *, required=False, default=0):
    if raw is None or _text(raw) == '':
        if required:
            errors.append(f'{label} is required.')
            return None
        return default
    amount, problem = _to_number(raw)
    if problem or amount is None:
        errors.append(f'{label}: {problem or "not a number"}.')
        return None
    if amount != amount.to_integral_value():
        errors.append(f'{label} must be a whole number.')
        return None
    if amount < 0:
        errors.append(f'{label} cannot be negative.')
        return None
    return int(amount)


def _as_units(raw, label, errors):
    if raw is None or _text(raw) == '':
        return Decimal('0')
    amount, problem = _to_number(raw)
    if problem or amount is None:
        errors.append(f'{label}: {problem or "not a number"}.')
        return None
    if amount < 0:
        errors.append(f'{label} cannot be negative.')
        return None
    if amount > _MAX_UNITS:
        errors.append(f'{label} of {amount} is not a shift\'s reading.')
        return None
    return amount.quantize(_FOUR_PLACES)


def _clean_row(index, raw, specs, today):
    """Validate one row as sent by the page; ``(values, errors)``."""
    errors = []
    if not isinstance(raw, dict):
        return {'index': index}, ['This row is not a row of the sheet.']

    row_date = raw.get('date')
    if isinstance(row_date, str):
        try:
            row_date = date.fromisoformat(row_date.strip())
        except ValueError:
            row_date = _to_date(row_date)
    elif not isinstance(row_date, date):
        row_date = None
    if row_date is None:
        errors.append('Pick the date.')
    elif row_date > today:
        errors.append(f'{row_date:%d %b %Y} is in the future.')

    shift = raw.get('shift')
    if shift not in (DAY, NIGHT):
        errors.append('Pick the shift — day or night.')
        shift = None

    spec = None
    try:
        spec = specs.get(int(raw.get('preform_spec_id')))
    except (TypeError, ValueError):
        pass
    if spec is None:
        errors.append('Pick the preform (SKU).')
    elif not spec.is_active:
        errors.append(f'{_spec_label(spec)} is no longer an active preform spec.')

    production = _as_int(raw.get('total_counter_production'), 'Total production',
                         errors, required=True)
    if production == 0:
        errors.append('Total production must be more than zero.')
    rejects = _as_int(raw.get('rejection_pcs'), 'Wastage', errors)
    if production and rejects is not None and rejects > production:
        errors.append(f'Wastage ({rejects}) is more than the total production ({production}).')
    own = _as_int(raw.get('own_labour_count'), 'Company labour', errors)
    contract = _as_int(raw.get('contract_labour_count'), 'Outside labour', errors)
    operators = _as_int(raw.get('operator_count'), 'Operators', errors,
                        default=DEFAULT_OPERATORS)
    units = _as_units(raw.get('machine_units'), 'Total electricity', errors)
    utility = _as_units(raw.get('utility_units'), 'Utility', errors)

    values = {
        'index': index,
        'date': row_date,
        'shift': shift,
        'spec': spec,
        'total_counter_production': production,
        'rejection_pcs': rejects,
        'own_labour_count': own,
        'contract_labour_count': contract,
        'operator_count': operators,
        'machine_units': units,
        'utility_units': utility,
        'add_anyway': raw.get('add_anyway') is True,
    }
    return values, errors


def _existing_runs(company, machine, dates):
    """The machine's runs from the last metered day before the sheet onwards.

    That is every run the sheet's rows can sit between, plus the one whose stop
    reading the first row starts from — without loading the machine's history.
    The anchor is the last day with a stop reading, not just the last day: a
    draft opened and never finished has none, and must not hide the reading
    before it.
    """
    first = min(dates)
    before = (
        BlowingRun.objects
        .filter(company=company, machine=machine, date__lt=first,
                machine_stop_reading__isnull=False)
        .aggregate(latest=Max('date'))['latest']
    )
    return list(
        BlowingRun.objects
        .filter(company=company, machine=machine, date__gte=before or first)
        .select_related('preform_spec')
        .prefetch_related('segments')
    )


def _describe(run, shift):
    started = _run_started(run)
    segments = [s for s in run.segments.all() if s.start_time]
    ended = max((s.end_time for s in segments if s.end_time), default=None)
    return {
        'id': run.id,
        'date': run.date.isoformat(),
        'run_number': run.run_number,
        'shift': shift,
        'preform_spec_id': run.preform_spec_id,
        'preform': _spec_label(run.preform_spec),
        'total_counter_production': run.total_counter_production,
        'status': run.status,
        'started_at': timezone.localtime(started, IST).isoformat() if segments else None,
        'ended_at': timezone.localtime(ended, IST).isoformat() if ended else None,
        'machine_start_reading': _str(run.machine_start_reading),
        'machine_stop_reading': _str(run.machine_stop_reading),
        'remarks': run.remarks,
    }


def _str(value):
    return None if value is None else str(value)


def _slot(run_or_row):
    return f"{run_or_row['date']:%d %b} {SHIFT_LABEL.get(run_or_row['shift'], '?')}"


def plan_rows(company, machine, rows):
    """What each row would become, without writing anything.

    Returns ``{'rows': [...], 'existing': [...], 'summary': {...}}``. Each row
    has a ``status``:

    * ``NEW`` — it will be booked, with the run number, meter readings and
      cost shown;
    * ``DUPLICATE`` — the app already has this date, shift and preform, so it
      is skipped (``add_anyway`` books it regardless);
    * ``ERROR`` — something on the row must be fixed first.
    """
    today = timezone.localtime(timezone.now(), IST).date()
    specs = {spec.id: spec for spec in PreformSpec.objects.filter(company=company)}
    cleaned = [_clean_row(index, raw, specs, today) for index, raw in enumerate(rows)]

    dates = {values['date'] for values, _ in cleaned if values.get('date')}
    existing = _existing_runs(company, machine, dates) if dates else []
    shifts = {run.id: run_shift(run) for run in existing}

    results = []
    seen = {}
    for values, errors in cleaned:
        result = {
            'index': values['index'], 'status': 'NEW', 'errors': list(errors),
            'warnings': [], 'duplicate_of': None, 'run_number': None,
            'machine_start_reading': None, 'machine_stop_reading': None,
            'good_bottles': None, 'blowing_cost': None,
            'blowing_cost_per_bottle': None, 'total_per_bottle_cost': None,
        }
        results.append(result)
        if result['errors']:
            result['status'] = 'ERROR'
            continue

        key = (values['date'], values['shift'], values['spec'].id)
        if key in seen:
            result['status'] = 'ERROR'
            result['errors'].append(
                f"Row {seen[key] + 1} already has {_slot(values)} on "
                f"{_spec_label(values['spec'])}.")
            continue
        seen[key] = values['index']

        same_slot = [
            run for run in existing
            if run.date == values['date'] and shifts[run.id] == values['shift']
        ]
        twin = next((run for run in same_slot if run.preform_spec_id == values['spec'].id), None)
        if twin is not None:
            result['duplicate_of'] = _describe(twin, shifts[twin.id])
            if values['add_anyway']:
                result['warnings'].append(
                    f'Booked beside run {twin.run_number}, which already has this shift.')
            else:
                result['status'] = 'DUPLICATE'
                continue
        for run in same_slot:
            if run is not twin:
                result['warnings'].append(
                    f'The app already has run {run.run_number} for this shift on '
                    f'{_spec_label(run.preform_spec)} '
                    f'({run.total_counter_production:,} bottles). A SKU change in the '
                    f'shift is fine — check this is not the same production.')
        if values['machine_units'] == 0:
            result['warnings'].append('No electricity given — the meter will not move.')

    # ---- meter chain + run numbers, in the order the shifts ran ------------
    booking = [
        (values, result) for (values, _), result in zip(cleaned, results)
        if result['status'] == 'NEW'
    ]
    timeline = [
        ((run.date, _SHIFT_RANK.get(shifts[run.id], 0), 0, _run_started(run), run.run_number),
         'run', run)
        for run in existing
    ] + [
        ((values['date'], _SHIFT_RANK[values['shift']], 1, None, values['index']),
         'row', (values, result))
        for values, result in booking
    ]
    # ``None`` never compares against a datetime: rows and runs differ at the
    # third key, so the fourth is only compared run to run.
    timeline.sort(key=lambda item: item[0])

    next_number = {
        entry['date']: entry['top']
        for entry in BlowingRun.objects
        .filter(company=company, date__in={values['date'] for values, _ in booking})
        .values('date').annotate(top=Max('run_number'))
    }
    last_stop = None
    last_booked = None  # the row booked since the last run in the app with a start reading
    for _, kind, item in timeline:
        if kind == 'run':
            if item.machine_start_reading is not None and last_booked is not None:
                stop = Decimal(last_booked['machine_stop_reading'])
                if abs(item.machine_start_reading - stop) > _METER_TOLERANCE:
                    last_booked['warnings'].append(
                        f'The next run in the app (run {item.run_number}, '
                        f'{item.date:%d %b} {SHIFT_LABEL.get(shifts[item.id], "")}) starts '
                        f'the meter at {item.machine_start_reading}, but this row stops '
                        f'it at {stop}.')
                last_booked = None
            if item.machine_stop_reading is not None:
                last_stop = item.machine_stop_reading
            continue

        values, result = item
        if last_stop is None:
            result['warnings'].append(
                'No earlier meter reading on this machine — the meter starts at 0.')
            last_stop = Decimal('0')
        start = last_stop
        stop = start + values['machine_units']
        last_stop = stop
        last_booked = result
        number = next_number.get(values['date'], 0) + 1
        next_number[values['date']] = number
        result.update({
            'run_number': number,
            'machine_start_reading': str(start),
            'machine_stop_reading': str(stop),
        })
        values['machine_start_reading'] = start
        values['machine_stop_reading'] = stop
        _preview_cost(company, machine, values, result)

    summary = {status: 0 for status in ('NEW', 'DUPLICATE', 'ERROR')}
    for result in results:
        summary[result['status']] += 1
    existing_on_dates = sorted(
        (_describe(run, shifts[run.id]) for run in existing if run.date in dates),
        key=lambda d: (d['date'], d['run_number']),
    )
    return {
        'machine': {'id': machine.id, 'name': machine.name},
        'rows': results,
        'existing': existing_on_dates,
        'summary': summary,
        '_cleaned': {values['index']: values for values, _ in cleaned},
    }


def _unsaved_run(company, machine, values):
    spec = values['spec']
    run = BlowingRun(
        company=company, machine=machine, preform_spec=spec, date=values['date'],
        status=RunStatus.COMPLETED,
        machine_start_reading=values['machine_start_reading'],
        machine_stop_reading=values['machine_stop_reading'],
        utility_units=values['utility_units'],
        total_counter_production=values['total_counter_production'],
        rejection_pcs=values['rejection_pcs'],
        operator_count=values['operator_count'],
        own_labour_count=values['own_labour_count'],
        contract_labour_count=values['contract_labour_count'],
        preform_rate_per_bottle=spec.preform_rate_per_bottle or 0,
    )
    run._recompute_derived()
    return run


def _preview_cost(company, machine, values, result):
    try:
        cost = compute_run_cost(_unsaved_run(company, machine, values))
    except Exception as exc:  # noqa: BLE001 - a preview must not sink the plan
        result['warnings'].append(f'The cost could not be worked out yet ({exc}).')
        return
    result.update({
        'good_bottles': cost['good_bottles'],
        'blowing_cost': str(round(cost['blowing_cost'], 2)),
        'blowing_cost_per_bottle': str(round(cost['blowing_cost_per_bottle'], 4)),
        'total_per_bottle_cost': str(round(cost['total_per_bottle_cost'], 4)),
    })


def public_plan(plan):
    """The plan as the API returns it (the cleaned values stay server-side)."""
    return {key: value for key, value in plan.items() if not key.startswith('_')}


# ===========================================================================
# Booking
# ===========================================================================

@transaction.atomic
def apply_rows(company, machine_id, rows, user, source=''):
    """Book the plan's NEW rows as completed runs, or book nothing.

    The machine row is locked first, so two people booking sheets for the same
    machine at once queue rather than both chaining from the same stop reading.
    The plan is then made again inside the lock — what was checked on screen a
    minute ago is not trusted to still be true.
    """
    from .blowing_service import BlowingService

    machine = (
        BlowingMachine.objects.select_for_update()
        .filter(id=machine_id, company=company, is_active=True).first()
    )
    if machine is None:
        raise ValueError('That blowing machine is not an active machine of this company.')
    plan = plan_rows(company, machine, rows)
    summary = plan['summary']
    if summary['ERROR']:
        raise ShiftSheetRefused(
            f"{summary['ERROR']} row(s) need fixing before anything is saved.", public_plan(plan))
    if not summary['NEW']:
        raise ShiftSheetRefused(
            'Nothing to add — every row is already in the app.', public_plan(plan))

    service = BlowingService(company.code)
    cleaned = plan['_cleaned']
    booked = sorted(
        (result for result in plan['rows'] if result['status'] == 'NEW'),
        key=lambda r: (cleaned[r['index']]['date'], r['run_number']),
    )
    note = f' ({source})' if source else ''
    created = []
    for result in booked:
        values = cleaned[result['index']]
        shift = values['shift']
        run = service.create_run({
            'machine_id': machine.id,
            'preform_spec_id': values['spec'].id,
            'date': values['date'],
            'status': RunStatus.COMPLETED,
            'machine_start_reading': values['machine_start_reading'],
            'machine_stop_reading': values['machine_stop_reading'],
            'utility_units': values['utility_units'],
            'total_counter_production': values['total_counter_production'],
            'rejection_pcs': values['rejection_pcs'],
            'operator_count': values['operator_count'],
            'own_labour_count': values['own_labour_count'],
            'contract_labour_count': values['contract_labour_count'],
            'preform_boxes_used': Decimal('0'),
            'remarks': f'Entered from the shift sheet{note} — {SHIFT_LABEL[shift]} shift',
        }, user=user)
        created_at = datetime.combine(values['date'], _SHIFT_CREATED_AT[shift], tzinfo=IST)
        # No preform request for a shift that has already run; the run is dated
        # to its shift, not to the morning it was typed in.
        BlowingRun.objects.filter(pk=run.pk).update(
            warehouse_approval_status=WarehouseApprovalStatus.APPROVED,
            created_at=created_at,
        )
        result['run_id'] = run.id
        result['run_number'] = run.run_number
        created.append({
            'index': result['index'], 'id': run.id, 'run_number': run.run_number,
            'date': values['date'].isoformat(), 'shift': shift,
        })
    return {**public_plan(plan), 'created': created}
