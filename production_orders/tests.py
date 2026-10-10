"""Production order entries: each step's preview, draft and post, payloads, rights, API.

SAP is simulated (``FakeSap``): HANA reads come from it, and a Service Layer
post changes it the way SAP would, so a later step sees what an earlier one
posted.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPValidationError
from sap_client.models import SapApproverIdentity
from sap_postings.models import SapPosting, SapPostingStatus

from . import payloads, services
from .models import STEPS, EntryStatus, Step
from .services import EntryError

TODAY = timezone.localdate()
MFG = TODAY - timedelta(days=1)

PRODUCT = {
    "item_code": "FG0000011",
    "item_name": "MUSTARD KACCHI GHANI 5 LTR 4 PCS",
    "uom": "PCS",
    "pieces_per_box": Decimal("4"),
    "litres_per_piece": Decimal("5"),
    "batch_managed": True,
    "sub_group": "MUSTARD",
    "item_group": 102,
    "frozen": False,
    "variety": "MUSTARD",
    "bom_warehouse": "BH-PF",
    "bom_quantity": Decimal("4"),
    "bom_type": "P",
}

TREE = {
    "quantity": 4.0,
    "warehouse": "BH-PF",
    "lines": [
        {"item_type": "item", "item_code": "RM0000003", "item_name": "MUSTARD LOOSE OIL",
         "quantity": 20.0, "warehouse": "BH-PC", "issue_method": "Manual", "uom": "LTR"},
        {"item_type": "item", "item_code": "PM0000053", "item_name": "TIN 5 LTR",
         "quantity": 4.0, "warehouse": "BH-PC", "issue_method": "Manual", "uom": ""},
        {"item_type": "item", "item_code": "PM0000075", "item_name": "TAPE",
         "quantity": 1.1176, "warehouse": "BH-PC", "issue_method": "Manual", "uom": ""},
        {"item_type": "text", "item_code": "", "item_name": "note", "quantity": 0,
         "warehouse": "", "issue_method": "Manual", "uom": ""},
        {"item_type": "resource", "item_code": "JWPL09240002", "item_name": "FILLING COST",
         "quantity": 20.0, "warehouse": "BH-PC", "issue_method": "Manual", "uom": ""},
    ],
}

# A second product, for changing a planned order's product.
PRODUCT_2 = {**PRODUCT, "item_code": "FG0000118", "item_name": "CANOLA OIL 5 LTR 4 PCS", "variety": "CANOLA"}
TREE_2 = {
    "quantity": 4.0,
    "warehouse": "BH-PF",
    "lines": [
        {"item_type": "item", "item_code": "RM0000002", "item_name": "CANOLA LOOSE OIL",
         "quantity": 20.0, "warehouse": "BH-PC", "issue_method": "Manual", "uom": "LTR"},
        {"item_type": "item", "item_code": "PM0000053", "item_name": "TIN 5 LTR",
         "quantity": 4.0, "warehouse": "BH-PC", "issue_method": "Manual", "uom": ""},
        {"item_type": "resource", "item_code": "JWPL09240001", "item_name": "FILLING COST CANOLA",
         "quantity": 20.0, "warehouse": "BH-PC", "issue_method": "Manual", "uom": ""},
    ],
}
TREES = {"FG0000011": TREE, "FG0000118": TREE_2}

ORDER_ENTRY = 501


class FakeSap:
    """HANA as the reader sees it, and the Service Layer that changes it."""

    def __init__(self):
        self.product_row = dict(PRODUCT)
        self.other_products = {"FG0000118": dict(PRODUCT_2)}
        self.order_item = None
        self.patch_headers = []
        self.patch_error = None
        self.flags = {"RM0000003": True, "FG0000011": True}
        self.stock = {
            ("RM0000003", "BH-PC"): Decimal("1000"),
            ("PM0000053", "BH-PC"): Decimal("500"),
            ("PM0000075", "BH-PC"): Decimal("500"),
        }
        self.batches = {
            ("RM0000003", "BH-PC"): [
                {"batch_number": "OLD", "quantity": Decimal("30"), "status": "0", "in_date": date(2026, 8, 1)},
                {"batch_number": "LOCKED", "quantity": Decimal("500"), "status": "1", "in_date": date(2026, 8, 2)},
                {"batch_number": "NEW", "quantity": Decimal("900"), "status": "0", "in_date": date(2026, 9, 1)},
            ]
        }
        self.litres = []
        self.batch_numbers = []
        self.order_row = None
        self.issue = None
        self.receipt = None
        self.posts = []
        self.patches = []
        self.post_error = None
        self.release_error = None
        self.sap_order_filters = None
        self.sap_order_rows = [
            {"doc_entry": ORDER_ENTRY, "doc_num": 1026202501, "type": "S", "status": "R"},
            {"doc_entry": 900, "doc_num": 1026202400, "type": "P", "status": "L"},
        ]

    # -- reader ---------------------------------------------------------
    def product(self, code):
        if code == self.product_row["item_code"]:
            return self.product_row
        return self.other_products.get(code)

    def batch_managed_flags(self, codes):
        return {c: self.flags.get(c, False) for c in codes}

    def on_hand(self, pairs):
        return {p: self.stock.get(p, Decimal("0")) for p in pairs}

    def litres_by_warehouse(self, warehouses):
        return self.litres

    def batch_numbers_starting(self, item_code, stem):
        return [b for b in self.batch_numbers if b.startswith(stem)]

    def batch_exists(self, item_code, number):
        return number in self.batch_numbers

    def branch_of(self, warehouse):
        return 2

    def order_by_reference(self, item_code, reference):
        if self.order_row and self.order_item == item_code and self.order_row["comments"].startswith(reference):
            return {"doc_entry": ORDER_ENTRY, "doc_num": self.order_row["doc_num"],
                    "status": self.order_row["status"]}
        return None

    def order(self, doc_entry):
        if not self.order_row or doc_entry != ORDER_ENTRY:
            return None
        return {key: value for key, value in self.order_row.items() if key != "comments"}

    def issue_for(self, doc_entry):
        return self.issue

    def receipt_for(self, doc_entry):
        return self.receipt

    def sap_orders(self, **filters):
        self.sap_order_filters = filters
        return {
            "count": 2,
            "status_counts": {"R": 1, "L": 1},
            "results": [dict(row) for row in self.sap_order_rows],
        }

    # -- batch stock reader --------------------------------------------
    def available_batches(self, item_code, warehouse):
        return list(self.batches.get((item_code, warehouse), []))

    def allocate_fifo(self, item_code, warehouse, quantity):
        picks, short = services._allocate(self.available_batches(item_code, warehouse), Decimal(str(quantity)))
        if short > 0:
            from sap_client.hana.batch_stock_reader import InsufficientBatchStock

            raise InsufficientBatchStock(f"{item_code} is {short} short.")
        return [{"BatchNumber": p["batch_number"], "Quantity": float(p["quantity"])} for p in picks]

    def check_allocation(self, item_code, warehouse, batches):
        return [
            {"BatchNumber": b.get("BatchNumber") or b.get("batch_number"),
             "Quantity": float(b.get("Quantity") or b.get("quantity"))}
            for b in batches
        ]

    # -- service layer --------------------------------------------------
    def post(self, path, payload, **kwargs):
        if self.post_error:
            raise self.post_error
        self.posts.append((path, payload))
        if path == "ProductionOrders":
            self.order_item = payload["ItemNo"]
            self.order_row = {
                "doc_entry": ORDER_ENTRY,
                "doc_num": 1026202600,
                # SAP creates production orders only as planned (-10).
                "status": "P" if payload["ProductionOrderStatus"] == "boposPlanned" else "BAD",
                "comments": payload["Remarks"],
                "planned_quantity": Decimal(str(payload["PlannedQuantity"])),
                "completed_quantity": Decimal("0"),
                "lines": [
                    {"line_num": n, "visual_order": n, "item_code": line["ItemNo"],
                     "base_quantity": Decimal(str(line["BaseQuantity"])),
                     "planned_quantity": Decimal(str(line["PlannedQuantity"])),
                     "issued_quantity": Decimal("0"), "warehouse": line["Warehouse"], "item_type": 4}
                    for n, line in enumerate(payload["ProductionOrderLines"])
                ],
            }
            return {"AbsoluteEntry": ORDER_ENTRY, "DocumentNumber": 1026202600}
        if path == "InventoryGenExits":
            self.issue = {"doc_entry": 900, "doc_num": 1026606600}
            return {"DocEntry": 900, "DocNum": 1026606600}
        if path == "InventoryGenEntries":
            self.receipt = {"doc_entry": 800, "doc_num": 1026596600}
            return {"DocEntry": 800, "DocNum": 1026596600}
        raise AssertionError(path)

    def patch(self, path, payload, headers=None, **kwargs):
        if self.patch_error:
            raise self.patch_error
        if payload.get("ProductionOrderStatus") == "boposReleased" and self.release_error:
            raise self.release_error
        self.patches.append((path, payload))
        self.patch_headers.append(headers)
        status = {"boposReleased": "R", "boposClosed": "L", "boposPlanned": "P"}.get(
            payload.get("ProductionOrderStatus")
        )
        if status:
            self.order_row["status"] = status
        if "ItemNo" in payload:
            # A changed plan; the lines are replaced only when SAP is asked to.
            self.order_item = payload["ItemNo"]
            self.order_row["comments"] = payload["Remarks"]
            self.order_row["planned_quantity"] = Decimal(str(payload["PlannedQuantity"]))
            new_lines = [
                {"line_num": n, "visual_order": n, "item_code": line["ItemNo"],
                 "base_quantity": Decimal(str(line["BaseQuantity"])),
                 "planned_quantity": Decimal(str(line["PlannedQuantity"])),
                 "issued_quantity": Decimal("0"), "warehouse": line["Warehouse"], "item_type": 4}
                for n, line in enumerate(payload["ProductionOrderLines"])
            ]
            if headers == {"B1S-ReplaceCollectionsOnPatch": "true"}:
                self.order_row["lines"] = new_lines
            else:
                self.order_row["lines"] += new_lines


def patch_sap(test, sap):
    """Point every SAP touch of the app at ``sap``."""
    tree_reader = mock.Mock(get_tree=mock.Mock(side_effect=lambda code: TREES.get(code, TREE)))
    series = lambda company, object_code, day: {  # noqa: E731
        "series": {"202": 2698, "60": 2638, "59": 2626}[object_code],
        "series_name": {"202": "PRO1026", "60": "GI1026", "59": "GRE1026"}[object_code],
    }
    patches = [
        mock.patch("production_orders.services.reader_for", return_value=sap),
        mock.patch("production_orders.sap_posting.reader_for", return_value=sap),
        mock.patch("production_orders.services.HanaBOMReader", return_value=tree_reader),
        mock.patch("sap_client.hana.bom_reader.HanaBOMReader", return_value=tree_reader),
        mock.patch("production_orders.services.HanaBatchStockReader", return_value=sap),
        mock.patch("production_orders.sap_posting.HanaBatchStockReader", return_value=sap),
        mock.patch("production_orders.services.resolve_series", side_effect=series),
        mock.patch("production_orders.sap_posting.resolve_series", side_effect=series),
        mock.patch("production_orders.identity.sap_client_for", return_value=sap),
    ]
    for patcher in patches:
        patcher.start()
        test.addCleanup(patcher.stop)




def plan_data(**overrides):
    data = {"item_code": "FG0000011", "boxes": 10, "loose_pieces": 2, "posting_date": TODAY}
    data.update(overrides)
    return data


def receipt_data(**overrides):
    data = {"line_code": "L4", "oil_code": "006024", "mfg_date": MFG}
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# rules that need no database
# ---------------------------------------------------------------------------


class RuleTests(SimpleTestCase):
    def test_expiry_is_two_years_less_a_day(self):
        self.assertEqual(services.default_expiry(date(2026, 10, 8)), date(2028, 10, 7))

    def test_expiry_from_a_leap_day(self):
        self.assertEqual(services.default_expiry(date(2028, 2, 29)), date(2030, 2, 27))

    def test_batch_number_is_line_oil_code_date_and_sequence(self):
        stem = services.batch_stem("L3", "851010", date(2026, 10, 8))
        self.assertEqual(stem, "L3851010 102608")
        self.assertEqual(services.batch_number(stem, 1), "L3851010 102608 01")

    def test_allocate_takes_oldest_released_batches_first(self):
        picks, short = services._allocate(
            [
                {"batch_number": "A", "quantity": Decimal("5"), "status": "0"},
                {"batch_number": "LOCK", "quantity": Decimal("50"), "status": "1"},
                {"batch_number": "B", "quantity": Decimal("50"), "status": "0"},
            ],
            Decimal("12"),
        )
        self.assertEqual(
            [(p["batch_number"], p["quantity"]) for p in picks], [("A", Decimal("5")), ("B", Decimal("7"))]
        )
        self.assertEqual(short, 0)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

ALL_STEP_RIGHTS = (
    "can_create_production_orders", "can_release_production_orders", "can_issue_production_orders",
    "can_receive_production_orders", "can_close_production_orders",
)


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {"USER24": "secret"}})
class EntryTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.role = UserRole.objects.create(name="Staff")
        self._n = 0
        self.sap = FakeSap()
        patch_sap(self, self.sap)
        self.gautam = self.make_user("Gautam", *ALL_STEP_RIGHTS, sap_code="USER24")

    def make_user(self, name, *codenames, sap_code=None):
        self._n += 1
        user = get_user_model().objects.create_user(
            email=f"po{self._n}@example.com", password="x", full_name=name, employee_code=f"PO{self._n:03d}"
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role, is_default=True)
        if codenames:
            user.user_permissions.add(
                *Permission.objects.filter(content_type__app_label="production_orders", codename__in=codenames)
            )
        if sap_code:
            SapApproverIdentity.objects.create(user=user, company=self.company, sap_user_code=sap_code)
        return get_user_model().objects.get(pk=user.pk)

    def make_entry(self, **overrides):
        return services.create_entry(self.company, self.gautam, plan_data(**overrides))

    def post(self, entry, step, user=None):
        entry.refresh_from_db()
        return services.post_step(entry, user or self.gautam, step)

    def run_to(self, entry, last):
        """Save and post every step up to ``last``, as Gautam."""
        for step in STEPS[: STEPS.index(last) + 1]:
            entry.refresh_from_db()
            if step == Step.RECEIPT:
                services.save_receipt(entry, self.gautam, receipt_data())
            result = self.post(entry, step)
            self.assertEqual(result["outcome"], "POSTED", result)
        entry.refresh_from_db()
        return entry


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class PlanTests(EntryTestCase):
    def test_the_bom_is_scaled_to_what_was_made(self):
        built = services.plan_preview(self.company, plan_data())
        self.assertEqual(built["quantity"], Decimal("42"))  # 10 boxes of 4 + 2
        self.assertEqual(built["litres"], Decimal("210"))
        self.assertEqual(built["line_count"], 4)  # the BOM's text line is not an order line
        lines = {line["item_code"]: line for line in built["lines"]}
        self.assertEqual(lines["RM0000003"]["planned_quantity"], Decimal("210"))
        self.assertEqual(lines["PM0000075"]["base_quantity"], Decimal("0.2794"))
        self.assertEqual(lines["JWPL09240002"]["item_type"], "resource")
        self.assertEqual(built["series"], "PRO1026")
        self.assertEqual(built["warnings"], [])

    def test_plan_asks_for_no_batch_and_no_variety(self):
        entry = self.make_entry()
        self.assertEqual((entry.status, entry.variety, entry.batch_number), (EntryStatus.DRAFT, "MUSTARD", ""))
        self.assertTrue(entry.entry_no.startswith(f"PRD-{TODAY:%Y%m%d}-"))
        self.assertEqual(entry.lines.count(), 4)
        self.assertNotEqual(self.make_entry().entry_no, entry.entry_no)

    def test_input_that_cannot_be_saved_is_refused(self):
        cases = [
            (plan_data(loose_pieces=4), "full box"),
            (plan_data(boxes=0, loose_pieces=0), "how many"),
            (plan_data(posting_date=TODAY + timedelta(days=1)), "future"),
            (plan_data(item_code="FG9"), "no item"),
        ]
        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesMessage(EntryError, message):
                services.plan_preview(self.company, data)

    def test_a_cap_over_its_limit_is_a_warning_and_stops_the_post(self):
        self.sap.litres = [{"warehouse": "BH-PF", "series": 389, "group": 102, "litres": Decimal("360000")}]
        self.assertTrue(
            any("350,000 L" in w for w in services.plan_preview(self.company, plan_data())["warnings"])
        )
        result = self.post(self.make_entry(), Step.PLAN)
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertIn("350,000 L", result["message"])

    def test_other_companies_are_not_set_up(self):
        with self.assertRaisesMessage(EntryError, "Jivo Oil only"):
            services.plan_preview(self.other_company, plan_data())

    def test_the_plan_creates_a_planned_order_with_the_boms_lines(self):
        entry = self.make_entry(remarks="night shift")
        result = self.post(entry, Step.PLAN)
        self.assertEqual(result["outcome"], "POSTED", result)
        path, payload = self.sap.posts[0]
        self.assertEqual(path, "ProductionOrders")
        self.assertEqual(payload["ProductionOrderStatus"], "boposPlanned")
        self.assertEqual(payload["Series"], 2698)
        self.assertEqual(payload["CustomerCode"], "VENDA001625")
        self.assertEqual(payload["Remarks"], f"App {entry.entry_no} night shift")
        self.assertEqual([line["U_WASTAGE_QUANTITY"] for line in payload["ProductionOrderLines"]], [0] * 4)
        self.assertEqual(payload["ProductionOrderLines"][-1]["ItemType"], "pit_Resource")
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.sap_order_num, entry.planned_by), (EntryStatus.PLANNED, 1026202600, self.gautam))
        self.assertEqual(list(entry.lines.values_list("sap_line_num", flat=True)), [0, 1, 2, 3])

    def test_once_planned_in_sap_the_plan_is_fixed(self):
        entry = self.run_to(self.make_entry(), Step.PLAN)
        with self.assertRaisesMessage(EntryError, "already in SAP"):
            services.save_plan(entry, self.gautam, plan_data(boxes=99))
        with self.assertRaisesMessage(EntryError, "Only a draft"):
            services.delete_entry(entry)

    def test_an_order_sap_already_has_is_adopted_not_posted_again(self):
        entry = self.make_entry()
        self.sap.post("ProductionOrders", payloads.order_payload(entry, series=2698, card_code="VENDA001625"))
        self.sap.posts.clear()
        result = self.post(entry, Step.PLAN)
        self.assertIn("not posted twice", result["message"])
        self.assertEqual(self.sap.posts, [])

    def test_a_changed_bom_is_caught_before_sap(self):
        entry = self.make_entry()
        entry.lines.filter(item_code="PM0000053").update(base_quantity=Decimal("2"))
        result = self.post(entry, Step.PLAN)
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertIn("BOM in SAP has changed", result["message"])
        self.assertEqual(self.sap.posts, [])


# ---------------------------------------------------------------------------
# Release, Issue, Receipt, Close
# ---------------------------------------------------------------------------


class StepTests(EntryTestCase):
    def test_all_five_steps_in_order(self):
        entry = self.run_to(self.make_entry(), Step.CLOSE)
        self.assertEqual(entry.status, EntryStatus.CLOSED)
        self.assertEqual(
            (entry.sap_order_num, entry.sap_issue_num, entry.sap_receipt_num),
            (1026202600, 1026606600, 1026596600),
        )
        self.assertEqual([path for path, _ in self.sap.posts],
                         ["ProductionOrders", "InventoryGenExits", "InventoryGenEntries"])
        self.assertEqual(
            [body for _, body in self.sap.patches],
            [{"ProductionOrderStatus": "boposReleased"},
             {"ProductionOrderStatus": "boposClosed", "ClosingDate": TODAY.isoformat()}],
        )
        self.assertEqual(
            SapPosting.objects.filter(source_id=entry.pk, status=SapPostingStatus.POSTED).count(), 5
        )

    def test_steps_go_in_sap_order(self):
        entry = self.make_entry()
        with self.assertRaises(EntryError) as caught:
            self.post(entry, Step.ISSUE)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.sap.posts, [])

    def test_release_is_its_own_right(self):
        entry = self.run_to(self.make_entry(), Step.PLAN)
        planner = self.make_user("Planner", "can_create_production_orders", sap_code="USER25")
        with self.assertRaises(EntryError) as caught:
            self.post(entry, Step.RELEASE, user=planner)
        self.assertEqual(caught.exception.status, 403)

    def test_an_order_released_in_sap_already_is_recorded(self):
        entry = self.run_to(self.make_entry(), Step.PLAN)
        self.sap.order_row["status"] = "R"
        result = self.post(entry, Step.RELEASE)
        self.assertIn("already released", result["message"])
        self.assertEqual(self.sap.patches, [])

    def test_without_a_sap_login_nothing_is_sent(self):
        unmapped = self.make_user("Unmapped", *ALL_STEP_RIGHTS)
        with self.assertRaises(EntryError) as caught:
            self.post(self.make_entry(), Step.PLAN, user=unmapped)
        self.assertEqual(caught.exception.extra.get("code"), "NO_SAP_LOGIN")
        self.assertEqual(self.sap.posts, [])

    def test_issue_draws_oldest_released_batches_and_carries_the_variety(self):
        entry = self.run_to(self.make_entry(), Step.RELEASE)
        preview = services.issue_preview(entry)
        oil = preview["lines"][0]
        self.assertEqual(
            [(b["batch_number"], b["quantity"]) for b in oil["batches"]],
            [("OLD", Decimal("30")), ("NEW", Decimal("180"))],
        )
        self.assertFalse(oil["batches_chosen"])
        services.save_issue(entry, self.gautam, {"variety": "CANOLA"})
        self.assertEqual(self.post(entry, Step.ISSUE)["outcome"], "POSTED")
        line = self.sap.posts[1][1]["DocumentLines"][0]
        self.assertEqual((line["BaseLine"], line["CostingCode"], line["U_SchemeAgst"]), (0, "CANOLA", "CANOLA"))
        self.assertEqual(line["BatchNumbers"], [{"BatchNumber": "OLD", "Quantity": 30.0},
                                                {"BatchNumber": "NEW", "Quantity": 180.0}])

    def test_chosen_batches_must_add_up_and_are_used(self):
        entry = self.run_to(self.make_entry(), Step.RELEASE)
        oil = entry.lines.get(item_code="RM0000003")
        with self.assertRaisesMessage(EntryError, "add up to"):
            services.save_issue(entry, self.gautam, {"lines": [{"line_id": oil.pk, "batches": [
                {"batch_number": "NEW", "quantity": "100"}]}]})
        services.save_issue(entry, self.gautam, {"lines": [{"line_id": oil.pk, "batches": [
            {"batch_number": "NEW", "quantity": "210"}]}]})
        self.assertTrue(services.issue_preview(entry)["lines"][0]["batches_chosen"])
        self.post(entry, Step.ISSUE)
        self.assertEqual(self.sap.posts[1][1]["DocumentLines"][0]["BatchNumbers"],
                         [{"BatchNumber": "NEW", "Quantity": 210.0}])

    def test_saved_batches_that_no_longer_fit_the_stock_are_flagged(self):
        entry = self.run_to(self.make_entry(), Step.RELEASE)
        oil = entry.lines.get(item_code="RM0000003")
        services.save_issue(entry, self.gautam, {"lines": [{"line_id": oil.pk, "batches": [
            {"batch_number": "NEW", "quantity": "210"}]}]})
        self.sap.batches[("RM0000003", "BH-PC")][2]["quantity"] = Decimal("50")  # NEW was drawn on since
        warnings = services.issue_preview(entry)["warnings"]
        self.assertTrue(any("batch NEW holds 50" in w and "Choose its batches again" in w for w in warnings))

    def test_an_issue_without_a_variety_is_not_sent(self):
        entry = self.run_to(self.make_entry(), Step.RELEASE)
        services.save_issue(entry, self.gautam, {"variety": ""})
        with self.assertRaisesMessage(EntryError, "variety"):
            self.post(entry, Step.ISSUE)

    def test_receipt_preview_builds_the_batch_and_its_expiry(self):
        entry = self.run_to(self.make_entry(), Step.ISSUE)
        self.assertFalse(services.receipt_preview(entry, {})["complete"])
        self.sap.batch_numbers = [f"L4006024 {MFG:%m%y%d} 01"]
        built = services.receipt_preview(entry, receipt_data())
        self.assertEqual(built["batch_number"], f"L4006024 {MFG:%m%y%d} 02")
        self.assertEqual(built["expiry_date"], services.default_expiry(MFG))
        self.assertEqual(built["quantity"], Decimal("42"))
        with self.assertRaisesMessage(EntryError, "already used"):
            services.receipt_preview(entry, receipt_data(batch_sequence=1))

    def test_a_receipt_without_its_batch_is_not_sent(self):
        entry = self.run_to(self.make_entry(), Step.ISSUE)
        with self.assertRaisesMessage(EntryError, "batch"):
            self.post(entry, Step.RECEIPT)

    def test_the_receipt_carries_the_batch_dates_and_its_own_date(self):
        entry = self.run_to(self.make_entry(), Step.ISSUE)
        services.save_receipt(entry, self.gautam, receipt_data(receipt_date=TODAY))
        self.post(entry, Step.RECEIPT)
        payload = self.sap.posts[2][1]
        line = payload["DocumentLines"][0]
        self.assertEqual(payload["DocDate"], TODAY.isoformat())
        self.assertEqual(line["TransactionType"], "botrntComplete")
        self.assertEqual(line["Quantity"], 42.0)
        self.assertEqual(
            line["BatchNumbers"],
            [{"BatchNumber": f"L4006024 {MFG:%m%y%d} 01", "Quantity": 42.0,
              "ManufacturingDate": MFG.isoformat(),
              "ExpiryDate": services.default_expiry(MFG).isoformat()}],
        )

    def test_a_batch_already_in_sap_stops_the_receipt(self):
        entry = self.run_to(self.make_entry(), Step.ISSUE)
        services.save_receipt(entry, self.gautam, receipt_data())
        entry.refresh_from_db()
        self.sap.batch_numbers.append(entry.batch_number)
        result = self.post(entry, Step.RECEIPT)
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertIn("already exists in SAP", result["message"])

    def test_a_closing_date_before_the_order_is_refused(self):
        entry = self.run_to(self.make_entry(posting_date=TODAY), Step.RECEIPT)
        with self.assertRaisesMessage(EntryError, "before the order"):
            services.save_close(entry, self.gautam, {"close_date": TODAY - timedelta(days=3)})

    def test_sap_down_waits_and_the_worker_posts_it_later(self):
        entry = self.make_entry()
        self.sap.post_error = SAPConnectionError("Unable to connect to SAP Service Layer")
        self.assertEqual(self.post(entry, Step.PLAN)["outcome"], "WAITING")
        waiting = SapPosting.objects.get(kind="production_order.create", source_id=entry.pk)
        self.assertEqual(waiting.status, SapPostingStatus.QUEUED)
        self.sap.post_error = None
        from sap_postings import services as queue

        queue.retry(waiting.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, EntryStatus.PLANNED)

    def test_sap_refusal_is_kept_with_its_words(self):
        entry = self.make_entry()
        self.sap.post_error = SAPValidationError("(20205) Component PM0000053 Exist Quantity mismatch")
        result = self.post(entry, Step.PLAN)
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertIn("20205", result["message"])
        entry.refresh_from_db()
        self.assertEqual(entry.status, EntryStatus.DRAFT)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class ApiTests(EntryTestCase):
    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def call(self, user, method, path, body=None, company="JIVO_OIL"):
        return getattr(self.client_for(user), method)(
            f"/api/v1/production-orders/{path}", body or {}, format="json", HTTP_COMPANY_CODE=company
        )

    def plan_body(self, **overrides):
        return {**plan_data(), "posting_date": TODAY.isoformat(), **overrides}

    def test_me_says_what_the_caller_may_do(self):
        response = self.call(self.gautam, "get", "me/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["sap_login"]["ready"])
        self.assertEqual(set(response.data["rights"]), {"PLAN", "RELEASE", "ISSUE", "RECEIPT", "CLOSE"})
        self.assertTrue(all(response.data["rights"].values()))

    def test_a_viewer_reads_but_cannot_enter(self):
        viewer = self.make_user("Viewer", "can_view_production_orders")
        self.make_entry()
        self.assertEqual(self.call(viewer, "get", "entries/").data["count"], 1)
        self.assertEqual(self.call(viewer, "post", "entries/", self.plan_body()).status_code, 403)

    def test_no_right_no_access(self):
        nobody = self.make_user("Nobody")
        self.assertEqual(self.call(nobody, "get", "entries/").status_code, 403)

    def test_each_step_saves_and_posts_on_its_own_page(self):
        created = self.call(self.gautam, "post", "entries/", self.plan_body(post=True))
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["result"]["outcome"], "POSTED")
        pk = created.data["entry"]["id"]
        self.assertEqual(self.call(self.gautam, "put", f"entries/{pk}/release/", {"post": True}).data["entry"]["status"], "RELEASED")
        issue = self.call(self.gautam, "get", f"entries/{pk}/issue/")
        self.assertEqual(len(issue.data["lines"]), 4)
        self.assertEqual(issue.data["variety"], "MUSTARD")
        self.call(self.gautam, "put", f"entries/{pk}/issue/", {"post": True})
        draft = self.call(self.gautam, "put", f"entries/{pk}/receipt/",
                          {"line_code": "L4", "oil_code": "006024", "mfg_date": MFG.isoformat()})
        self.assertEqual(draft.data["result"], None)  # saved, not posted
        self.assertEqual(draft.data["entry"]["batch_number"], f"L4006024 {MFG:%m%y%d} 01")
        self.call(self.gautam, "put", f"entries/{pk}/receipt/", {"post": True})
        closed = self.call(self.gautam, "put", f"entries/{pk}/close/", {"post": True})
        self.assertEqual(closed.data["entry"]["status"], "CLOSED")
        self.assertTrue(all(step["done"] for step in closed.data["entry"]["steps"]))

    def test_receipt_preview_follows_what_is_typed(self):
        entry = self.run_to(self.make_entry(), Step.ISSUE)
        response = self.call(self.gautam, "post", f"entries/{entry.pk}/receipt/preview/",
                             {"line_code": "L1", "oil_code": "123456", "mfg_date": MFG.isoformat()})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["batch_number"], f"L1123456 {MFG:%m%y%d} 01")

    def test_plan_preview_returns_no_bom_lines(self):
        response = self.call(self.gautam, "post", "plan-preview/", self.plan_body())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["quantity"], "42.000000")
        self.assertEqual(response.data["line_count"], 4)
        self.assertNotIn("lines", response.data)

    def test_another_companys_entry_is_not_found(self):
        entry = self.make_entry()
        UserCompany.objects.create(user=self.gautam, company=self.other_company, role=self.role)
        response = self.call(self.gautam, "get", f"entries/{entry.pk}/", company="JIVO_MART")
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Changing an order already in SAP
# ---------------------------------------------------------------------------


class ChangeTests(EntryTestCase):
    def test_a_planned_order_takes_a_new_product_and_its_boms_lines(self):
        entry = self.run_to(self.make_entry(), Step.PLAN)
        result = services.request_replan(entry, self.gautam, plan_data(item_code="FG0000118", boxes=5, loose_pieces=0))
        self.assertEqual(result["outcome"], "POSTED", result)
        path, payload = self.sap.patches[-1]
        self.assertEqual(path, f"ProductionOrders({ORDER_ENTRY})")
        self.assertEqual(self.sap.patch_headers[-1], {"B1S-ReplaceCollectionsOnPatch": "true"})
        self.assertEqual((payload["ItemNo"], payload["PlannedQuantity"]), ("FG0000118", 20.0))
        self.assertEqual([line["ItemNo"] for line in payload["ProductionOrderLines"]],
                         ["RM0000002", "PM0000053", "JWPL09240001"])
        self.assertEqual([line["U_WASTAGE_QUANTITY"] for line in payload["ProductionOrderLines"]], [0, 0, 0])
        entry.refresh_from_db()
        self.assertEqual((entry.item_code, entry.quantity, entry.variety, entry.status),
                         ("FG0000118", Decimal("20"), "CANOLA", EntryStatus.PLANNED))
        self.assertEqual(list(entry.lines.values_list("item_code", "sap_line_num")),
                         [("RM0000002", 0), ("PM0000053", 1), ("JWPL09240001", 2)])
        self.assertEqual(self.sap.posts[0][0], "ProductionOrders")  # changed, not created again
        self.assertEqual(len(self.sap.posts), 1)

    def test_only_a_planned_order_can_be_changed(self):
        draft = self.make_entry()
        with self.assertRaises(EntryError) as caught:
            services.request_replan(draft, self.gautam, plan_data(boxes=11))
        self.assertEqual(caught.exception.status, 409)
        released = self.run_to(self.make_entry(), Step.RELEASE)
        with self.assertRaisesMessage(EntryError, "back to"):
            services.request_replan(released, self.gautam, plan_data(boxes=11))

    def test_a_change_that_changes_nothing_is_refused(self):
        entry = self.run_to(self.make_entry(), Step.PLAN)
        with self.assertRaisesMessage(EntryError, "Nothing has changed"):
            services.request_replan(entry, self.gautam, plan_data())

    def test_a_change_waits_for_sap_and_reaches_the_entry_only_when_sap_takes_it(self):
        entry = self.run_to(self.make_entry(), Step.PLAN)
        self.sap.patch_error = SAPConnectionError("Unable to connect to SAP Service Layer")
        result = services.request_replan(entry, self.gautam, plan_data(boxes=20))
        self.assertEqual(result["outcome"], "WAITING")
        entry.refresh_from_db()
        self.assertEqual(entry.boxes, 10)  # SAP has not taken it yet
        self.sap.patch_error = None
        from sap_postings import services as queue

        queue.retry(SapPosting.objects.get(kind="production_order.replan", source_id=entry.pk).pk)
        entry.refresh_from_db()
        self.assertEqual((entry.boxes, entry.quantity), (20, Decimal("82")))

    def test_back_to_planned_then_changed_then_released_again(self):
        entry = self.run_to(self.make_entry(), Step.RELEASE)
        self.assertEqual(services.request_unrelease(entry, self.gautam)["outcome"], "POSTED")
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.released_at), (EntryStatus.PLANNED, None))
        self.assertEqual(self.sap.order_row["status"], "P")
        services.request_replan(entry, self.gautam, plan_data(boxes=12))
        self.assertEqual(self.post(entry, Step.RELEASE)["outcome"], "POSTED")
        entry.refresh_from_db()
        self.assertEqual((entry.status, entry.quantity), (EntryStatus.RELEASED, Decimal("50")))

    def test_back_to_planned_is_refused_once_issued(self):
        entry = self.run_to(self.make_entry(), Step.ISSUE)
        with self.assertRaises(EntryError) as caught:
            services.request_unrelease(entry, self.gautam)
        self.assertEqual(caught.exception.status, 409)

    def test_back_to_planned_needs_the_release_right(self):
        entry = self.run_to(self.make_entry(), Step.RELEASE)
        issuer = self.make_user("Issuer", "can_issue_production_orders", sap_code="USER25")
        with self.assertRaises(EntryError) as caught:
            services.request_unrelease(entry, issuer)
        self.assertEqual(caught.exception.status, 403)

    def test_the_api_sends_a_planned_orders_change_straight_to_sap(self):
        entry = self.run_to(self.make_entry(), Step.PLAN)
        client = APIClient()
        client.force_authenticate(self.gautam)
        path = f"/api/v1/production-orders/entries/{entry.pk}/plan/"
        body = {**plan_data(boxes=11), "posting_date": TODAY.isoformat()}
        draft = client.put(path, body, format="json", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(draft.status_code, 400)
        sent = client.put(path, {**body, "post": True}, format="json", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(sent.data["result"]["outcome"], "POSTED")
        self.assertEqual(sent.data["entry"]["boxes"], 11)
        self.assertEqual(sent.data["entry"]["changes"]["replan"]["status"], "POSTED")


class SapOrdersTests(EntryTestCase):
    """SAP's own list of orders, read-only, each marked with the entry that made it."""

    client_for = ApiTests.client_for
    call = ApiTests.call

    def test_lists_sap_orders_and_marks_the_ones_made_here(self):
        entry = self.make_entry()
        services.entries_for(self.company).filter(pk=entry.pk).update(sap_order_entry=ORDER_ENTRY)
        viewer = self.make_user("Viewer", "can_view_production_orders")
        response = self.call(
            viewer, "get",
            "sap-orders/?status=R&type=S&date_from=2026-10-01&date_to=2026-10-10&search=canola&offset=50",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.sap.sap_order_filters, {
            "status": "R", "order_type": "S", "date_from": date(2026, 10, 1), "date_to": date(2026, 10, 10),
            "search": "canola", "limit": 50, "offset": 50,
        })
        made_here, made_in_sap = response.data["results"]
        self.assertEqual(made_here["entry"], {"id": entry.pk, "entry_no": entry.entry_no})
        self.assertEqual((made_here["type_label"], made_here["status_label"]), ("Standard", "Released"))
        self.assertIsNone(made_in_sap["entry"])
        self.assertEqual((made_in_sap["type_label"], made_in_sap["status_label"]), ("Special", "Closed"))
        self.assertEqual(response.data["status_counts"], {"R": 1, "L": 1})

    def test_refuses_filters_it_does_not_know(self):
        for query in ("status=X", "type=Z", "date_from=10-10-2026", "date_from=2026-10-10&date_to=2026-10-01"):
            response = self.call(self.gautam, "get", f"sap-orders/?{query}")
            self.assertEqual(response.status_code, 400, query)
        self.assertIsNone(self.sap.sap_order_filters)

    def test_needs_a_production_order_right_and_oil(self):
        self.assertEqual(self.call(self.make_user("Nobody"), "get", "sap-orders/").status_code, 403)
        UserCompany.objects.create(user=self.gautam, company=self.other_company, role=self.role)
        response = self.call(self.gautam, "get", "sap-orders/", company="JIVO_MART")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Jivo Oil only", response.data["detail"])
        self.assertIsNone(self.sap.sap_order_filters)
