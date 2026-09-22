"""The wall board reads a day's electricity off the READING, not the meter.

Kept beside :mod:`factory_expense.tests` rather than in it: this is the board
end of a register change that lives in ``maintenance``, and it borrows that
module's fixture rather than building a second one.
"""

from datetime import date
from decimal import Decimal

from maintenance.models import (
    DailyElectricityReading,
    ElectricityConsumer,
    ElectricityMeter,
)

from .services import build_board
from .tests import CostMasterFixture


class ReadingAttributionBoardTests(CostMasterFixture):
    """A day belongs to whoever the READING says, not to whoever the meter
    usually feeds.

    The meter master is a standing arrangement; a day is a fact. Once the
    register lets an operator say "this day was Beverages alone", or "this was
    Sidle, who is not one of our companies at all", the board has to read that
    or the correction is cosmetic.
    """

    def setUp(self):
        super().setUp()
        self.day = date(2026, 6, 15)
        self.sidle = ElectricityConsumer.objects.create(name="Sidle", code="SIDLE")
        self.meter = ElectricityMeter.objects.create(
            name="KWH", rate_per_unit=Decimal("7"), multiplying_factor=Decimal("1"),
        )
        self.meter.companies.set([self.company, self.other])
        self.reading = DailyElectricityReading.objects.create(
            meter=self.meter, date=self.day,
            opening_reading=Decimal("0"), closing_reading=Decimal("1000"),
            multiplying_factor=Decimal("1"), rate_per_unit=Decimal("7"),
        )

    def test_a_day_moved_onto_one_company_leaves_the_others_board(self):
        self.reading.companies.set([self.other])

        oil = build_board([self.company], self.day)
        self.assertEqual(oil["buckets"]["ELECTRICITY"]["today"], Decimal("0.00"))

        beverages = build_board([self.other], self.day)
        self.assertEqual(beverages["buckets"]["ELECTRICITY"]["today"], Decimal("7000.00"))

    def test_a_sidle_day_lands_on_nobodys_board(self):
        """Sidle is not a company, so its units are not a company's cost."""
        self.reading.companies.clear()
        self.reading.consumers.set([self.sidle])

        board = build_board([self.company, self.other], self.day)
        self.assertEqual(board["buckets"]["ELECTRICITY"]["today"], Decimal("0.00"))

    def test_a_reading_that_names_nobody_still_follows_its_meter(self):
        """History entered before the register asked must keep counting."""
        board = build_board([self.company], self.day)
        self.assertEqual(board["buckets"]["ELECTRICITY"]["today"], Decimal("7000.00"))

    def test_a_shared_day_is_still_counted_once(self):
        self.reading.companies.set([self.company, self.other])
        board = build_board([self.company, self.other], self.day)
        # NOT 14000: the reading is one row, whoever it names.
        self.assertEqual(board["buckets"]["ELECTRICITY"]["today"], Decimal("7000.00"))
