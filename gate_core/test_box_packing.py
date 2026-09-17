"""The SAP box/loose rule: SalFactor2 drives the split, CSD is the exception."""

from decimal import Decimal

from django.test import SimpleTestCase

from gate_core.services.box_packing import (
    box_invoice_units,
    is_csd_item,
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
