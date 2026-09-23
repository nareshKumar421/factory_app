"""Tests for the filling cost sheet — the month's filling cost, typed in.

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
            'period': '2026-09-01',
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

    def test_any_day_of_the_month_is_stored_as_the_month(self):
        response = self._post(period='2026-09-17')
        self.assertEqual(response.data['period'], '2026-09-01')

    def test_a_sheet_can_be_entered_for_one_line(self):
        response = self._post(line_id=self.line.id)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['line_name'], 'Line 1')
        # The floor-wide sheet for the same month is a different sheet.
        self.assertEqual(self._post().status_code, status.HTTP_201_CREATED)

    def test_second_sheet_for_the_same_month_is_refused(self):
        self._post()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('September 2026', response.data['detail'])

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

    def test_moving_a_sheet_onto_a_month_already_entered_is_refused(self):
        self._post(period='2026-08-01')
        sheet_id = self._post(period='2026-09-01').data['id']
        response = self.client.patch(
            self._detail_url(sheet_id), {'period': '2026-08-01'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FillingCostSheet.objects.get(id=sheet_id).period.month, 9)

    def test_a_sheet_can_be_deleted_with_its_rows(self):
        sheet_id = self._post().data['id']
        response = self.client.delete(self._detail_url(sheet_id))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(FillingCostSheet.objects.exists())

    # -- listing ------------------------------------------------------------

    def test_list_is_newest_month_first_and_filters_by_month(self):
        self._post(period='2026-08-01')
        self._post(period='2026-09-01')
        response = self.client.get(self.list_url)
        self.assertEqual([s['period'] for s in response.data],
                         ['2026-09-01', '2026-08-01'])

        # Any day in the month finds that month's sheet.
        response = self.client.get(self.list_url, {'period': '2026-08-20'})
        self.assertEqual([s['period'] for s in response.data], ['2026-08-01'])

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
            company=other, period='2026-09-01', cases=Decimal(CASES))
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
