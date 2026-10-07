"""Tests for the filling cost sheet — the day's filling cost, typed in.

Run with:
    .venv/bin/python manage.py test production_execution.tests_filling_cost

The sheet the factory hands over is the fixture: Salary 12,00,000 down to
Miscellaneous 1,00,000, over 1,60,000 cases, totalling 24,88,400 and 15.55 a
case. What is under test is that the page gives those numbers back — including
the total, which the sheet writes as 15.55 and the per-case column would add up
to 15.56 — and that only Beverages can reach it at all.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from company.models import Company, UserCompany, UserRole

from .models import FillingCostSheet, ProductionLine

User = get_user_model()

# The sheet as it is written, head by head, in its own order.
SHEET = [
    ('Salary', '1200000'),
    ('Electricity', '800000'),
    ('Maintenance', '250000'),
    ('Batch Coding', '60000'),
    ('Ground Water Extraction Bill', '16000'),
    ('Briquette', '25000'),
    ('Lubrication', '32400'),
    ('Lab', '5000'),
    ('Miscellaneous', '100000'),
]
CASES = '160000'


def entries_payload(rows=SHEET):
    return [{'head': head, 'amount': amount} for head, amount in rows]


class FillingCostSheetTests(APITestCase):
    maxDiff = None

    def setUp(self):
        # The sheet is Beverages' — see InFillingCostCompany.
        self.company = Company.objects.create(
            name='Jivo Beverages', code='JIVO_BEVERAGES')
        self.role = UserRole.objects.create(name='Accounts')
        self.line = ProductionLine.objects.create(company=self.company, name='Line 1')

        self.user = self._user('entry@bev.test', 'E001', [
            'can_view_filling_cost', 'can_manage_filling_cost',
        ])
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        self.list_url = reverse('pe-filling-cost-list-create')

    def _user(self, email, employee_code, codenames, company=None):
        user = User.objects.create_user(
            email=email, password='pw', full_name=email.split('@')[0],
            employee_code=employee_code,
        )
        UserCompany.objects.create(
            user=user, company=company or self.company, role=self.role,
            is_default=True,
        )
        if codenames:
            user.user_permissions.add(*Permission.objects.filter(
                content_type__app_label='production_execution',
                codename__in=codenames,
            ))
        return User.objects.get(pk=user.pk)  # has_perm caches per instance

    def _detail_url(self, sheet_id):
        return reverse('pe-filling-cost-detail', args=[sheet_id])

    def _post(self, **overrides):
        payload = {
            'date': '2026-09-01',
            'cases': CASES,
            'entries': entries_payload(),
        }
        payload.update(overrides)
        return self.client.post(self.list_url, payload, format='json')

    # -- entry ------------------------------------------------------------

    def test_sheet_is_stored_and_read_back_per_case(self):
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        body = response.data
        self.assertEqual(Decimal(body['total_amount']), Decimal('2488400.00'))
        # The sheet's own total row: 24,88,400 / 1,60,000 = 15.5525 -> 15.55.
        # Adding the rounded per-case column instead would read 15.56.
        self.assertEqual(Decimal(body['total_per_case']), Decimal('15.55'))

        per_case = {e['head']: Decimal(e['per_case']) for e in body['entries']}
        self.assertEqual(per_case['Salary'], Decimal('7.50'))
        self.assertEqual(per_case['Electricity'], Decimal('5.00'))
        self.assertEqual(per_case['Maintenance'], Decimal('1.56'))
        # 0.375 a case: the sheet rounds it up, as the factory writes it.
        self.assertEqual(per_case['Batch Coding'], Decimal('0.38'))
        self.assertEqual(per_case['Ground Water Extraction Bill'], Decimal('0.10'))
        self.assertEqual(per_case['Briquette'], Decimal('0.16'))
        self.assertEqual(per_case['Lubrication'], Decimal('0.20'))
        self.assertEqual(per_case['Lab'], Decimal('0.03'))
        self.assertEqual(per_case['Miscellaneous'], Decimal('0.63'))

    def test_rows_keep_the_order_they_were_entered_in(self):
        self._post()
        sheet = FillingCostSheet.objects.get()
        self.assertEqual(
            [e.head for e in sheet.entries.all()],
            [head for head, _ in SHEET],
        )

    def test_a_sheet_is_kept_for_its_own_day(self):
        response = self._post(date='2026-09-17')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['date'], '2026-09-17')
        # The next day of the same month is a sheet of its own.
        response = self._post(date='2026-09-18')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(FillingCostSheet.objects.count(), 2)

    def test_a_sheet_needs_its_case_count(self):
        # No standing default: a day's cases are that day's, not a month's.
        response = self.client.post(
            self.list_url,
            {'date': '2026-09-01', 'entries': entries_payload()},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('cases', response.data['errors'])

    def test_a_sheet_can_be_entered_for_one_line(self):
        response = self._post(line_id=self.line.id)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['line_name'], 'Line 1')
        # The floor-wide sheet for the same day is a different sheet.
        self.assertEqual(self._post().status_code, status.HTTP_201_CREATED)

    def test_second_sheet_for_the_same_day_is_refused(self):
        self._post()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('1 September 2026', response.data['detail'])

    def test_a_head_cannot_be_listed_twice(self):
        response = self._post(entries=entries_payload(
            [('Salary', '1'), ('salary', '2')]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_sheet_needs_at_least_one_head(self):
        response = self._post(entries=[])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cases_cannot_be_zero(self):
        response = self._post(cases='0')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_companys_line_is_refused(self):
        other = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        other_line = ProductionLine.objects.create(company=other, name='Line X')
        response = self._post(line_id=other_line.id)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- correcting a sheet -------------------------------------------------

    def test_saving_again_replaces_the_rows(self):
        sheet_id = self._post().data['id']
        response = self.client.patch(
            self._detail_url(sheet_id),
            {'entries': entries_payload([('Salary', '1200000'), ('Lab', '5000')])},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual([e['head'] for e in response.data['entries']],
                         ['Salary', 'Lab'])
        self.assertEqual(Decimal(response.data['total_amount']), Decimal('1205000.00'))

    def test_changing_the_case_count_reprices_every_row(self):
        sheet_id = self._post().data['id']
        response = self.client.patch(
            self._detail_url(sheet_id), {'cases': '80000'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        per_case = {e['head']: Decimal(e['per_case']) for e in response.data['entries']}
        self.assertEqual(per_case['Salary'], Decimal('15.00'))
        self.assertEqual(Decimal(response.data['total_per_case']), Decimal('31.11'))

    def test_moving_a_sheet_onto_a_day_already_entered_is_refused(self):
        self._post(date='2026-09-01')
        sheet_id = self._post(date='2026-09-02').data['id']
        response = self.client.patch(
            self._detail_url(sheet_id), {'date': '2026-09-01'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FillingCostSheet.objects.get(id=sheet_id).date.day, 2)

    def test_a_sheet_can_be_deleted_with_its_rows(self):
        sheet_id = self._post().data['id']
        response = self.client.delete(self._detail_url(sheet_id))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(FillingCostSheet.objects.exists())

    # -- listing ------------------------------------------------------------

    def test_list_is_newest_day_first_and_filters_by_day(self):
        self._post(date='2026-09-24')
        self._post(date='2026-09-25')
        response = self.client.get(self.list_url)
        self.assertEqual([s['date'] for s in response.data],
                         ['2026-09-25', '2026-09-24'])

        response = self.client.get(self.list_url, {'date': '2026-09-24'})
        self.assertEqual([s['date'] for s in response.data], ['2026-09-24'])
        # A day nobody entered finds nothing, not the nearest day.
        response = self.client.get(self.list_url, {'date': '2026-09-23'})
        self.assertEqual(response.data, [])

    def test_list_can_be_limited_to_the_newest_days(self):
        for day in ('2026-09-23', '2026-09-24', '2026-09-25'):
            self._post(date=day)
        response = self.client.get(self.list_url, {'limit': 2})
        self.assertEqual([s['date'] for s in response.data],
                         ['2026-09-25', '2026-09-24'])
        self.assertEqual(self.client.get(self.list_url, {'limit': '0'}).status_code,
                         status.HTTP_400_BAD_REQUEST)

    def test_list_filters_to_a_line_or_to_the_floor_wide_sheet(self):
        self._post()
        self._post(line_id=self.line.id)

        response = self.client.get(self.list_url, {'line_id': self.line.id})
        self.assertEqual([s['line'] for s in response.data], [self.line.id])

        response = self.client.get(self.list_url, {'line_id': 'none'})
        self.assertEqual([s['line'] for s in response.data], [None])

    def test_another_companys_sheets_are_not_listed(self):
        other = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        FillingCostSheet.objects.create(
            company=other, date='2026-09-01', cases=Decimal(CASES))
        response = self.client.get(self.list_url)
        self.assertEqual(response.data, [])

    # -- who may see it -----------------------------------------------------

    def test_entering_needs_the_manage_permission(self):
        reader = self._user('reader@bev.test', 'E002', ['can_view_filling_cost'])
        self.client.force_authenticate(reader)
        self.assertEqual(self._post().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(self.list_url).status_code, status.HTTP_200_OK)

    def test_run_cost_holders_read_the_sheet(self):
        self._post()
        coster = self._user('cost@bev.test', 'E003', ['can_view_run_cost'])
        self.client.force_authenticate(coster)
        self.assertEqual(self.client.get(self.list_url).status_code, status.HTTP_200_OK)

    def test_the_sheet_is_closed_to_everyone_else(self):
        outsider = self._user('nobody@bev.test', 'E004', [])
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(self.list_url).status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_no_other_company_reaches_the_sheet(self):
        # The permission travels with the user, the sheet does not: the same
        # holder working under Oil is refused, so a second company's sheet
        # cannot be started at all.
        oil = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        user = self._user('entry@oil.test', 'E005', [
            'can_view_filling_cost', 'can_manage_filling_cost',
        ], company=oil)
        self.client.force_authenticate(user)
        self.client.credentials(HTTP_COMPANY_CODE=oil.code)

        self.assertEqual(self.client.get(self.list_url).status_code,
                         status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._post().status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(FillingCostSheet.objects.exists())




class FillingCostDefaultsTests(APITestCase):
    """A new sheet opens worked out from the shift's runs and the Cost Master.

    The fixture is the factory's own night-shift sheet for the 28th: 500 ML at
    24 a box, 5,655 boxes, Fixed Manpower 46,154 and so on down to the total.
    """

    RATES = [
        ('Fixed Manpower', 'PER_MONTH', '1200000'),
        ('Maintenance', 'PER_MONTH', '250000'),
        ('Batch Coding', 'PER_BOTTLE', '0.03'),
        ('Lubrication', 'PER_LITRE', '0.0133'),
        ('Lab', 'PER_MONTH', '5000'),
        ('Miscellaneous', 'PER_MONTH', '10000'),
        ('Scrap Recovering', 'PER_KG', '7.5'),
    ]
    DAY = '2026-09-28'

    def setUp(self):
        from datetime import date
        from unittest import mock

        from cost_master.codes import FILLING_COST_TYPES
        from cost_master.models import CostRate, CostType

        self.company = Company.objects.create(
            name='Jivo Beverages', code='JIVO_BEVERAGES')
        self.role = UserRole.objects.create(name='Accounts')
        self.line = ProductionLine.objects.create(company=self.company, name='Sidel')
        # Put in place by cost_master migrations 0004/0005; made here as well
        # because the test settings build the schema without migrations.
        self.types = {}
        for head, basis, rate in self.RATES:
            code, name, _, _ = FILLING_COST_TYPES[head]
            self.types[head], _ = CostType.objects.get_or_create(
                code=code, defaults={'name': name, 'default_basis': basis,
                                     'is_credit': head == 'Scrap Recovering'})
            CostRate.objects.create(
                cost_type=self.types[head], scope='COMPANY', company=self.company,
                basis=basis, rate=Decimal(rate), effective_from=date(2026, 9, 1))

        # Electricity++ for the day, as its allocation would report it.
        patcher = mock.patch(
            'production_execution.services.filling_cost._electricity',
            return_value=(Decimal('32787'), {'Production Floor Beverage': Decimal('32787')}, {}))
        self.electricity = patcher.start()
        self.addCleanup(patcher.stop)
        # No night readings unless a test reads the meter twice.
        rounds = mock.patch(
            'production_execution.services.filling_cost._meter_rounds', return_value={})
        self.rounds = rounds.start()
        self.addCleanup(rounds.stop)

        self.user = FillingCostSheetTests._user(
            self, 'entry@bev.test', 'E001', ['can_view_filling_cost'])
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)
        self.url = reverse('pe-filling-cost-defaults')

    # -- fixtures ---------------------------------------------------------------

    def _at(self, day, clock):
        from datetime import datetime

        from django.utils import timezone

        return timezone.make_aware(datetime.fromisoformat(f"{day}T{clock}"))

    def _run(self, number, segments, *, status_='IN_PROGRESS', total='0', line=None,
             run_date=None, pieces=24, litres='0.5'):
        """``segments``: (start, end, cases) with start/end as 'YYYY-MM-DD HH:MM'."""
        from datetime import date

        from .models import ProductionRun, ProductionSegment

        run = ProductionRun.objects.create(
            company=self.company, line=line or self.line, run_number=number,
            date=run_date or date.fromisoformat(self.DAY), status=status_,
            total_production=Decimal(total), pieces_per_case=pieces,
            litres_per_piece=Decimal(litres) if litres else None,
            product='JIVO WATER 500 ML')
        for start, end, cases in segments:
            ProductionSegment.objects.create(
                production_run=run,
                start_time=self._at(*start.split()), end_time=self._at(*end.split()),
                produced_cases=Decimal(cases), is_active=False)
        return run

    def _waste(self, run, qty, uom='KG', price='10', code='PF-500'):
        from .models import ProductionMaterialUsage, WasteLog

        ProductionMaterialUsage.objects.get_or_create(
            production_run=run, material_code=code,
            defaults={'material_name': 'PREFORM 500 ML',
                      'unit_price': Decimal(price) if price else None})
        WasteLog.objects.create(
            production_run=run, company=self.company, material_code=code,
            material_name='PREFORM 500 ML', wastage_qty=Decimal(qty), uom=uom)

    def _night_of_the_28th(self):
        run = self._run(1, [('2026-09-28 20:00', '2026-09-29 06:00', '5655')])
        self._waste(run, '412.1')        # 412.1 kg at ₹10 = ₹4,121
        return run

    def _open(self, shift='', **params):
        response = self.client.get(self.url, {'date': self.DAY, 'shift': shift, **params})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def _amounts(self, data):
        return {e['head']: e['amount'] for e in data['entries']}

    # -- the manual sheet -------------------------------------------------------

    def test_the_night_shift_opens_as_the_factory_writes_it(self):
        self._night_of_the_28th()
        data = self._open('NIGHT')

        self.assertEqual(data['produced_cases'], '5655.00')
        # The sheet's head: SKU (its bottle), box size, boxes.
        self.assertEqual([(s['sku'], s['pieces_per_case'], s['cases']) for s in data['skus']],
                         [('500 ML', 24, '5655.00')])
        self.assertEqual(data['bottles'], '135720')          # 5,655 x 24
        self.assertEqual(data['litres'], '67860')            # x 0.5 L
        self.assertEqual(self._amounts(data), {
            'Electricity': '32787.00',
            'Fixed Manpower': '46153.85',   # 12,00,000 / 26; only night ran
            'Maintenance': '9615.38',
            'Batch Coding': '4071.60',      # 1,35,720 bottles x 0.03
            'Lubrication': '902.54',        # 67,860 L x 0.0133 (0.2 / 15)
            'Lab': '192.31',
            'Miscellaneous': '384.62',
            'Scrap Recovering': '-3090.75',  # a credit: 412.1 kg x 7.5
            'Wastage': '4121.00',
        })
        self.assertEqual(data['warnings'], [])

    def test_each_figure_says_where_it_came_from(self):
        self._night_of_the_28th()
        explain = {e['head']: e['explain'] for e in self._open('NIGHT')['entries']}
        self.assertEqual(explain['Fixed Manpower'], '₹12,00,000 a month ÷ 26 days')
        self.assertEqual(explain['Batch Coding'], '1,35,720 bottles × ₹0.03')
        self.assertIn('32,787', explain['Electricity'])
        self.assertIn('412.100 kg', explain['Scrap Recovering'])

    def test_the_day_shift_of_a_night_only_day_takes_nothing(self):
        self._night_of_the_28th()
        data = self._open('DAY')
        amounts = self._amounts(data)
        self.assertEqual(data['produced_cases'], '0.00')
        self.assertEqual(amounts['Fixed Manpower'], '0.00')
        self.assertEqual(amounts['Electricity'], '0.00')

    # -- two shifts -------------------------------------------------------------

    def test_two_shifts_share_the_day(self):
        self._night_of_the_28th()                                   # 10 h, 5,655
        self._run(2, [('2026-09-28 07:00', '2026-09-28 19:00', '3000')])  # 12 h

        night, day = self._amounts(self._open('NIGHT')), self._amounts(self._open('DAY'))
        # The monthly heads by running hours: 10 of 22, and 12 of 22.
        self.assertEqual(night['Fixed Manpower'], '20979.02')
        self.assertEqual(day['Fixed Manpower'], '25174.83')
        # Electricity by cases: 5,655 and 3,000 of 8,655.
        self.assertEqual(night['Electricity'], '21422.36')
        self.assertEqual(day['Electricity'], '11364.64')
        whole = self._amounts(self._open(''))
        self.assertEqual(whole['Fixed Manpower'], '46153.85')
        self.assertEqual(whole['Electricity'], '32787.00')

    def test_a_meter_read_by_day_and_by_night_splits_by_its_readings(self):
        self._night_of_the_28th()
        self._run(2, [('2026-09-28 07:00', '2026-09-28 19:00', '3000')])
        self.rounds.return_value = {
            'Production Floor Beverage': {'DAY': Decimal('3000'), 'NIGHT': Decimal('1000')}}

        night = {e['head']: e for e in self._open('NIGHT')['entries']}['Electricity']
        self.assertEqual(night['amount'], '8196.75')             # a quarter of 32,787
        self.assertIn('1 meter by their night readings', night['explain'])
        day = {e['head']: e for e in self._open('DAY')['entries']}['Electricity']
        self.assertEqual(day['amount'], '24590.25')

    def test_a_run_across_shifts_is_split_by_its_segments(self):
        # 600 logged by day and 200 by night; 1,000 entered at completion.
        self._run(3, [('2026-09-28 15:00', '2026-09-28 19:00', '600'),
                      ('2026-09-28 19:00', '2026-09-28 21:00', '200')],
                  status_='COMPLETED', total='1000')
        self.assertEqual(self._open('DAY')['produced_cases'], '750.00')
        self.assertEqual(self._open('NIGHT')['produced_cases'], '250.00')
        self.assertEqual(self._open('')['produced_cases'], '1000.00')

    def test_a_night_runs_past_midnight_on_its_own_date(self):
        # Segment starts 01:00 on the 29th: still the night of the 28th.
        self._run(4, [('2026-09-29 01:00', '2026-09-29 05:00', '900')])
        self.assertEqual(self._open('NIGHT')['produced_cases'], '900.00')

    def test_a_line_takes_only_its_runs_and_its_part_of_the_day(self):
        other = ProductionLine.objects.create(company=self.company, name='Krones')
        self._night_of_the_28th()                                   # Sidel, 10 h
        self._run(5, [('2026-09-28 20:00', '2026-09-29 06:00', '2000')], line=other)

        data = self._open('NIGHT', line_id=self.line.id)
        self.assertEqual(data['produced_cases'], '5655.00')
        amounts = self._amounts(data)
        self.assertEqual(amounts['Fixed Manpower'], '23076.92')     # half the hours
        self.assertEqual(amounts['Batch Coding'], '4071.60')        # its own bottles

    def test_drafts_and_other_days_are_left_out(self):
        self._night_of_the_28th()
        self._run(6, [], status_='DRAFT', total='0')
        self._run(7, [('2026-09-27 20:00', '2026-09-28 06:00', '4000')])
        self.assertEqual(self._open('NIGHT')['produced_cases'], '5655.00')

    # -- what cannot be worked out ------------------------------------------------

    def test_a_run_without_bottles_per_case_is_named(self):
        self._run(8, [('2026-09-28 20:00', '2026-09-29 06:00', '100')], pieces=None)
        data = self._open('NIGHT')
        self.assertEqual(self._amounts(data)['Batch Coding'], '0.00')
        self.assertIn('No bottles per case on run #8', data['warnings'][0])

    def test_waste_without_a_price_is_named_not_guessed(self):
        run = self._run(9, [('2026-09-28 20:00', '2026-09-29 06:00', '100')])
        self._waste(run, '5', price=None)
        data = self._open('NIGHT')
        self.assertEqual(self._amounts(data)['Wastage'], '0.00')
        self.assertTrue(any('No SAP price for PREFORM 500 ML' in w for w in data['warnings']))

    def test_no_scrap_rate_leaves_scrap_to_be_typed(self):
        from cost_master.models import CostRate

        CostRate.objects.filter(cost_type=self.types['Scrap Recovering']).update(rate=0)
        self._night_of_the_28th()
        data = self._open('NIGHT')
        self.assertNotIn('Scrap Recovering', self._amounts(data))
        self.assertTrue(any('Scrap Recovering: no rate' in w for w in data['warnings']))

    def test_no_meter_readings_leaves_electricity_to_be_typed(self):
        self.electricity.return_value = (Decimal('0'), {}, {})
        data = self._open('NIGHT')
        self.assertNotIn('Electricity', self._amounts(data))

    def test_the_etp_meter_is_not_filling_electricity(self):
        # Electricity++ charges Beverages for the ETP too; the sheet leaves it out.
        from unittest import mock

        mock.patch.stopall()  # the stand-in Electricity++ above; read the real split
        party = f'company:{self.company.code}'
        breakdown = {
            'by_party': {party: {'cost': Decimal('35379')}},
            'by_meter': {party: {'Production Floor Beverage': {'cost': Decimal('32787')},
                                 'etp ': {'cost': Decimal('2592')}}},
        }
        with mock.patch('maintenance.electricity.service.company_breakdown',
                        return_value=breakdown), \
                mock.patch('production_execution.services.filling_cost._meter_rounds',
                           return_value={}):
            day = self._open()
            night = self._open('NIGHT')
        explain = {e['head']: e['explain'] for e in day['entries']}
        self.assertEqual(self._amounts(day)['Electricity'], '32787.00')
        self.assertIn('less etp', explain['Electricity'].lower())
        self.assertIn('2,592', explain['Electricity'])
        # A shift's share is of the filling meters only.
        self.assertLessEqual(Decimal(self._amounts(night)['Electricity']), Decimal('32787'))

    def test_fixed_manpower_takes_over_an_old_salary_row(self):
        heads = {e['head']: e for e in self._open('NIGHT')['entries']}
        self.assertEqual(heads['Fixed Manpower']['aliases'], ['Salary'])

    # -- the request --------------------------------------------------------------

    def test_a_date_is_required_and_a_shift_must_be_one(self):
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.get(self.url, {'date': self.DAY, 'shift': 'EVENING'})
                         .status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_companys_line_is_refused(self):
        oil = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        line = ProductionLine.objects.create(company=oil, name='Oil Line')
        response = self.client.get(self.url, {'date': self.DAY, 'line_id': line.id})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_beverages_gets_defaults(self):
        oil = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        user = FillingCostSheetTests._user(
            self, 'entry@oil.test', 'E005', ['can_view_filling_cost'], company=oil)
        self.client.force_authenticate(user)
        self.client.credentials(HTTP_COMPANY_CODE=oil.code)
        self.assertEqual(self.client.get(self.url, {'date': self.DAY}).status_code,
                         status.HTTP_403_FORBIDDEN)


class FillingCostShiftSheetTests(FillingCostSheetTests):
    """A day can hold a sheet per shift, and one for the whole day besides."""

    def test_a_day_holds_a_sheet_per_shift(self):
        for shift in ('DAY', 'NIGHT', ''):
            response = self.client.post(self.list_url, {
                'date': '2026-09-28', 'shift': shift, 'cases': '5655',
                'entries': entries_payload()}, format='json')
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.client.post(self.list_url, {
            'date': '2026-09-28', 'shift': 'NIGHT', 'cases': '1',
            'entries': entries_payload()}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Night', response.data['detail'])

        listed = self.client.get(self.list_url, {'date': '2026-09-28', 'shift': 'NIGHT'}).data
        self.assertEqual([s['shift'] for s in listed], ['NIGHT'])

    def test_saving_without_the_shift_keeps_it(self):
        sheet = self.client.post(self.list_url, {
            'date': '2026-09-28', 'shift': 'NIGHT', 'cases': '5655',
            'entries': entries_payload()}, format='json').data
        self.client.patch(self._detail_url(sheet['id']), {'cases': '6000'}, format='json')
        self.assertEqual(FillingCostSheet.objects.get(pk=sheet['id']).shift, 'NIGHT')


class BeverageFillingCostMigrationTests(APITestCase):
    """cost_master 0004 puts the sheet's types and Beverages' rates in place."""

    def _run(self):
        from importlib import import_module

        from django.apps import apps

        import_module('cost_master.migrations.0004_beverage_filling_cost_types') \
            .add_beverage_filling_costs(apps, None)

    def test_the_hand_entered_salary_is_renamed_not_duplicated(self):
        from datetime import date

        from cost_master.models import CostRate, CostType

        company = Company.objects.create(name='Jivo Beverages', code='JIVO_BEVERAGES')
        hand = CostType.objects.create(code='1', name='beverage salary',
                                       default_basis='PER_MONTH')
        CostRate.objects.create(cost_type=hand, scope='FACTORY', basis='PER_MONTH',
                                rate=Decimal('1200000'), effective_from=date(2026, 9, 29))
        self._run()
        self._run()  # and again, as a re-run would

        hand.refresh_from_db()
        self.assertEqual(hand.code, 'beverage-salary')
        self.assertEqual(hand.name, 'Beverage — Fixed Manpower')
        self.assertFalse(CostType.objects.filter(code='1').exists())
        self.assertEqual(hand.rates.count(), 2)  # the hand-entered one, kept, and Beverages'
        rates = {r.cost_type.code: r.rate for r in CostRate.objects.filter(
            scope='COMPANY', company=company)}
        self.assertEqual(rates, {
            'beverage-salary': Decimal('1200000'),
            'beverage-maintenance': Decimal('250000'),
            'beverage-batch-coding': Decimal('0.03'),
            'beverage-lubrication': Decimal('0.0133'),
            'beverage-lab': Decimal('5000'),
            'beverage-miscellaneous': Decimal('10000'),
        })
