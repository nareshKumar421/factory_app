"""The run list's ``produced_cases``: what each run has made so far.

The Production Execution board totals it for the dates on screen, so a line
still filling has to count what its segments logged — ``total_production`` is
only entered at completion and reads 0 until then.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import status

from production_execution.models import (
    ProductionLine, ProductionRun, ProductionSegment, RunStatus,
)
from production_execution.serializers import ProductionRunListSerializer
from production_execution.tests import BASE_URL, BaseTestCase


class RunListProducedCasesTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        self.line = ProductionLine.objects.create(company=self.company, name='L1')
        self.anchor = timezone.now() - timedelta(hours=6)

    def _run(self, number, run_status, total='0', run_date=None):
        return ProductionRun.objects.create(
            company=self.company, line=self.line, run_number=number,
            date=run_date or date.today(), status=run_status,
            total_production=Decimal(total),
        )

    def _segment(self, run, cases, active=False):
        return ProductionSegment.objects.create(
            production_run=run, start_time=self.anchor,
            end_time=None if active else self.anchor + timedelta(hours=1),
            produced_cases=Decimal(cases), is_active=active,
        )

    def _listed(self, **params):
        resp = self.client.get(f'{BASE_URL}/runs/', params)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return {row['run_number']: row for row in resp.data}

    def test_a_running_line_counts_its_segments(self):
        run = self._run(1, RunStatus.IN_PROGRESS)
        self._segment(run, '100.5')
        self._segment(run, '50', active=True)

        row = self._listed()[1]
        self.assertEqual(row['total_production'], '0.0')
        self.assertEqual(row['produced_cases'], '150.5')

    def test_a_completed_run_counts_its_entered_total(self):
        # The count entered at completion wins over whatever the segments say.
        run = self._run(1, RunStatus.COMPLETED, total='900')
        self._segment(run, '271')

        self.assertEqual(self._listed()[1]['produced_cases'], '900.0')

    def test_a_draft_or_an_unlogged_run_reads_zero(self):
        self._run(1, RunStatus.DRAFT)
        self._run(2, RunStatus.IN_PROGRESS)

        rows = self._listed()
        self.assertEqual(rows[1]['produced_cases'], '0.0')
        self.assertEqual(rows[2]['produced_cases'], '0.0')

    def test_segments_are_not_counted_twice_across_runs(self):
        # One grouped sum for the whole list must still keep runs apart.
        first = self._run(1, RunStatus.IN_PROGRESS)
        second = self._run(2, RunStatus.IN_PROGRESS)
        for cases in ('10', '20', '30'):
            self._segment(first, cases)
        self._segment(second, '5')

        rows = self._listed()
        self.assertEqual(rows[1]['produced_cases'], '60.0')
        self.assertEqual(rows[2]['produced_cases'], '5.0')

    def test_the_date_filter_decides_which_runs_are_counted(self):
        self._run(1, RunStatus.COMPLETED, total='40', run_date=date(2026, 8, 31))
        self._run(2, RunStatus.COMPLETED, total='60', run_date=date(2026, 9, 1))

        rows = self._listed(date_from='2026-09-01', date_to='2026-09-30')
        self.assertEqual(set(rows), {2})

    def test_a_lone_run_adds_its_own_segments_up(self):
        # The yield report serializes one run without the list's annotation.
        run = self._run(1, RunStatus.IN_PROGRESS)
        self._segment(run, '12.5')
        self._segment(run, '7.5')

        data = ProductionRunListSerializer(ProductionRun.objects.get(pk=run.pk)).data
        self.assertEqual(data['produced_cases'], '20.0')
