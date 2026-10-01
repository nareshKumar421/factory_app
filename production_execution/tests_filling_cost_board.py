"""Tests for Beverages' filling cost board — the saved sheets, by month and by day.

Run with:
    .venv/bin/python manage.py test production_execution.tests_filling_cost_board \
        --settings=config.sqlite_test_settings
"""
from datetime import date, datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from company.models import Company, UserRole

from .models import (
    FillingCostSheet, FillingCostSheetEntry, ProductionLine, ProductionRun, ProductionSegment,
)
from . import tests_filling_cost


class FillingCostBoardTests(APITestCase):
    """September: the 26th with no runs, the 27th a whole day, the 28th by shift."""

    _user = tests_filling_cost.FillingCostSheetTests._user

    def setUp(self):
        self.company = Company.objects.create(name='Jivo Beverages', code='JIVO_BEVERAGES')
        self.role = UserRole.objects.create(name='Accounts')
        self.line = ProductionLine.objects.create(company=self.company, name='Sidel')
        self.client.force_authenticate(
            self._user('board@bev.test', 'E001', ['can_view_filling_cost']))
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)
        self.url = reverse('pe-filling-cost-board')

        self._sheet('2026-08-15', '', '100', [('Misc', '250')])       # August: 2.50 a case
        self._sheet('2026-09-26', '', '10', [('Lab', '10')])          # no runs: no bottles
        self._sheet('2026-09-27', '', '1000', [('Salary', '500'), ('Electricity', '500')])
        self._sheet('2026-09-28', 'DAY', '3000',
                    [('Fixed Manpower', '3000'), ('Electricity', '3000')])
        self._sheet('2026-09-28', 'NIGHT', '5655', [('Fixed Manpower', '5655')])
        # Neither counts: the 28th is kept by shift, and a line's sheet is not the floor's.
        self._sheet('2026-09-28', '', '8655', [('Fixed Manpower', '99999')])
        self._sheet('2026-09-28', 'NIGHT', '5655', [('Lab', '99999')], line=self.line)

        # The runs the bottles come from: 24 a case by day, 12 by night.
        self._run(1, '2026-09-27T08:00', '2026-09-27T12:00', '1000', 24)
        self._run(2, '2026-09-28T08:00', '2026-09-28T18:00', '3000', 24)
        self._run(3, '2026-09-28T20:00', '2026-09-29T06:00', '5655', 12)

    def _sheet(self, day, shift, cases, rows, line=None):
        sheet = FillingCostSheet.objects.create(
            company=self.company, line=line, date=date.fromisoformat(day), shift=shift,
            cases=Decimal(cases))
        for order, (head, amount) in enumerate(rows):
            FillingCostSheetEntry.objects.create(
                sheet=sheet, head=head, amount=Decimal(amount), sort_order=order)
        return sheet

    def _run(self, number, start, end, cases, pieces):
        start, end = (timezone.make_aware(datetime.fromisoformat(t)) for t in (start, end))
        run = ProductionRun.objects.create(
            company=self.company, line=self.line, run_number=number,
            date=start.date(), status='IN_PROGRESS', pieces_per_case=pieces)
        ProductionSegment.objects.create(
            production_run=run, start_time=start, end_time=end,
            produced_cases=Decimal(cases), is_active=False)

    def _board(self, **params):
        response = self.client.get(self.url, {'month': '2026-09', 'day': '2026-09-28', **params})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    # -- the month ---------------------------------------------------------------

    def test_the_month_adds_up_the_days_that_make_it(self):
        data = self._board()
        self.assertEqual(data['days_entered'], 3)
        self.assertEqual(data['days_in_month'], 30)
        self.assertEqual(data['totals'], {
            'cases': '9665.00',          # 10 + 1,000 + 3,000 + 5,655
            'bottles': '163860',         # 24,000 + 72,000 + 67,860
            'total': '12665.00',
            'per_case': '1.31',
            # Over the bottled days only: the 26th's 10 rupees are not spread
            # over bottles nobody counted. 12,655 / 1,63,860.
            'per_bottle': '0.0772',
        })

    def test_each_day_is_its_sheets_and_its_runs_bottles(self):
        days = {d['date']: d for d in self._board()['days']}
        self.assertEqual(list(days), ['2026-09-26', '2026-09-27', '2026-09-28'])
        self.assertEqual(days['2026-09-28']['kept_by'], 'shift')
        self.assertEqual(days['2026-09-28']['total'], '11655.00')     # not the 99,999s
        self.assertEqual(days['2026-09-28']['bottles'], '139860')
        self.assertEqual(days['2026-09-27']['per_bottle'], '0.0417')   # 1,000 / 24,000
        self.assertIsNone(days['2026-09-26']['per_bottle'])
        self.assertEqual(days['2026-09-26']['per_case'], '1.00')

    def test_heads_add_up_across_the_month_under_todays_names(self):
        heads = {h['head']: h for h in self._board()['heads']}
        # The 27th's 'Salary' is the 28th's 'Fixed Manpower'.
        self.assertEqual(set(heads), {'Fixed Manpower', 'Electricity', 'Lab'})
        self.assertEqual(heads['Fixed Manpower']['amount'], '9155.00')
        self.assertEqual(heads['Fixed Manpower']['share'], '72.29')
        self.assertEqual(heads['Electricity']['per_case'], '0.36')    # 3,500 / 9,665
        # Lab was only spent on the 26th, which had no runs to count bottles from.
        self.assertIsNone(heads['Lab']['per_bottle'])

    def test_the_month_before_is_there_to_compare(self):
        self.assertEqual(self._board()['previous'],
                         {'month': '2026-08', 'per_case': '2.50', 'days_entered': 1})

    # -- the day -------------------------------------------------------------------

    def test_the_day_shows_every_head_and_each_shift(self):
        day = self._board()['day']
        self.assertEqual(day['date'], '2026-09-28')
        self.assertEqual((day['cases'], day['total'], day['per_case'], day['per_bottle']),
                         ('8655.00', '11655.00', '1.35', '0.0833'))
        self.assertEqual([h['head'] for h in day['heads']], ['Fixed Manpower', 'Electricity'])
        shifts = {s['shift']: s for s in day['shifts']}
        # Each shift's bottles from its own runs: 24 a case by day, 12 by night.
        self.assertEqual(shifts['DAY']['bottles'], '72000')
        self.assertEqual(shifts['NIGHT']['bottles'], '67860')
        self.assertEqual(shifts['NIGHT']['per_bottle'], '0.0833')

    def test_each_shift_reads_like_the_factorys_sheet(self):
        day = self._board()['day']
        night = {s['shift']: s for s in day['shifts']}['NIGHT']
        # The SKU and box size the shift filled, from its runs.
        self.assertEqual(night['skus'], [
            {'product': 'Sidel', 'sku': 'Sidel', 'pieces_per_case': 12,
             'litres_per_piece': None, 'cases': '5655.00'}])
        # Its own heads, in the order the sheet was written.
        self.assertEqual([(h['head'], h['amount'], h['per_case']) for h in night['heads']],
                         [('Fixed Manpower', '5655.00', '1.00')])
        day_shift = {s['shift']: s for s in day['shifts']}['DAY']
        self.assertEqual([h['head'] for h in day_shift['heads']],
                         ['Fixed Manpower', 'Electricity'])
        self.assertEqual([h['head'] for h in day['sheet_heads']],
                         ['Fixed Manpower', 'Electricity'])

    def test_a_day_nobody_entered_has_no_detail(self):
        self.assertIsNone(self._board(day='2026-09-20')['day'])

    def test_a_day_outside_the_month_is_still_shown(self):
        day = self._board(month='2026-08', day='2026-09-27')['day']
        self.assertEqual(day['total'], '1000.00')

    def test_the_day_defaults_to_yesterday(self):
        from unittest import mock

        with mock.patch('django.utils.timezone.localdate', return_value=date(2026, 9, 29)):
            data = self.client.get(self.url).data
        self.assertEqual(data['selected_day'], '2026-09-28')
        self.assertEqual(data['month'], '2026-09')

    # -- who ---------------------------------------------------------------------

    def test_bad_dates_are_refused(self):
        for params in ({'day': '28-09-2026'}, {'month': 'Sep'}):
            self.assertEqual(self.client.get(self.url, params).status_code,
                             status.HTTP_400_BAD_REQUEST)

    def test_another_companys_sheets_are_not_counted(self):
        oil = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        FillingCostSheet.objects.create(company=oil, date=date(2026, 9, 27), cases=5)
        self.assertEqual(self._board()['days_entered'], 3)

    def test_only_beverages_and_holders_of_the_right_reach_it(self):
        outsider = self._user('nobody@bev.test', 'E009', [])
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)
