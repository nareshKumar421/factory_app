"""Every board's electricity is Electricity++'s, each unit once.

Run with:
    .venv/bin/python manage.py test maintenance.tests_electricity_boards \
        --settings=config.sqlite_test_settings

The fixture is Electricity++'s own tree (``TreeFixture``): KWH (the supply)
-> Ground Floor (Beverages) -> Lab (half each), and First Floor (Oil). Its
day: 1,000 units on KWH, 400 on Ground Floor of which 40 on Lab, 500 on First
Floor. Electricity++ gives Oil 520 (First Floor's 500 and half the Lab),
Beverages 380 (Ground Floor's own 360 and the other half), and the 100 KWH
measured that no sub-meter did to nobody. At 9 a unit.

Each board is asked for the same day and has to give exactly that: Admin
Control the signed-in company's share, the expense wall the campus, the
matrix each company's column. KWH, the main, is on no board: its 100 units
nobody's meter saw are the supply's, not a meter anybody draws on, so the
campus is the 900 its sub-meters carry.
"""
from decimal import Decimal
from types import SimpleNamespace

from company.models import Company
from maintenance.electricity import boards, service
from maintenance.models import ElectricityMeter
from maintenance.tests_electricity_tree import D1, TreeFixture


class ElectricityBoardsTests(TreeFixture):
    def setUp(self):
        super().setUp()
        ElectricityMeter.objects.update(rate_per_unit=Decimal("9"))
        self.read_day(D1)

    def money(self, units):
        return Decimal(units) * 9

    # -- the shared helper -----------------------------------------------------

    def test_a_companys_figure_is_its_electricity_plus_allocation(self):
        result = boards.breakdown(D1, D1)
        oil = boards.company(result, 'JIVO_OIL')
        self.assertEqual((oil['units'], oil['cost']), (Decimal('520'), self.money(520)))
        self.assertEqual({name: part['units'] for name, part in oil['by_meter'].items()},
                         {'First Floor': Decimal('500'), 'Lab': Decimal('20')})
        self.assertEqual(oil['by_day'][D1]['units'], Decimal('520'))
        totals = service.party_totals(D1, D1)
        self.assertEqual(oil['units'], totals['company:JIVO_OIL']['units'])

    def test_everybody_together_is_the_meters_each_unit_once_mains_left_out(self):
        everyone = boards.for_parties(boards.breakdown(D1, D1), None)
        self.assertEqual(everyone['units'], Decimal('900'))
        self.assertNotIn('KWH', everyone['by_meter'])

    def test_the_tree_is_reading_less_sub_meters_is_own(self):
        rows = {r['name']: r for r in boards.meter_tree(D1, D1)}
        self.assertNotIn('KWH', rows)                       # the main: on no board
        ground = rows['Ground Floor']
        self.assertEqual((ground['units'], ground['sub_metered_units'], ground['own_units']),
                         ('400.00', '40.00', '360.00'))
        # Under the main, so shown at the top; the Lab below it.
        self.assertEqual((ground['depth'], rows['Lab']['depth']), (0, 1))
        self.assertEqual(rows['Lab']['parent_id'], ground['id'])

    def test_the_tree_shows_a_companys_part_of_each_meter(self):
        rows = {r['name']: r for r in boards.meter_tree(D1, D1, 'JIVO_OIL')}
        self.assertEqual((rows['Lab']['company_units'], rows['Lab']['company_share_pct']),
                         ('20.00', '50.0'))
        self.assertIsNone(rows['Ground Floor']['company_units'])   # Beverages' alone

    def test_a_meter_shared_says_what_share_it_is(self):
        result = boards.breakdown(D1, D1)
        oil = boards.company(result, 'JIVO_OIL')
        self.assertEqual(boards.meter_share(result, oil['by_meter']['Lab']['units'], 'Lab'),
                         Decimal('0.5'))
        self.assertIsNone(boards.meter_share(result, oil['by_meter']['First Floor']['units'],
                                             'First Floor'))

    # -- Admin Control -----------------------------------------------------------

    def admin_board(self, code):
        from admin_board.services import AdminBoardService

        return AdminBoardService(code, today=D1)._electricity()

    def test_admin_control_shows_the_signed_in_companys_electricity(self):
        oil, bev = self.admin_board('JIVO_OIL'), self.admin_board('JIVO_BEVERAGES')
        self.assertEqual((oil['cost'], bev['cost']), (4680.0, 3420.0))     # 520, 380 at 9
        self.assertEqual(oil['today_units'], 520.0)

    def test_admin_controls_rows_are_its_meters_and_add_up(self):
        oil = self.admin_board('JIVO_OIL')
        rows = {row['label']: row for row in oil['rows']}
        self.assertEqual(set(rows), {'First Floor', 'Lab'})
        self.assertEqual(sum(row['amount'] for row in oil['rows']), oil['cost'])
        self.assertIn("Jivo Oil's 50% of the meter", rows['Lab']['detail'])
        self.assertNotIn('%', rows['First Floor']['detail'])

    def test_admin_control_says_so_when_electricity_plus_has_nothing(self):
        Company.objects.create(name='Jivo Mart', code='JIVO_MART')
        mart = self.admin_board('JIVO_MART')
        self.assertEqual(mart['cost'], 0.0)
        self.assertIn('Electricity++ has no Jivo Mart units', mart['warning'])

    # -- the factory expense wall --------------------------------------------------

    def wall(self, only_companies):
        from factory_expense.services import electricity_costs

        companies = list(Company.objects.filter(code__in=['JIVO_OIL', 'JIVO_BEVERAGES']))
        return electricity_costs(companies, [D1],
                                 SimpleNamespace(electricity_only_company_meters=only_companies),
                                 focus=[D1])

    def test_the_wall_is_the_meters_each_unit_once_and_no_main(self):
        per_date, meters, _ = self.wall(False)
        # 900, not 1,000 + 400 + 40 + 500 as the register sums it.
        self.assertEqual(per_date[D1]['units'], Decimal('900'))
        self.assertEqual(sum(m['units'] for m in meters.values()), Decimal('900'))
        self.assertNotIn('KWH', meters)

    def test_the_wall_can_keep_to_its_companies(self):
        per_date, meters, _ = self.wall(True)
        self.assertEqual(per_date[D1]['units'], Decimal('900'))          # 520 + 380
        self.assertEqual(meters['Lab']['units'], Decimal('40'))          # both halves
        self.assertEqual(meters['Lab']['rate'], Decimal('9'))

    # -- the company expense matrix --------------------------------------------------

    def matrix(self):
        from factory_expense import matrix

        companies = list(Company.objects.filter(code__in=['JIVO_OIL', 'JIVO_BEVERAGES']))
        return matrix, companies, matrix.electricity_by_company(companies, [D1], None)

    def test_each_companys_column_is_its_allocation(self):
        _, companies, (per_company, shared, incomer, allocated, _notes) = self.matrix()
        by_code = {c.code: per_company[c.id] for c in companies}
        self.assertEqual(by_code['JIVO_OIL']['cost'], self.money(520))
        self.assertEqual(by_code['JIVO_BEVERAGES']['cost'], self.money(380))
        # KWH's own 100 is the supply's, on no board.
        self.assertEqual(shared['cost'], 0)
        self.assertEqual(allocated, self.money(900))
        self.assertEqual(by_code['JIVO_OIL']['meters'], {'First Floor', 'Lab'})

    def test_the_matrix_reconciles_to_the_supply_meter(self):
        matrix, _, (_, _, incomer, allocated, _) = self.matrix()
        self.assertEqual(incomer['kwh']['units'], Decimal('1000'))
        check = matrix.reconcile_against_incomer(allocated, incomer)
        # The meters carry 900 of the 1,000 KWH brought in: 10% nobody metered.
        self.assertEqual(check['drift_pct'], -10.0)

    def test_the_wall_and_the_matrix_agree(self):
        _, _, (_, _, _, allocated, _) = self.matrix()
        per_date, _, _ = self.wall(False)
        self.assertEqual(per_date[D1]['cost'], allocated)

    # -- what Electricity++ cannot place ---------------------------------------------

    def test_a_meter_outside_the_tree_is_on_no_company_and_named(self):
        from maintenance.models import DailyElectricityReading

        loose = ElectricityMeter.objects.create(name='Canteen', rate_per_unit=Decimal('9'))
        DailyElectricityReading.objects.create(
            meter=loose, date=D1, opening_reading=Decimal('0'), closing_reading=Decimal('70'),
            rate_per_unit=Decimal('9'))
        result = boards.breakdown(D1, D1)
        # Counted in the campus (a supply of its own), charged to no company.
        self.assertEqual(boards.for_parties(result, None)['units'], Decimal('970'))
        self.assertEqual(boards.company(result, 'JIVO_OIL')['units'], Decimal('520'))
        self.assertTrue(any('charged to no company yet: Canteen' in w
                            for w in boards.warnings(result)))


class ElectricityBoardAPITests(TreeFixture):
    """The Electricity dashboard's endpoint: the same figures, for the page."""

    URL = '/api/v1/maintenance/electricity-board/'

    def setUp(self):
        super().setUp()
        ElectricityMeter.objects.update(rate_per_unit=Decimal("9"))
        self.read_day(D1)

    def board(self, **params):
        response = self.client.get(self.URL, {'date_from': str(D1), 'date_to': str(D1), **params})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_a_companys_board_is_its_electricity_plus_share(self):
        data = self.board(company='JIVO_OIL')
        self.assertEqual((data['units'], data['cost']), ('520.00', '4680.00'))
        meters = {m['name']: m for m in data['meters']}
        self.assertEqual(meters['Lab']['share_pct'], '50.00')
        self.assertIsNone(meters['First Floor']['share_pct'])
        self.assertEqual(meters['First Floor']['rate'], '9.00')
        self.assertEqual(data['days'][0]['by_meter'], {'First Floor': '500.00', 'Lab': '20.00'})
        self.assertEqual(data['days_with_units'], 1)

    def test_the_campus_board_is_the_supply_each_unit_once(self):
        data = self.board()
        self.assertEqual(data['units'], '900.00')
        self.assertEqual(data['supply']['units'], '1000.00')
        self.assertTrue(all(m['share_pct'] is None for m in data['meters']))
        self.assertEqual([r['name'] for r in data['tree']],
                         ['First Floor', 'Ground Floor', 'Lab'])

    def test_the_board_needs_the_daily_electricity_right(self):
        from maintenance.tests_meter_scope import client_for, make_user

        self.client = client_for(make_user('nobody@example.com'))
        self.assertEqual(self.client.get(self.URL).status_code, 403)
