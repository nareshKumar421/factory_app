"""Tests for reading a pasted SAP/Excel block into movement lines.

What is worth testing here is not that a clean two-column paste works — it is
every way a real paste is not clean:

* an arbitrary SAP grid with columns nobody configured;
* a header that says "Item No." where the last sheet said "Item Code";
* a title row above the header;
* quantities written "1,200" or "240 PCS";
* the same item on two rows, which in a copied document means two batches;
* a box count pasted into a register that stores pieces — the one mistake that
  would be invisible afterwards;
* rows that cannot be resolved, which must be reported and never guessed.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from warehouse.models_manager import UserWarehouse
from warehouse.services import pf_movement_paste

PASTE_URL = "/api/v1/warehouse/pf-movements/paste/"

# What SAP answers for the codes these tests paste.
SAP_ITEMS = {
    "FG0000032": {
        "item_code": "FG0000032",
        "item_name": "COLD PRESS 1 LTR 20 PCS",
        "uom": "PCS",
        "pieces_per_box": 20,
        "litres_per_piece": 1.0,
        "sap_on_hand": 4800.0,
        "is_active": True,
    },
    "FG0000114": {
        "item_code": "FG0000114",
        "item_name": "POMACE OLIVE 2 LTR 10 PCS HANDLE",
        "uom": "PCS",
        "pieces_per_box": 10,
        "litres_per_piece": 2.0,
        "sap_on_hand": 0.0,
        "is_active": True,
    },
    # A finished good SAP holds no volume for, and no pack size either.
    "FG0000900": {
        "item_code": "FG0000900",
        "item_name": "GIFT CARTON ASSORTED",
        "uom": "PCS",
        "pieces_per_box": None,
        "litres_per_piece": None,
        "sap_on_hand": 12.0,
        "is_active": True,
    },
}


class PasteParseTests(TestCase):
    """The parser alone — no SAP, no database."""

    def test_a_plain_two_column_paste(self):
        parsed = pf_movement_paste.parse_block(
            "FG0000032\t240\nFG0000114\t100\n"
        )

        self.assertEqual(
            [(i["item_code"], i["qty"]) for i in parsed["items"]],
            [("FG0000032", Decimal("240")), ("FG0000114", Decimal("100"))],
        )
        self.assertEqual(parsed["skipped"], [])
        # No header row was recognisable, so it read by position.
        self.assertIsNone(parsed["header_row"])

    def test_a_headed_sheet_is_read_by_its_headers(self):
        parsed = pf_movement_paste.parse_block(
            "Item Code\tItem Description\tQuantity\n"
            "FG0000032\tCOLD PRESS 1 LTR 20 PCS\t240\n"
        )

        self.assertEqual(parsed["header_row"], 1)
        item = parsed["items"][0]
        self.assertEqual(item["item_code"], "FG0000032")
        self.assertEqual(item["qty"], Decimal("240"))
        self.assertEqual(item["pasted_name"], "COLD PRESS 1 LTR 20 PCS")

    def test_sap_spells_the_headers_differently_and_it_still_reads(self):
        parsed = pf_movement_paste.parse_block(
            "Item No.\tItem Description\tWhse\tQty\n"
            "FG0000032\tCOLD PRESS 1 LTR 20 PCS\tBH-PF\t240\n"
        )

        self.assertEqual(parsed["items"][0]["item_code"], "FG0000032")
        self.assertEqual(parsed["items"][0]["qty"], Decimal("240"))

    def test_a_title_row_above_the_header_is_skipped(self):
        # These grids grow a title or filter row over time; insisting on row 1
        # would break the first time somebody tidies the sheet.
        parsed = pf_movement_paste.parse_block(
            "Stock Report - Bhakharpur Production Finished\t\t\n"
            "\t\t\n"
            "Item Code\tItem Description\tQuantity\n"
            "FG0000032\tCOLD PRESS 1 LTR 20 PCS\t240\n"
        )

        self.assertEqual(parsed["header_row"], 3)
        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["skipped"], [])

    def test_an_arbitrary_grid_reads_by_position(self):
        # Item, description, batch, warehouse, quantity — no header at all.
        parsed = pf_movement_paste.parse_block(
            "FG0000032\tCOLD PRESS 1 LTR 20 PCS\tL3 004192\tBH-PF\t240\n"
        )

        item = parsed["items"][0]
        self.assertEqual(item["item_code"], "FG0000032")
        # The last numeric cell, not the batch number that begins with digits.
        self.assertEqual(item["qty"], Decimal("240"))
        self.assertEqual(item["pasted_name"], "COLD PRESS 1 LTR 20 PCS")

    def test_thousands_separators_and_trailing_units(self):
        parsed = pf_movement_paste.parse_block(
            "FG0000032\t1,200\nFG0000114\t240 PCS\n"
        )

        self.assertEqual(
            [i["qty"] for i in parsed["items"]],
            [Decimal("1200"), Decimal("240")],
        )

    def test_the_same_item_twice_is_summed_and_reported(self):
        # Two rows for one item in a copied document is two batches of the same
        # thing — unlike the manual form, where a duplicate is a mistake.
        parsed = pf_movement_paste.parse_block(
            "FG0000032\t240\nFG0000114\t100\nFG0000032\t60\n"
        )

        by_code = {i["item_code"]: i for i in parsed["items"]}
        self.assertEqual(by_code["FG0000032"]["qty"], Decimal("300"))
        self.assertEqual(by_code["FG0000032"]["lines"], [1, 3])
        # Summed, but never silently.
        self.assertEqual(parsed["combined_codes"], ["FG0000032"])

    def test_a_row_with_a_code_but_no_quantity_is_reported(self):
        parsed = pf_movement_paste.parse_block("FG0000032\t240\nFG0000114\t\n")

        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(len(parsed["skipped"]), 1)
        self.assertIn("No quantity found for FG0000114", parsed["skipped"][0]["reason"])
        # The line number matches what the keeper sees in the source.
        self.assertEqual(parsed["skipped"][0]["line"], 2)

    def test_a_quantity_with_no_code_beside_it_is_reported(self):
        # It might have been a line whose code column was empty, so it is not
        # quietly dropped.
        parsed = pf_movement_paste.parse_block("FG0000032\t240\n\t500\n")

        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(len(parsed["skipped"]), 1)
        self.assertIn("no item code beside it", parsed["skipped"][0]["reason"])

    def test_title_header_and_note_rows_are_ignored_without_complaint(self):
        # A row with neither a code nor a number is structure, not data.
        # Reporting it would put two or three "problems" on every SAP paste and
        # teach the keeper to skip the warnings that matter.
        parsed = pf_movement_paste.parse_block(
            "Stock Report - Bhakharpur Production Finished\t\t\n"
            "\t\t\n"
            "Item No.\tItem Description\tIn Stock\n"
            "FG0000032\tCOLD PRESS 1 LTR 20 PCS\t240\n"
            "\tsend the rest tomorrow\t\n"
        )

        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["skipped"], [])
        # "In Stock" is not accepted as a quantity header, so this read by
        # position — and still found the code and the figure.
        self.assertIsNone(parsed["header_row"])
        self.assertEqual(parsed["items"][0]["qty"], Decimal("240"))

    def test_zero_and_negative_quantities_are_reported(self):
        parsed = pf_movement_paste.parse_block(
            "FG0000032\t0\nFG0000114\t-5\n"
        )

        self.assertEqual(parsed["items"], [])
        self.assertEqual(len(parsed["skipped"]), 2)
        self.assertIn("nothing to send", parsed["skipped"][0]["reason"])
        self.assertIn("negative", parsed["skipped"][1]["reason"])

    def test_blank_lines_between_blocks_are_not_reported(self):
        parsed = pf_movement_paste.parse_block(
            "FG0000032\t240\n\t\n\nFG0000114\t100\n"
        )

        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(parsed["skipped"], [])

    def test_a_single_column_paste_is_refused_with_an_explanation(self):
        # Splitting on runs of spaces would look more forgiving and would cut
        # "COLD PRESS 5 LTR + COLD PRESS 1 LTR 4 PCS" into eight columns.
        with self.assertRaises(pf_movement_paste.PasteError) as ctx:
            pf_movement_paste.parse_block("FG0000032\nFG0000114\n")

        self.assertIn("no columns in it", str(ctx.exception))

    def test_a_csv_paste_is_read_when_there_are_no_tabs(self):
        parsed = pf_movement_paste.parse_block("FG0000032,240\nFG0000114,100\n")

        self.assertEqual(
            [i["item_code"] for i in parsed["items"]], ["FG0000032", "FG0000114"]
        )

    def test_an_empty_paste_is_refused(self):
        with self.assertRaises(pf_movement_paste.PasteError):
            pf_movement_paste.parse_block("   \n\n")

    def test_an_enormous_paste_is_refused_rather_than_sent_to_hana(self):
        block = "".join(f"FG000{n:04d}\t10\n" for n in range(2100))

        with self.assertRaises(pf_movement_paste.PasteError) as ctx:
            pf_movement_paste.parse_block(block)

        self.assertIn("Paste it in parts", str(ctx.exception))

    def test_a_stock_column_is_not_read_as_the_quantity_by_header(self):
        # "In Stock" is what SAP has, not what is being sent. Reading it as the
        # quantity would file the whole floor balance as an outward movement.
        parsed = pf_movement_paste.parse_block(
            "Item Code\tIn Stock\tQuantity\n" "FG0000032\t4800\t240\n"
        )

        self.assertEqual(parsed["items"][0]["qty"], Decimal("240"))


@override_settings(PF_MOVEMENT_WAREHOUSE="BH-PF")
class PasteResolveTests(TestCase):
    """Parsing plus the SAP lookup, with the reader stubbed."""

    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")

    def _resolve(self, text, unit="PCS", items=None):
        with patch(
            "warehouse.services.pf_movement_paste.WMSHanaReader"
        ) as reader_class:
            reader_class.return_value.fetch_items_by_code.return_value = (
                SAP_ITEMS if items is None else items
            )
            result = pf_movement_paste.resolve_block(
                company_code=self.company.code, text=text, unit=unit
            )
            self.call = reader_class.return_value.fetch_items_by_code.call_args
        return result

    def test_resolved_lines_carry_sap_name_factors_and_on_hand(self):
        result = self._resolve("FG0000032\t240\n")

        line = result["lines"][0]
        self.assertEqual(line["item_code"], "FG0000032")
        self.assertEqual(line["item_name"], "COLD PRESS 1 LTR 20 PCS")
        self.assertEqual(line["pieces"], 240)
        self.assertEqual(line["pieces_per_box"], 20)
        self.assertEqual(line["litres_per_piece"], 1.0)
        self.assertEqual(line["sap_on_hand"], 4800.0)
        self.assertEqual(result["total_pieces"], 240)

    def test_only_finished_goods_are_accepted(self):
        # The group filter is applied in SAP, not after the fact, so a paste
        # cannot slip a preform onto a finished-goods movement.
        self._resolve("FG0000032\t240\n")

        self.assertEqual(self.call.kwargs["item_group_code"], 102)
        self.assertEqual(self.call.kwargs["warehouse_code"], "BH-PF")

    def test_a_box_paste_is_multiplied_by_each_items_own_pack_size(self):
        result = self._resolve("FG0000032\t12\nFG0000114\t10\n", unit="BOX")

        by_code = {line["item_code"]: line for line in result["lines"]}
        self.assertEqual(by_code["FG0000032"]["pieces"], 240)  # 12 x 20
        self.assertEqual(by_code["FG0000114"]["pieces"], 100)  # 10 x 10
        self.assertEqual(result["unit"], "BOX")

    def test_a_box_paste_refuses_an_item_with_no_pack_size(self):
        # Assuming one piece per box is how a box count becomes a wrong piece
        # count that nobody can spot afterwards.
        result = self._resolve("FG0000900\t5\n", unit="BOX")

        self.assertEqual(result["lines"], [])
        self.assertIn("no pack size", result["unresolved"][0]["reason"])
        self.assertIn("in pieces", result["unresolved"][0]["reason"])

    def test_the_same_item_in_boxes_is_summed_then_converted(self):
        result = self._resolve("FG0000032\t12\nFG0000032\t3\n", unit="BOX")

        self.assertEqual(result["lines"][0]["pieces"], 300)  # (12 + 3) x 20
        self.assertEqual(result["combined_codes"], ["FG0000032"])

    def test_a_code_sap_does_not_know_is_reported_not_dropped(self):
        result = self._resolve("FG0000032\t240\nFG9999999\t50\n")

        self.assertEqual([l["item_code"] for l in result["lines"]], ["FG0000032"])
        self.assertEqual(result["unresolved"][0]["item_code"], "FG9999999")
        self.assertIn("not a finished-goods item", result["unresolved"][0]["reason"])

    def test_a_fractional_piece_count_is_reported_rather_than_rounded(self):
        result = self._resolve("FG0000032\t240.5\n")

        self.assertEqual(result["lines"], [])
        self.assertIn("not a whole number", result["unresolved"][0]["reason"])

    def test_hana_being_down_leaves_the_rows_usable_and_says_why(self):
        with patch(
            "warehouse.services.pf_movement_paste.WMSHanaReader",
            side_effect=RuntimeError("HANA unreachable"),
        ):
            result = pf_movement_paste.resolve_block(
                company_code=self.company.code, text="FG0000032\t240\n"
            )

        self.assertEqual(result["lines"], [])
        self.assertIn("HANA unreachable", result["lookup_error"])
        self.assertIn("could not be reached", result["unresolved"][0]["reason"])

    def test_an_unknown_unit_is_refused(self):
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            pf_movement_paste.resolve_block(
                company_code=self.company.code, text="FG0000032\t240\n", unit="LTR"
            )

    def test_an_item_inactive_in_sap_is_flagged_but_still_offered(self):
        items = {
            "FG0000032": {**SAP_ITEMS["FG0000032"], "is_active": False},
        }
        result = self._resolve("FG0000032\t240\n", items=items)

        self.assertTrue(result["lines"][0]["inactive_in_sap"])


@override_settings(PF_MOVEMENT_WAREHOUSE="BH-PF")
class PasteAPITests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        self.role = UserRole.objects.create(name="Store")

        self.keeper = self._user("pf@example.com", "PF Keeper", "E-PF")
        self._grant(self.keeper, "can_view_pf_movement", "can_record_pf_movement")
        UserWarehouse.objects.create(
            user=self.keeper, company=self.company, warehouse_code="BH-PF"
        )

        self.viewer = self._user("plan@example.com", "Planner", "E-PL")
        self._grant(self.viewer, "can_view_pf_movement")

    def _user(self, email, name, code):
        user = User.objects.create_user(
            email=email, full_name=name, employee_code=code, password="x"
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role)
        return user

    def _grant(self, user, *codenames):
        for codename in codenames:
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="warehouse", codename=codename
                )
            )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

    def _post(self, user, payload):
        with patch(
            "warehouse.services.pf_movement_paste.WMSHanaReader"
        ) as reader_class:
            reader_class.return_value.fetch_items_by_code.return_value = SAP_ITEMS
            return self._client(user).post(PASTE_URL, payload, format="json")

    def test_a_keeper_can_paste_a_block(self):
        response = self._post(
            self.keeper,
            {
                "text": "Item Code\tQuantity\nFG0000032\t240\nFG0000114\t100\n",
                "unit": "PCS",
                "from_warehouse": "BH-PF",
            },
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["lines"]), 2)
        self.assertEqual(response.data["total_pieces"], 340)
        self.assertEqual(response.data["skipped"], [])

    def test_a_viewer_cannot_paste(self):
        # Nothing is written by this endpoint, but it exists only to fill a form
        # a viewer may not submit — so it is gated with the form, not the list.
        response = self._post(self.viewer, {"text": "FG0000032\t240", "unit": "PCS"})

        self.assertEqual(response.status_code, 403)

    def test_the_unit_must_be_stated(self):
        # No default: pieces-vs-boxes is the one thing a paste cannot imply, and
        # guessing it wrong is invisible afterwards.
        response = self._post(self.keeper, {"text": "FG0000032\t240"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("unit", response.data)

    def test_an_unreadable_block_answers_400_with_the_reason(self):
        response = self._post(
            self.keeper, {"text": "FG0000032\nFG0000114\n", "unit": "PCS"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("no columns in it", response.data["detail"])

    def test_an_empty_paste_is_refused_by_the_serializer(self):
        response = self._post(self.keeper, {"text": "   ", "unit": "PCS"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("text", response.data)

    def test_the_paste_endpoint_writes_nothing(self):
        from warehouse.models_pf_movement import PFStockMovement

        self._post(
            self.keeper,
            {"text": "FG0000032\t240", "unit": "PCS"},
        )

        self.assertFalse(PFStockMovement.objects.exists())
