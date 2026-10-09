"""The SAP box/loose rule: SalFactor2 drives the split, CSD is the exception."""

from decimal import Decimal

from django.test import SimpleTestCase

from gate_core.services.box_packing import (
    box_invoice_units,
    is_csd_item,
    load_box_count,
    pieces_per_box,
    split_line,
)


class BoxPackingRuleTests(SimpleTestCase):
    def test_boxed_item_divides_quantity_by_sal_factor2(self):
        packing = split_line(100, 4, "COLD PRESS 5 LTR 4 PCS")
        self.assertEqual(packing.boxes, 25)
        self.assertEqual(packing.loose, Decimal("0"))
        self.assertFalse(packing.is_loose)

    def test_uneven_quantity_leaves_a_loose_remainder(self):
        # SAP prints INT(qty / factor) boxes and the remainder as loose pieces.
        packing = split_line(37, 20, "COLD PRESS SUNFLOWER 1 LTR 20 PCS")
        self.assertEqual(packing.boxes, 1)
        self.assertEqual(packing.loose, Decimal("17"))

    def test_sal_factor2_of_one_ships_loose_not_one_box_per_piece(self):
        # FG0000381: the bill prints "0 Box  500.00 PCS".
        packing = split_line(500, 1, "EXTRA VIRGIN OLIVE OIL 10ML")
        self.assertEqual(packing.boxes, 0)
        self.assertEqual(packing.loose, Decimal("500"))
        self.assertTrue(packing.is_loose)

    def test_csd_item_stays_box_counted_at_one_piece_per_box(self):
        # CSD SKUs also carry SalFactor2 = 1, but there one box IS the billed piece.
        for name in (
            "JIVO EXTRA VIRGIN OLIVE OIL 1 LTR 16 PCS ( CSD )",
            "MUSTARD OIL 100 MLS 20 PCS(CSD)",
            "JIVO KACHI GHANI COLD PRESSED MUSTARD OIL 5 LTR 4 PCS ( CSD )",
        ):
            with self.subTest(name=name):
                packing = split_line(29, 1, name)
                self.assertEqual(packing.boxes, 29)
                self.assertEqual(packing.loose, Decimal("0"))

    def test_missing_factor_is_treated_as_loose(self):
        # An item SAP never configured must not invent a box count.
        for factor in (None, 0, ""):
            with self.subTest(factor=factor):
                packing = split_line(12, factor, "SOYABEAN OIL 12 KGS")
                self.assertEqual(packing.boxes, 0)
                self.assertEqual(packing.loose, Decimal("12"))

    def test_item_name_pack_size_is_ignored(self):
        # The old rule divided by the name's "20 PCS"; CSD boxes are billed 1 pc each,
        # so trusting the name under-counted the boxes 20x.
        packing = split_line(37, 1, "REFINED OIL 1 LTR 20 PCS(CSD)")
        self.assertEqual(packing.boxes, 37)

    def test_zero_and_negative_quantities_are_empty(self):
        for quantity in (0, -5):
            with self.subTest(quantity=quantity):
                packing = split_line(quantity, 4, "COLD PRESS 5 LTR 4 PCS")
                self.assertEqual(packing.boxes, 0)
                self.assertEqual(packing.loose, Decimal("0"))

    def test_fractional_quantity_on_a_csd_line_still_needs_a_box(self):
        self.assertEqual(split_line(Decimal("2.5"), 1, "OIL 1 LTR (CSD)").boxes, 3)

    def test_pieces_per_box_reports_none_for_a_loose_item(self):
        self.assertIsNone(pieces_per_box(1, "EXTRA VIRGIN OLIVE OIL 10ML"))
        self.assertEqual(pieces_per_box(1, "OIL 1 LTR (CSD)"), Decimal("1"))
        self.assertEqual(pieces_per_box(16, "OIL 1 LTR 16 PCS"), Decimal("16"))

    def test_csd_detection_is_word_bounded(self):
        self.assertTrue(is_csd_item("EXTRA LIGHT OLIVE 250 MLS 4 PCS(CSD)"))
        self.assertTrue(is_csd_item("TIKKI BARCODE CSD 2 LTR CANOLA"))
        self.assertFalse(is_csd_item("EXTRA VIRGIN OLIVE OIL 10ML"))
        self.assertFalse(is_csd_item(""))
        self.assertFalse(is_csd_item(None))


class BoxInvoiceUnitTests(SimpleTestCase):
    """What one physical box is worth against the bill's invoiced quantity."""

    def test_csd_carton_counts_as_one_however_many_pieces_it_declares(self):
        # A CSD bill counts boxes: its line of 4 means four cartons, so the 20 bottles
        # the carton label declares must not be compared against it.
        self.assertEqual(
            box_invoice_units(20, 1, "MUSTARD OIL 100 MLS 20 PCS(CSD)"), Decimal("1")
        )
        self.assertEqual(
            box_invoice_units(4, 1, "EXTRA LIGHT OLIVE 250 MLS 4 PCS(CSD)"), Decimal("1")
        )

    def test_piece_billed_item_counts_its_pieces(self):
        self.assertEqual(box_invoice_units(20, 20, "OIL 1 LTR 20 PCS"), Decimal("20"))

    def test_loose_item_counts_its_pieces(self):
        # A 362-piece carton of a loose item covers 362 of the invoiced pieces.
        self.assertEqual(
            box_invoice_units(362, 1, "EXTRA VIRGIN OLIVE OIL 10ML"), Decimal("362")
        )

    def test_missing_box_quantity_is_zero_not_an_error(self):
        self.assertEqual(box_invoice_units(None, 20, "OIL 1 LTR 20 PCS"), Decimal("0"))


class SalFactor3OptInTests(SimpleTestCase):
    """SAP's own carton marker, which only the bill summary passes.

    ``BoxInt`` tests ``SalFactor3 > 1`` before it looks at ``SalFactor2``, so a
    line SAP bills in cartons prints as boxes however its name reads. The
    scanning callers do not pass it: they count physical boxes against a bill,
    and moving what a box means there would move the dispatch and BST quantity
    locks. These tests pin both halves.
    """

    def test_sal_factor3_makes_the_billed_unit_a_box(self):
        # FG0000013 REFINED OIL 1000 MLS: SalFactor2 = 1, SalFactor3 = 20. One
        # billed unit is a sealed 20-bottle carton, not a loose bottle.
        packing = split_line(3, 1, "REFINED OIL 1000 MLS", 20)
        self.assertEqual(packing.boxes, 3)
        self.assertEqual(packing.loose, Decimal("0"))
        self.assertEqual(packing.pieces_per_box, Decimal("1"))

    def test_sal_factor3_of_one_leaves_the_line_loose(self):
        packing = split_line(500, 1, "EXTRA VIRGIN OLIVE OIL 10ML", 1)
        self.assertEqual(packing.boxes, 0)
        self.assertEqual(packing.loose, Decimal("500"))
        self.assertTrue(packing.is_loose)

    def test_omitting_sal_factor3_leaves_every_other_caller_unchanged(self):
        # The same three item shapes the scanning callers rely on.
        self.assertEqual(split_line(1, 1, "REFINED OIL 1000 MLS"),
                         split_line(1, 1, "REFINED OIL 1000 MLS", None))
        self.assertTrue(split_line(1, 1, "REFINED OIL 1000 MLS").is_loose)
        self.assertEqual(split_line(32, 16, "EXTRA LIGHT OLIVE 1 LTR 16 PCS").boxes, 2)
        self.assertEqual(split_line(4, 1, "MUSTARD OIL 100 MLS 20 PCS(CSD)").boxes, 4)

    def test_sal_factor3_wins_over_a_pack_size(self):
        """SAP checks it first, so an item carrying both is billed in cartons."""
        packing = split_line(2, 16, "SOME ITEM", 16)
        self.assertEqual(packing.boxes, 2)
        self.assertEqual(packing.loose, Decimal("0"))


def _line(item_code, quantity, sal_factor2=1, item_name="", uom="PCS", sal_factor3=0,
          litres_each=0):
    return {
        "item_code": item_code,
        "item_name": item_name,
        "quantity": Decimal(str(quantity)),
        "uom": uom,
        "litres": Decimal(str(quantity)) * Decimal(str(litres_each)),
        "sal_factor2": Decimal(str(sal_factor2)),
        "sal_factor3": Decimal(str(sal_factor3)),
    }


class LoadBoxCountTests(SimpleTestCase):
    """Every box a bill goes out in -- the dispatch plan export's Boxes column."""

    def test_full_boxes_divide_by_the_pack_size(self):
        self.assertEqual(load_box_count([_line("FG0000002", 100, 4)]), 25)

    def test_a_part_box_is_one_more_box(self):
        # 37 of a 20-PCS item: 1 full box + 17 pieces repacked into a second.
        self.assertEqual(load_box_count([_line("FG0000002", 37, 20)]), 2)

    def test_less_than_a_box_is_still_a_box(self):
        # The Mart shape that used to read 0: 3 pieces of a 12-PCS item.
        self.assertEqual(load_box_count([_line("FG0000004", 3, 12)]), 1)

    def test_lines_of_one_item_are_split_together_like_the_scan(self):
        # 13 + 3 pieces of a 16-PCS item fill one box, not two.
        lines = [_line("FG0000142", 13, 16), _line("fg0000142 ", 3, 16)]
        self.assertEqual(load_box_count(lines), 1)

    def test_different_items_are_counted_apart(self):
        lines = [_line("FG0000142", 13, 16), _line("FG0000143", 3, 16)]
        self.assertEqual(load_box_count(lines), 2)

    def test_csd_counts_one_box_per_billed_unit(self):
        line = _line("FG0000394", 143, 1, "JIVO EXTRA LIGHT OLIVE OIL 1 LTR 16 PCS ( CSD )")
        self.assertEqual(load_box_count([line]), 143)

    def test_sal_factor3_counts_one_carton_per_billed_unit(self):
        # FG0000013 REFINED OIL 1000 MLS: no CSD in the name, SalFactor3 = 20.
        line = _line("FG0000013", 5, 1, "REFINED OIL 1000 MLS", sal_factor3=20)
        self.assertEqual(load_box_count([line]), 5)

    def test_a_big_unboxed_unit_is_one_box_each(self):
        # 15 LTR tins carry SalFactor2 = 1: each tin goes on the truck by itself.
        tins = _line("FG0000015", 16, 1, "REFINED OIL 15 LTR", litres_each=15)
        self.assertEqual(load_box_count([tins]), 16)
        drums = _line("FG0000034", 3, 1, "EXTRA LIGHT OLIVE 200 LTR 1 PCS", litres_each=200)
        self.assertEqual(load_box_count([drums]), 3)
        sets = _line("SL0000029", 2, 1, "COLD PRESS 5 LTR + 1 LTR", "SET", litres_each=6)
        self.assertEqual(load_box_count([sets]), 2)

    def test_small_unboxed_goods_fill_at_least_one_box_not_one_each(self):
        # FG0000381: 300 bottles of 10 ML are a few cartons, never 300 boxes. SAP gives
        # no pack size, so the item counts the one box it surely fills.
        bottles = _line("FG0000381", 300, 1, "EXTRA VIRGIN OLIVE OIL 10ML", litres_each="0.01")
        self.assertEqual(load_box_count([bottles]), 1)
        spices = _line("FG0000196", 16, 1, "SPICES BLACK PEPPER 100 GMS", "NOS")
        self.assertEqual(load_box_count([bottles, spices]), 2)

    def test_bulk_billed_by_measure_has_no_boxes(self):
        lines = [
            _line("RM0000002", 36780.555, 1, "CANOLA COLD PRESS LOOSE OIL", "LTR"),
            _line("RM0000067", 5040, 1, "DESI GHEE KGS", "KGS"),
        ]
        self.assertEqual(load_box_count(lines), 0)

    def test_packaging_and_service_lines_are_left_out(self):
        lines = [
            _line("PM0000003", 161924, 1, "CAPS 5 LTR/15 LTR"),
            _line("PM0000634", 40, 20, "CARTON"),
            _line("", 1, 0, "IT consulting and support services", ""),
            _line("FG0000002", 20, 20),
        ]
        self.assertEqual(load_box_count(lines), 1)

    def test_no_lines_is_no_boxes(self):
        self.assertEqual(load_box_count([]), 0)
