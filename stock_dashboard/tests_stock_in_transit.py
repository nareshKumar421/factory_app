"""Stock-in-transit: the SAP rule for what is still on the road.

A load is in transit when SAP says it left and does not yet say it arrived --
an A/R invoice at the sending company with no Goods Receipt PO answering it at
the receiving one. These cover the parts of that rule that are cheap to get
wrong and expensive to notice: the two filters that keep the figure honest, and
the age banding that decides which loads are shouted about.
"""

from django.test import SimpleTestCase

from stock_dashboard.hana_reader import HanaStockDashboardReader
from stock_dashboard.views import StockInTransitAPI


class _StubConnection:
    schema = "JIVO_OIL_HANADB"


class _QueryCapturingReader(HanaStockDashboardReader):
    """A reader that records SQL instead of running it.

    Deliberately bypasses ``__init__``: the query shape is worth testing on its
    own, and standing up a company context here would make these tests need a
    live SAP.
    """

    def __init__(self):  # noqa: D107 - see class docstring
        self.connection = _StubConnection()
        self._columns_cache = {"OITM": {"U_Gross_Weight"}}
        self.queries = []

    def _execute(self, query, params):
        self.queries.append((query, params))
        return []


class UnreceivedDispatchQueryTests(SimpleTestCase):
    def setUp(self):
        self.reader = _QueryCapturingReader()

    def _build(self, **overrides):
        kwargs = {
            "receiving_schema": "JIVO_MART_HANADB",
            "customer_codes": ("CUSTA000606",),
            "warehouses": ("BH-PF", "BH-BT"),
            "lookback_days": 60,
        }
        kwargs.update(overrides)
        self.reader.get_unreceived_intercompany_dispatches(**kwargs)
        return self.reader.queries[-1]

    def test_restricts_to_shipping_warehouses(self):
        """The warehouse filter is the guard against rate-difference notes.

        Three of them in August 2026 came to 533 tonnes between them -- four
        times the genuine figure. They are raised against another plant's
        warehouse and, being purely financial, can never be received, so
        without this filter they would sit on the board as permanent traffic.
        """
        query, params = self._build()

        self.assertIn('UPPER(l."WhsCode") IN', query)
        self.assertIn("BH-PF", params)
        self.assertIn("BH-BT", params)

    def test_bounds_the_lookback_and_always_looks_backwards(self):
        """A positive lookback must not become a window into the future."""
        _, params = self._build(lookback_days=30)

        self.assertIn(-30, params)

    def test_matches_the_receipt_in_the_receiving_schema(self):
        query, _ = self._build()

        self.assertIn('"JIVO_MART_HANADB"."OPDN"', query)
        self.assertIn('"NumAtCard"', query)
        # Unmatched only: a receipt that exists means the load has landed.
        self.assertIn('WHERE r."Ref" IS NULL', query)

    def test_excludes_cancelled_documents_on_both_sides(self):
        query, _ = self._build()

        self.assertEqual(query.count('"CANCELED" = \'N\''), 2)

    def test_reads_no_sap_at_all_without_a_customer_or_a_warehouse(self):
        """No route configured is not the same as a route with nothing on it.

        An empty ``IN ()`` list is a SQL error, and a query built round one
        would take the whole tile down rather than report an unconfigured leg.
        """
        self.assertEqual(
            self.reader.get_unreceived_intercompany_dispatches(
                receiving_schema="JIVO_MART_HANADB",
                customer_codes=(),
                warehouses=("BH-PF",),
            ),
            [],
        )
        self.assertEqual(
            self.reader.get_unreceived_intercompany_dispatches(
                receiving_schema="JIVO_MART_HANADB",
                customer_codes=("CUSTA000606",),
                warehouses=(),
            ),
            [],
        )
        self.assertEqual(self.reader.queries, [])


class TransitBandingTests(SimpleTestCase):
    """The bands are exclusive, and their edges match the labels above them."""

    def setUp(self):
        self.view = StockInTransitAPI()

    def test_edges_fall_on_the_side_the_labels_promise(self):
        self.assertEqual(self.view._band_for(0), "fresh")
        self.assertEqual(self.view._band_for(3), "fresh")
        self.assertEqual(self.view._band_for(4), "ageing")
        self.assertEqual(self.view._band_for(7), "ageing")
        self.assertEqual(self.view._band_for(8), "stale")

    def test_every_load_lands_in_exactly_one_band(self):
        bands = [self.view._band_for(days) for days in range(0, 40)]

        self.assertEqual(len(bands), 40)
        self.assertEqual(set(bands), {"fresh", "ageing", "stale"})

    def test_an_empty_board_reports_bands_rather_than_nothing(self):
        """Zero loads on the road is a real answer and must render as one."""
        bands = self.view._empty_bands()

        self.assertEqual(set(bands), {"fresh", "ageing", "stale"})
        for band in bands.values():
            self.assertEqual(band["loads"], 0)
            self.assertEqual(band["tonnes"], 0)
            self.assertTrue(band["label"])
