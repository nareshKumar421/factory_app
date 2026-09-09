"""Tests for the next-day plan readiness check.

Run with:
    python manage.py test production_execution.tests_plan_check \
        --settings=config.sqlite_test_settings

The SAP readers are faked, so what is under test is the arithmetic and the
judgement — whether a shortfall is called a shortfall, whether the material
another plan has already been given is double-counted, whether a back-to-back
changeover is mistaken for a clash.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from company.models import Company
from production_execution.models import (
    ProductionLine,
    ProductionMaterialUsage,
    ProductionRun,
    RunStatus,
)
from warehouse.models_rm_stock import RawMaterialStock
from production_execution.services.plan_check_service import (
    CONFLICT_DUPLICATE_SKU,
    CONFLICT_LINE_BUSY,
    CONFLICT_MATERIAL_CONTENTION,
    STATUS_CONTESTED,
    STATUS_OK,
    STATUS_SHORT,
    STATUS_TIGHT,
    STATUS_UNKNOWN,
    ProductionPlanCheckService,
    compute_planned_window,
)

BOM_LINE_ITEM = 4
BOM_LINE_RESOURCE = 290


def bom_row(
    code, name, per_case, group='PACKAGING MATERIAL',
    line_type=BOM_LINE_ITEM, uom='PCS', base_qty=20,
):
    """One `ITT1` line as the reader returns it.

    `per_case` is `ITT1."Quantity"` exactly as authored in SAP — the quantity for
    ONE box, which is how this data is written (`FG0000118 CANOLA OIL 5 LTR 4
    PCS`: 20 litres of oil, 4 bottles, 4 caps, 1 carton).

    `base_qty` (`OITT."Qauntity"`) defaults to 20 and `QtyPerUnit` is derived
    from it faithfully, so it is deliberately *not* equal to `per_case`. Any
    code that goes back to dividing by the base quantity — a per-piece rate
    multiplied by a case count — then fails these tests loudly instead of
    quietly understating every requirement twentyfold.
    """
    qty = Decimal(str(per_case)) if per_case is not None else None
    return {
        'ParentCode': 'FG001',
        'BomBaseQty': Decimal(str(base_qty)),
        'ChildNum': 1,
        'ComponentCode': code,
        'ComponentName': name,
        'LineType': line_type,
        'BomQty': qty,
        'QtyPerUnit': (qty / Decimal(str(base_qty))) if qty is not None and base_qty else None,
        'IssueWarehouse': 'BH-PM',
        'Uom': uom,
        'ItemGroup': group,
        'PurchaseItem': 'Y',
        'LastPurchasePrice': 1,
        'HasOwnBom': 0,
    }


def stock_row(code, warehouse, on_hand, committed=0, group='PACKAGING MATERIAL'):
    return {
        'ItemCode': code,
        'WhsCode': warehouse,
        'OnHand': Decimal(str(on_hand)),
        'MinStock': 0,
        'Committed': Decimal(str(committed)),
        'OnOrder': 0,
        'Uom': 'PCS',
        'LastPurchasePrice': 1,
        'ItemGroup': group,
        'LastConsumptionDate': None,
        'DaysSinceLastConsumption': None,
    }


class FakePlanReader:
    def __init__(self, bom_rows=(), stock_rows=(), po_rows=()):
        self._bom = list(bom_rows)
        self._stock = list(stock_rows)
        self._po = list(po_rows)

    def get_bom_components(self, item_codes):
        return [r for r in self._bom if r['ParentCode'] in set(item_codes)]

    def get_item_stock(self, item_codes, warehouses=None):
        wanted = set(item_codes)
        rows = [r for r in self._stock if r['ItemCode'] in wanted]
        if warehouses:
            rows = [r for r in rows if r['WhsCode'] in set(warehouses)]
        return rows

    def get_open_purchase_qty(self, item_codes):
        wanted = set(item_codes)
        return [r for r in self._po if r['ItemCode'] in wanted]


class FakeItemReader:
    def __init__(self, pieces_per_case=None):
        self._map = pieces_per_case or {}

    def get_pieces_per_case_map(self, item_codes):
        return {c: self._map[c] for c in item_codes if c in self._map}


class PlanCheckBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code='TEST_CO', name='Test Company')
        self.line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.other_line = ProductionLine.objects.create(company=self.company, name='Line-2')
        self.day = date(2026, 9, 8)

    def service(self, bom_rows=(), stock_rows=(), po_rows=(), pieces_per_case=None):
        return ProductionPlanCheckService(
            'TEST_CO',
            plan_reader=FakePlanReader(bom_rows, stock_rows, po_rows),
            item_reader=FakeItemReader(pieces_per_case),
        )

    def at(self, hour, minute=0):
        return timezone.make_aware(datetime(2026, 9, 8, hour, minute))

    def make_run(self, *, line=None, item_code='FG002', qty=100, status=RunStatus.DRAFT,
                 start=None, end=None, run_number=None, day=None, materials=None):
        run = ProductionRun.objects.create(
            company=self.company,
            run_number=run_number or (ProductionRun.objects.count() + 1),
            date=day or self.day,
            line=line or self.line,
            product=f'Product {item_code}',
            item_code=item_code,
            required_qty=Decimal(str(qty)),
            planned_start_at=start,
            planned_end_at=end,
            status=status,
        )
        for code, needed in (materials or {}).items():
            ProductionMaterialUsage.objects.create(
                production_run=run, material_code=code, material_name=code,
                opening_qty=Decimal(str(needed)),
            )
        return run


class MaterialReadinessTests(PlanCheckBase):

    def test_enough_stock_reads_as_ok(self):
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 5000)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['required_qty'], 2000)
        self.assertEqual(row['on_hand'], 5000)
        self.assertEqual(row['status'], STATUS_OK)
        self.assertEqual(row['shortfall'], 0)
        self.assertFalse(result['blocking']['requires_remark'])

    def test_shortfall_is_the_gap_against_physical_stock(self):
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 1500)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['status'], STATUS_SHORT)
        self.assertEqual(row['shortfall'], 500)
        self.assertEqual(result['materials']['summary']['short_lines'], 1)
        self.assertTrue(result['blocking']['requires_remark'])

    def test_committed_stock_is_shown_but_does_not_create_a_shortfall(self):
        """On this data most components are over-committed by open SAP orders.

        Judging on `free` would read almost every plan as blocked, which is
        untrue of a factory that ships every day — so committed travels as
        information and the shortfall stays physical.
        """
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 5000, committed=4500)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['status'], STATUS_TIGHT)
        self.assertEqual(row['shortfall'], 0)
        self.assertEqual(row['free'], 500)
        self.assertEqual(result['materials']['summary']['short_lines'], 0)
        self.assertFalse(result['blocking']['requires_remark'])

    def test_wastage_warehouse_is_never_usable_stock(self):
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[
                stock_row('PM001', 'BH-PM', 1000),
                stock_row('PM001', 'BH-WST', 9000),
            ],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['on_hand'], 1000)
        self.assertEqual(row['status'], STATUS_SHORT)

    def test_packaging_does_not_pick_up_stock_from_the_oil_stores(self):
        """Per-material-type scope, the same one Planning & Purchase applies."""
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20, group='PACKAGING MATERIAL')],
            stock_rows=[
                stock_row('PM001', 'BH-PM', 500),
                stock_row('PM001', 'BH-LO', 9000),
            ],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        self.assertEqual(result['materials']['rows'][0]['on_hand'], 500)

    def test_a_component_with_no_stock_row_is_called_out(self):
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['status'], 'NO_STOCK_RECORD')
        self.assertEqual(row['shortfall'], 2000)
        self.assertEqual(result['materials']['summary']['short_lines'], 1)

    def test_open_purchase_orders_are_reported_against_a_shortfall(self):
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 1000)],
            po_rows=[{
                'ItemCode': 'PM001', 'OpenQty': Decimal('50000'),
                'EarliestDue': date(2026, 9, 9), 'LatestDue': date(2026, 9, 20),
                'OpenLines': 2,
            }],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['on_order_qty'], 50000)
        self.assertEqual(row['on_order_earliest_due'], '2026-09-09')

    def test_resource_lines_are_separated_not_dropped(self):
        result = self.service(
            bom_rows=[
                bom_row('PM001', 'Caps', 20),
                bom_row('CONV', 'Conversion cost', 1, line_type=BOM_LINE_RESOURCE),
            ],
            stock_rows=[stock_row('PM001', 'BH-PM', 5000)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        self.assertEqual(len(result['materials']['rows']), 1)
        self.assertEqual(
            [r['item_code'] for r in result['materials']['resource_lines']], ['CONV']
        )

    def test_a_bom_line_with_no_quantity_is_reported_as_unusable(self):
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', None)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        self.assertEqual(result['materials']['rows'], [])
        self.assertEqual(result['materials']['unusable'][0]['item_code'], 'PM001')

    def test_short_lines_sort_to_the_top(self):
        result = self.service(
            bom_rows=[
                bom_row('PM001', 'Caps', 20),
                bom_row('PM002', 'Labels', 20),
            ],
            stock_rows=[
                stock_row('PM001', 'BH-PM', 99999),
                stock_row('PM002', 'BH-PM', 10),
            ],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        self.assertEqual(
            [r['item_code'] for r in result['materials']['rows']], ['PM002', 'PM001']
        )

    def test_a_failed_stock_read_does_not_invent_a_shortage(self):
        class Broken(FakePlanReader):
            def get_item_stock(self, item_codes, warehouses=None):
                raise RuntimeError('HANA unreachable')

        service = ProductionPlanCheckService(
            'TEST_CO',
            plan_reader=Broken([bom_row('PM001', 'Caps', 20)]),
            item_reader=FakeItemReader(),
        )
        result = service.check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day
        )
        self.assertFalse(result['materials']['available'])
        self.assertIn('HANA unreachable', result['materials']['error'])
        self.assertFalse(result['blocking']['requires_remark'])
        row = result['materials']['rows'][0]
        self.assertEqual(row['status'], STATUS_UNKNOWN)
        self.assertIsNone(row['on_hand'])
        self.assertIsNone(row['shortfall'])
        self.assertEqual(result['materials']['summary']['short_lines'], 0)


class MaterialContentionTests(PlanCheckBase):

    def test_another_plan_claiming_the_same_component_is_contention(self):
        self.make_run(line=self.other_line, item_code='FG002', materials={'PM001': 4000})

        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 5000)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['status'], STATUS_CONTESTED)
        self.assertEqual(row['other_plan_demand'], 4000)
        self.assertEqual(row['balance_after_this_plan'], -1000)
        self.assertEqual(row['competing_runs'][0]['product'], 'Product FG002')

        contention = [c for c in result['conflicts'] if c['type'] == CONFLICT_MATERIAL_CONTENTION]
        self.assertEqual(len(contention), 1)
        self.assertTrue(result['blocking']['requires_remark'])

    def test_material_already_issued_to_the_other_run_is_not_counted_twice(self):
        """Issued material has left the store, so `OnHand` already excludes it.

        Counting the issued part as a live claim would report a shortage on a
        plan that is perfectly fine — the fastest way to make the warning
        worthless.
        """
        from warehouse.models import BOMRequest, BOMRequestLine, BOMRequestStatus

        other = self.make_run(line=self.other_line, materials={'PM001': 4000})
        request = BOMRequest.objects.create(
            company=self.company, production_run=other,
            required_qty=Decimal('100'), status=BOMRequestStatus.APPROVED,
        )
        BOMRequestLine.objects.create(
            bom_request=request, item_code='PM001', item_name='Caps',
            per_unit_qty=Decimal('20'), required_qty=Decimal('4000'),
            approved_qty=Decimal('4000'), issued_qty=Decimal('4000'), base_line=0,
        )

        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 5000)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['other_plan_demand'], 0)
        self.assertEqual(row['status'], STATUS_OK)

    def test_a_completed_run_no_longer_claims_material(self):
        self.make_run(
            line=self.other_line, materials={'PM001': 4000}, status=RunStatus.COMPLETED
        )
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 5000)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        self.assertEqual(result['materials']['rows'][0]['other_plan_demand'], 0)

    def test_a_run_still_in_progress_from_another_day_still_claims_material(self):
        self.make_run(
            line=self.other_line, materials={'PM001': 4000},
            status=RunStatus.IN_PROGRESS, day=self.day - timedelta(days=1),
        )
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 5000)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        self.assertEqual(result['materials']['rows'][0]['other_plan_demand'], 4000)

    def test_the_run_being_edited_does_not_compete_with_itself(self):
        mine = self.make_run(line=self.line, item_code='FG001', materials={'PM001': 2000})

        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 2500)],
        ).check(
            line_id=self.line.id, item_code='FG001', required_qty=100,
            date=self.day, exclude_run_id=mine.id,
        )

        row = result['materials']['rows'][0]
        self.assertEqual(row['other_plan_demand'], 0)
        self.assertEqual(row['status'], STATUS_OK)
        self.assertEqual(result['conflicts'], [])


class ConflictTests(PlanCheckBase):

    def test_overlapping_window_on_the_same_line_is_a_clash(self):
        self.make_run(line=self.line, start=self.at(6), end=self.at(14))

        result = self.service().check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
            planned_start_at=self.at(10), planned_end_at=self.at(18),
            planned_end_is_manual=True,
        )
        clashes = [c for c in result['conflicts'] if c['type'] == CONFLICT_LINE_BUSY]
        self.assertEqual(len(clashes), 1)
        self.assertTrue(clashes[0]['window_overlap'])

    def test_back_to_back_runs_on_one_line_are_not_a_clash(self):
        self.make_run(line=self.line, start=self.at(6), end=self.at(14))

        result = self.service().check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
            planned_start_at=self.at(14), planned_end_at=self.at(22),
            planned_end_is_manual=True,
        )
        self.assertEqual(
            [c for c in result['conflicts'] if c['type'] == CONFLICT_LINE_BUSY], []
        )

    def test_a_same_line_plan_with_no_times_is_reported_as_uncheckable(self):
        self.make_run(line=self.line)

        result = self.service().check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
            planned_start_at=self.at(10), planned_end_at=self.at(18),
            planned_end_is_manual=True,
        )
        clash = [c for c in result['conflicts'] if c['type'] == CONFLICT_LINE_BUSY][0]
        self.assertFalse(clash['window_overlap'])
        self.assertTrue(clash['window_unknown'])

    def test_a_plan_on_a_different_line_is_not_a_line_clash(self):
        self.make_run(line=self.other_line, start=self.at(6), end=self.at(14))

        result = self.service().check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
            planned_start_at=self.at(10), planned_end_at=self.at(18),
            planned_end_is_manual=True,
        )
        self.assertEqual(
            [c for c in result['conflicts'] if c['type'] == CONFLICT_LINE_BUSY], []
        )

    def test_the_same_sku_planned_twice_on_one_date_is_flagged(self):
        self.make_run(line=self.other_line, item_code='FG001', qty=250)

        result = self.service().check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
        )
        dupes = [c for c in result['conflicts'] if c['type'] == CONFLICT_DUPLICATE_SKU]
        self.assertEqual(len(dupes), 1)
        self.assertIn('250', dupes[0]['message'])

    def test_the_same_sku_on_another_date_is_not_flagged(self):
        self.make_run(
            line=self.other_line, item_code='FG001', day=self.day + timedelta(days=1)
        )
        result = self.service().check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
        )
        self.assertEqual(
            [c for c in result['conflicts'] if c['type'] == CONFLICT_DUPLICATE_SKU], []
        )


class PlannedWindowTests(PlanCheckBase):

    def test_the_finish_time_comes_from_bottles_not_cases(self):
        """3,000 bottles/hr on 100 cases of 20 = 2,000 bottles = 40 minutes."""
        window = compute_planned_window(self.at(6), 100, 20, 3000)
        self.assertEqual(window['duration_minutes'], 40)
        self.assertEqual(window['bottles'], 2000)
        self.assertEqual(window['planned_end_at'], self.at(6, 40))
        self.assertTrue(window['derived'])

    def test_a_part_minute_rounds_up_rather_than_promising_too_much(self):
        window = compute_planned_window(self.at(6), 1, 20, 3000)
        self.assertEqual(window['duration_minutes'], 1)

    def test_a_missing_input_leaves_the_finish_time_unknown(self):
        window = compute_planned_window(self.at(6), 100, None, 3000)
        self.assertIsNone(window['planned_end_at'])
        self.assertFalse(window['derived'])
        self.assertIn('bottles per case', window['undecidable_because'])

    def test_bottles_per_case_falls_back_to_sap_when_the_form_omits_it(self):
        result = self.service(pieces_per_case={'FG001': 20}).check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
            planned_start_at=self.at(6), rated_speed=3000,
        )
        self.assertEqual(result['timing']['pieces_per_case'], 20)
        self.assertEqual(result['timing']['duration_minutes'], 40)

    def test_a_typed_finish_time_wins_but_the_derived_one_is_still_shown(self):
        result = self.service().check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
            planned_start_at=self.at(6), planned_end_at=self.at(16),
            planned_end_is_manual=True, rated_speed=3000, pieces_per_case=20,
        )
        timing = result['timing']
        self.assertEqual(timing['planned_end_at'], self.at(16))
        self.assertEqual(timing['duration_minutes'], 600)
        self.assertEqual(timing['derived_end_at'], self.at(6, 40))
        self.assertEqual(timing['derived_duration_minutes'], 40)


class PlanCheckEndpointTests(TestCase):
    """The endpoint itself — URL, permissions and serializer wiring.

    SAP is unreachable in tests, so this asserts the shape that survives an
    outage: a 200 with the database-side clash findings intact and the material
    side honestly marked unavailable.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        from rest_framework.test import APIClient

        from company.models import UserCompany, UserRole

        User = get_user_model()
        self.company = Company.objects.create(code='TEST_CO', name='Test Company')
        self.line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.user = User.objects.create_user(email='planner@test.com', password='pw123456')
        role = UserRole.objects.create(name='Admin')
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_active=True
        )
        self.user.user_permissions.set(
            Permission.objects.filter(content_type__app_label='production_execution')
        )
        self.user = User.objects.get(pk=self.user.pk)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE='TEST_CO')

    def test_plan_check_returns_conflicts_even_when_sap_is_down(self):
        ProductionRun.objects.create(
            company=self.company, run_number=1, date=date(2026, 9, 8),
            line=self.line, item_code='FG001', product='FG One',
            required_qty=Decimal('250'), status=RunStatus.DRAFT,
        )

        resp = self.client.post(
            '/api/v1/production-execution/runs/plan-check/',
            {
                'line_id': self.line.id,
                'item_code': 'FG001',
                'required_qty': '100',
                'date': '2026-09-08',
                'planned_start_at': '2026-09-08T06:00:00',
                'rated_speed': '3000',
                'pieces_per_case': 20,
            },
            format='json',
        )

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['materials']['available'])
        self.assertEqual(resp.data['timing']['duration_minutes'], 40)
        types = {c['type'] for c in resp.data['conflicts']}
        self.assertIn(CONFLICT_DUPLICATE_SKU, types)
        self.assertIn(CONFLICT_LINE_BUSY, types)

    def test_a_bad_finish_time_is_rejected_by_the_create_serializer(self):
        resp = self.client.post(
            '/api/v1/production-execution/runs/',
            {
                'line_id': self.line.id,
                'date': '2026-09-08',
                'product': 'FG One',
                'item_code': 'FG001',
                'planned_start_at': '2026-09-08T14:00:00',
                'planned_end_at': '2026-09-08T06:00:00',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('planned_end_at', resp.data['errors'])

    def test_a_planned_run_keeps_its_window_and_derives_the_finish(self):
        resp = self.client.post(
            '/api/v1/production-execution/runs/',
            {
                'line_id': self.line.id,
                'date': '2026-09-08',
                'product': 'FG One',
                'item_code': 'FG001',
                'required_qty': '100',
                'rated_speed': '3000',
                'pieces_per_case': 20,
                'planned_start_at': '2026-09-08T06:00:00',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        run = ProductionRun.objects.get(pk=resp.data['id'])
        self.assertIsNotNone(run.planned_start_at)
        # 100 cases x 20 bottles / 3,000 per hour = 40 minutes.
        self.assertEqual(
            int((run.planned_end_at - run.planned_start_at).total_seconds() // 60), 40
        )
        self.assertFalse(run.planned_end_is_manual)


class UncomparableWindowTests(PlanCheckBase):
    """A line clash must not go silent just because a time is missing.

    Silence reads as "the line is free", which is the one wrong answer a
    planning screen must never give.
    """

    def test_a_booked_line_is_still_flagged_when_this_plan_has_no_start_time(self):
        self.make_run(line=self.line, start=self.at(6), end=self.at(14))

        result = self.service().check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
        )
        clash = [c for c in result['conflicts'] if c['type'] == CONFLICT_LINE_BUSY]
        self.assertEqual(len(clash), 1)
        self.assertFalse(clash[0]['window_overlap'])
        self.assertTrue(clash[0]['window_unknown'])


class PerBoxRequirementTests(PlanCheckBase):
    """The requirement is the BOM quantity per BOX times the number of boxes.

    This is the arithmetic the screen exists for, and it was wrong once: the
    check divided `ITT1."Quantity"` by the BOM's base quantity — a per-*piece*
    rate — and then multiplied by a *case* count, reporting 200 litres of oil
    where 4,000 were needed. The base quantity is not even consistent master
    data (4 on `CANOLA OIL 5 LTR 4 PCS`, 20 on `1 LTR 20 PCS`, 1 on `REFINED OIL
    1000 MLS`), while the child quantities are per box throughout.
    """

    def test_the_requirement_is_the_bom_quantity_times_the_box_count(self):
        # FG0000121 CANOLA OIL 1 LTR 20 PCS: 20 LTR of oil per box.
        result = self.service(
            bom_rows=[bom_row('RM0000002', 'Canola loose oil', 20, base_qty=20, uom='LTR')],
            stock_rows=[stock_row('RM0000002', 'BH-LO', 5000, group='RAW MATERIAL')],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=200, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['qty_per_case'], 20)
        self.assertEqual(row['required_qty'], 4000)
        self.assertEqual(row['bom_required_qty'], 4000)

    def test_the_bom_base_quantity_is_never_divided_by(self):
        """Same per-box quantity, three different base quantities, one answer.

        Base 4 / 20 / 1 are all real values on this company's BOMs.
        """
        for base in (4, 20, 1):
            with self.subTest(base_qty=base):
                result = self.service(
                    bom_rows=[bom_row('PM001', 'Caps', 20, base_qty=base)],
                    stock_rows=[stock_row('PM001', 'BH-PM', 99999)],
                ).check(
                    line_id=self.line.id, item_code='FG001',
                    required_qty=200, date=self.day,
                )
                self.assertEqual(result['materials']['rows'][0]['required_qty'], 4000)
                self.assertEqual(result['materials']['rows'][0]['bom_base_qty'], base)

    def test_one_carton_per_box_stays_one_per_box(self):
        """The line that gave the old bug away: 1 carton, base 20.

        Per piece that is 0.05, and 0.05 x 200 cases = 10 cartons for 4,000
        bottles. The right answer is 200.
        """
        result = self.service(
            bom_rows=[bom_row('PM0000006', 'Carton 1 LTR 20 PCS', 1, base_qty=20)],
            stock_rows=[stock_row('PM0000006', 'BH-PM', 150)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=200, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['required_qty'], 200)
        self.assertEqual(row['shortfall'], 50)
        self.assertEqual(row['status'], STATUS_SHORT)

    def test_a_fractional_per_box_quantity_survives(self):
        """Tape is 1.26 MTR per box on the 5 LTR carton — not a whole number."""
        result = self.service(
            bom_rows=[bom_row('PM0000075', 'Tape logo printed', '1.26', base_qty=4, uom='MTR')],
            stock_rows=[stock_row('PM0000075', 'BH-PM', 1000)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=200, date=self.day)

        self.assertEqual(result['materials']['rows'][0]['required_qty'], 252)


class WarehouseProvenanceTests(PlanCheckBase):
    """The screen has to be able to say where every figure came from.

    "1,143 litres in stock" is not actionable on its own — the supervisor needs
    to know it is 1,143 in the tank farm and nothing in the packaging store, and
    that the oil was never looked for in a carton store to begin with.
    """

    def test_each_row_names_the_warehouses_it_was_searched_in(self):
        result = self.service(
            bom_rows=[
                bom_row('RM0000002', 'Canola oil', 20, group='RAW MATERIAL', uom='LTR'),
                bom_row('PM001', 'Caps', 20, group='PACKAGING MATERIAL'),
            ],
            stock_rows=[
                stock_row('RM0000002', 'BH-LO', 5000, group='RAW MATERIAL'),
                stock_row('PM001', 'BH-PM', 5000),
            ],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        by_code = {r['item_code']: r for r in result['materials']['rows']}
        self.assertEqual(by_code['RM0000002']['searched_warehouses'], ['BH-LO', 'BH-OT'])
        self.assertEqual(
            by_code['PM001']['searched_warehouses'], ['BH-PS', 'BH-PC', 'BH-PM']
        )

    def test_the_payload_carries_the_scope_per_material_type(self):
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[stock_row('PM001', 'BH-PM', 5000)],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        scope = result['materials']['warehouse_scope']
        self.assertEqual(scope['RAW'], ['BH-LO', 'BH-OT'])
        self.assertEqual(scope['PACKAGING'], ['BH-PS', 'BH-PC', 'BH-PM'])

    def test_stock_split_across_warehouses_is_broken_down_biggest_first(self):
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[
                stock_row('PM001', 'BH-PS', 400, committed=50),
                stock_row('PM001', 'BH-PM', 1600),
            ],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        row = result['materials']['rows'][0]
        self.assertEqual(row['on_hand'], 2000)
        self.assertEqual(
            [(w['warehouse'], w['on_hand']) for w in row['warehouses']],
            [('BH-PM', 1600.0), ('BH-PS', 400.0)],
        )

    def test_a_warehouse_holding_nothing_is_left_out_of_the_breakdown(self):
        """A row of zeroes tells the operator nothing about where to go."""
        result = self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)],
            stock_rows=[
                stock_row('PM001', 'BH-PM', 2000),
                stock_row('PM001', 'BH-PS', 0),
            ],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        self.assertEqual(
            [w['warehouse'] for w in result['materials']['rows'][0]['warehouses']], ['BH-PM']
        )

    def test_a_failed_stock_read_claims_no_provenance(self):
        class Broken(FakePlanReader):
            def get_item_stock(self, item_codes, warehouses=None):
                raise RuntimeError('HANA unreachable')

        result = ProductionPlanCheckService(
            'TEST_CO',
            plan_reader=Broken([bom_row('PM001', 'Caps', 20)]),
            item_reader=FakeItemReader(),
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        self.assertEqual(result['materials']['warehouse_scope'], {})
        self.assertEqual(result['materials']['rows'][0]['searched_warehouses'], [])


class RawMaterialRegisterTests(PlanCheckBase):
    """Raw material availability is the store keeper's figure, not SAP's.

    SAP and the floor diverge badly on bulk oil — on the day this was built SAP
    carried 143.846 litres of `RM0000002` while the keeper had registered
    32,000 — and for planning tomorrow's run it is the keeper who is right about
    what is in the tank.
    """

    def register(self, item_code, warehouse, qty, *, active=True, as_of=None):
        return RawMaterialStock.objects.create(
            company=self.company,
            warehouse_code=warehouse,
            item_code=item_code,
            item_name=item_code,
            uom='LTR',
            qty=Decimal(str(qty)),
            as_of_date=as_of or self.day,
            is_active=active,
        )

    def oil_service(self, register_qty=None, sap_qty=1000, **kwargs):
        if register_qty is not None:
            self.register('RM0000002', 'BH-LO', register_qty)
        return self.service(
            bom_rows=[bom_row('RM0000002', 'Canola oil', 20, group='RAW MATERIAL', uom='LTR')],
            stock_rows=[stock_row('RM0000002', 'BH-LO', sap_qty, group='RAW MATERIAL')],
            **kwargs,
        )

    def check_oil(self, service, qty=100):
        return service.check(
            line_id=self.line.id, item_code='FG001', required_qty=qty, date=self.day,
        )['materials']['rows'][0]

    def test_the_register_figure_wins_over_sap(self):
        row = self.check_oil(self.oil_service(register_qty=32000, sap_qty=143.846))

        self.assertEqual(row['stock_source'], 'REGISTER')
        self.assertEqual(row['on_hand'], 32000)
        self.assertEqual(row['status'], STATUS_OK)
        self.assertEqual(row['shortfall'], 0)

    def test_sap_still_travels_on_the_row_so_a_stale_register_is_visible(self):
        row = self.check_oil(self.oil_service(register_qty=32000, sap_qty=143.846))

        self.assertEqual(row['sap_on_hand'], 143.846)
        self.assertEqual(row['register_as_of'], self.day.isoformat())

    def test_an_item_not_on_the_register_reads_zero(self):
        """Show 0 — and say it is a missing entry, not an empty tank.

        Both stop the run, but only one of them is fixed by going to look.
        """
        row = self.check_oil(self.oil_service(register_qty=None, sap_qty=99999))

        self.assertEqual(row['on_hand'], 0)
        self.assertTrue(row['register_missing'])
        self.assertEqual(row['status'], 'NO_STOCK_RECORD')
        self.assertEqual(row['shortfall'], 2000)

    def test_every_register_warehouse_counts_whatever_its_code(self):
        self.register('RM0000002', 'BH-LO', 1200)
        self.register('RM0000002', 'BH-OT', 800)
        self.register('RM0000002', 'SOMEWHERE-ELSE', 500)
        row = self.check_oil(self.oil_service(sap_qty=0))

        self.assertEqual(row['on_hand'], 2500)
        self.assertEqual(
            [w['warehouse'] for w in row['warehouses']],
            ['BH-LO', 'BH-OT', 'SOMEWHERE-ELSE'],
        )

    def test_a_removed_register_row_does_not_count(self):
        self.register('RM0000002', 'BH-LO', 5000, active=False)
        row = self.check_oil(self.oil_service(sap_qty=5000))

        self.assertEqual(row['on_hand'], 0)
        self.assertTrue(row['register_missing'])

    def test_the_figure_is_dated_by_its_stalest_count(self):
        self.register('RM0000002', 'BH-LO', 1000, as_of=date(2026, 9, 1))
        self.register('RM0000002', 'BH-OT', 1000, as_of=date(2026, 9, 8))
        row = self.check_oil(self.oil_service(sap_qty=0))

        self.assertEqual(row['register_as_of'], '2026-09-01')

    def test_the_register_has_no_committed_figure_to_net_off(self):
        """A hand-typed quantity carries no SAP reservations.

        Showing `OITW."IsCommited"` beside it would mix two people's arithmetic
        in one row, so free reads as not-applicable rather than as a number.
        """
        row = self.check_oil(self.oil_service(register_qty=32000, sap_qty=143.846))

        self.assertIsNone(row['free'])
        self.assertIsNone(row['committed'])
        self.assertNotEqual(row['status'], STATUS_TIGHT)

    def test_raw_material_survives_a_sap_stock_outage(self):
        """The register is a database read, so an unreachable HANA cannot blank it."""
        class Broken(FakePlanReader):
            def get_item_stock(self, item_codes, warehouses=None):
                raise RuntimeError('HANA unreachable')

        self.register('RM0000002', 'BH-LO', 32000)
        service = ProductionPlanCheckService(
            'TEST_CO',
            plan_reader=Broken(
                [bom_row('RM0000002', 'Canola oil', 20, group='RAW MATERIAL', uom='LTR')]
            ),
            item_reader=FakeItemReader(),
        )
        row = service.check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
        )['materials']['rows'][0]

        self.assertEqual(row['on_hand'], 32000)
        self.assertEqual(row['status'], STATUS_OK)
        self.assertNotEqual(row['status'], STATUS_UNKNOWN)

    def test_another_plan_can_still_contest_registered_oil(self):
        self.make_run(line=self.other_line, materials={'RM0000002': 1500})
        row = self.check_oil(self.oil_service(register_qty=3000, sap_qty=0))

        self.assertEqual(row['other_plan_demand'], 1500)
        self.assertEqual(row['status'], STATUS_CONTESTED)


class ApprovalScopeTests(PlanCheckBase):
    """Only packing material that has to be fetched is sent to the warehouse."""

    def pm_row(self, *, bh_pc=0, bh_pm=0, required_cases=100):
        stock = []
        if bh_pc:
            stock.append(stock_row('PM001', 'BH-PC', bh_pc))
        if bh_pm:
            stock.append(stock_row('PM001', 'BH-PM', bh_pm))
        return self.service(
            bom_rows=[bom_row('PM001', 'Caps', 20)], stock_rows=stock,
        ).check(
            line_id=self.line.id, item_code='FG001',
            required_qty=required_cases, date=self.day,
        )['materials']['rows'][0]

    def test_raw_material_is_always_requested_in_full(self):
        """RM goes on its own request, whatever the register or BH-PC hold."""
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code='BH-LO', item_code='RM0000002',
            qty=Decimal('50000'), as_of_date=self.day, uom='LTR',
        )
        row = self.service(
            bom_rows=[bom_row('RM0000002', 'Canola oil', 20, group='RAW MATERIAL')],
            stock_rows=[stock_row('RM0000002', 'BH-PC', 50000, group='RAW MATERIAL')],
        ).check(
            line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day,
        )['materials']['rows'][0]

        self.assertTrue(row['approval_required'])
        self.assertEqual(row['approval_qty'], 2000)
        self.assertIn('Raw Material register', row['approval_reason'])

    def test_packing_material_wholly_at_bh_pc_needs_no_approval(self):
        row = self.pm_row(bh_pc=5000)

        self.assertFalse(row['approval_required'])
        self.assertEqual(row['qty_at_production_consumption'], 2000)
        self.assertIn('BH-PC', row['approval_reason'])

    def test_only_the_part_that_must_be_fetched_is_approved(self):
        """3,000 caps at the line, 4,000 needed — the ask is 1,000, not 4,000."""
        row = self.pm_row(bh_pc=3000, bh_pm=9000, required_cases=200)

        self.assertEqual(row['required_qty'], 4000)
        self.assertTrue(row['approval_required'])
        self.assertEqual(row['approval_qty'], 1000)
        self.assertEqual(row['qty_at_production_consumption'], 3000)

    def test_packing_material_held_only_elsewhere_is_approved_in_full(self):
        row = self.pm_row(bh_pm=9000)

        self.assertTrue(row['approval_required'])
        self.assertEqual(row['approval_qty'], 2000)
        self.assertEqual(row['qty_at_production_consumption'], 0)

    def test_a_negative_bh_pc_balance_does_not_shrink_the_request(self):
        """SAP can report a negative on-hand; it is not stock already at the line."""
        row = self.pm_row(bh_pc=-500, bh_pm=9000)

        self.assertEqual(row['approval_qty'], 2000)
        self.assertEqual(row['qty_at_production_consumption'], 0)

    def test_the_summary_counts_the_lines_that_need_approval(self):
        result = self.service(
            bom_rows=[
                bom_row('PM001', 'Caps', 20),
                bom_row('PM002', 'Labels', 20),
                bom_row('RM0000002', 'Oil', 20, group='RAW MATERIAL'),
            ],
            stock_rows=[
                stock_row('PM001', 'BH-PC', 99999),
                stock_row('PM002', 'BH-PM', 99999),
                stock_row('RM0000002', 'BH-LO', 99999, group='RAW MATERIAL'),
            ],
        ).check(line_id=self.line.id, item_code='FG001', required_qty=100, date=self.day)

        # PM001 is wholly at BH-PC so it is not requested; PM002 must be
        # fetched, and the raw material is always requested.
        self.assertEqual(result['materials']['summary']['approval_lines'], 2)
        self.assertEqual(result['materials']['summary']['register_missing_lines'], 1)
