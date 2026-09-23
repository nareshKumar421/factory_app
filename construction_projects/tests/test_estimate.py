"""The estimate's breakdown, and how big the thing is.

The sheet a site actually costs from: 700 bags of cement at 350, 111,000 bricks
at 7, and a total at the bottom. Optional — a small job carries a round number
and no detail — but when a breakdown exists it IS the estimate, because two
numbers that are supposed to agree eventually will not.
"""

from decimal import Decimal

from rest_framework import status

from construction_projects.constants import ProjectStatus

from .base import ConstructionTestCase

# Straight off the spreadsheet, so the arithmetic is checkable by eye.
SHEET = [
    {"line_no": 1, "material": "CEMENT", "quantity": "700", "unit": "BAG", "rate": "350"},
    {"line_no": 2, "material": "CORSE SAND", "quantity": "4000", "unit": "CFT", "rate": "34"},
    {"line_no": 3, "material": "BRICKS", "quantity": "111000", "unit": "NOS", "rate": "7"},
]
# 245000 + 136000 + 777000
SHEET_TOTAL = Decimal("1158000.00")


class EstimateBreakdownTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project(approved=False)

    def _put(self, lines):
        return self.client.put(
            f"/api/v1/construction/projects/{self.project.id}/estimate/",
            {"lines": lines},
            format="json",
            **self.headers,
        )

    def test_a_project_starts_with_no_breakdown(self):
        data = self.get(f"projects/{self.project.id}/estimate/").data
        self.assertEqual(data["lines"], [])
        self.assertEqual(Decimal(data["total"]), Decimal("0"))
        self.assertTrue(data["is_editable"])

    def test_each_line_amount_is_quantity_times_rate(self):
        response = self._put(SHEET)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        amounts = {row["material"]: Decimal(row["amount"]) for row in response.data["lines"]}
        self.assertEqual(amounts["CEMENT"], Decimal("245000.00"))
        self.assertEqual(amounts["CORSE SAND"], Decimal("136000.00"))
        self.assertEqual(amounts["BRICKS"], Decimal("777000.00"))

    def test_the_breakdown_becomes_the_estimate(self):
        """Not a second figure beside it — the same figure."""
        self._put(SHEET)
        self.assertEqual(
            self.refreshed(self.project).estimated_cost, SHEET_TOTAL
        )
        data = self.get(f"projects/{self.project.id}/estimate/").data
        self.assertEqual(Decimal(data["total"]), SHEET_TOTAL)
        self.assertEqual(Decimal(data["estimated_cost"]), SHEET_TOTAL)

    def test_a_typed_estimate_cannot_override_a_breakdown(self):
        self._put(SHEET)
        self.patch(f"projects/{self.project.id}/", {"estimated_cost": "5.00"})
        self.assertEqual(
            self.refreshed(self.project).estimated_cost,
            SHEET_TOTAL,
            "the breakdown is the estimate; a typed figure must not win",
        )

    def test_saving_replaces_the_whole_sheet(self):
        self._put(SHEET)
        response = self._put(
            [{"line_no": 1, "material": "STEEL 8MM", "quantity": "8000", "unit": "KG", "rate": "62"}]
        )
        self.assertEqual(len(response.data["lines"]), 1)
        self.assertEqual(Decimal(response.data["total"]), Decimal("496000.00"))
        self.assertEqual(
            self.refreshed(self.project).estimated_cost, Decimal("496000.00")
        )

    def test_a_row_with_no_quantity_is_allowed_and_totals_zero(self):
        """The real sheet has a '10MM' row with a rate and no quantity yet."""
        response = self._put(
            [{"line_no": 1, "material": "10MM", "unit": "KG", "rate": "62"}]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["lines"][0]["amount"]), Decimal("0.00"))

    def test_line_numbers_are_filled_in_when_omitted(self):
        response = self._put(
            [{"material": "CEMENT", "quantity": "1", "rate": "350"},
             {"material": "SAND", "quantity": "1", "rate": "34"}]
        )
        self.assertEqual([row["line_no"] for row in response.data["lines"]], [1, 2])

    def test_two_rows_sharing_a_line_number_are_refused(self):
        response = self._put(
            [{"line_no": 1, "material": "CEMENT", "quantity": "1", "rate": "1"},
             {"line_no": 1, "material": "SAND", "quantity": "1", "rate": "1"}]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_clearing_the_sheet_hands_the_figure_back(self):
        self._put(SHEET)
        response = self._put([])
        self.assertEqual(response.data["lines"], [])
        # The last derived figure stands until somebody types a new one.
        self.patch(f"projects/{self.project.id}/", {"estimated_cost": "900000.00"})
        self.assertEqual(
            self.refreshed(self.project).estimated_cost, Decimal("900000.00")
        )

    def test_an_approved_project_refuses_an_estimate_edit(self):
        project = self.make_project()
        response = self.client.put(
            f"/api/v1/construction/projects/{project.id}/estimate/",
            {"lines": SHEET},
            format="json",
            **self.headers,
        )
        self.assertCode(response, "project_not_editable")

    def test_editing_needs_the_edit_permission(self):
        viewer = self.make_user("est@example.com", "CON970", ["can_view_project"])
        project = self.make_project(approved=False, site_incharge=viewer)
        self.client.force_authenticate(viewer)
        response = self.client.put(
            f"/api/v1/construction/projects/{project.id}/estimate/",
            {"lines": SHEET},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_sanctioned_budget_follows_the_breakdown_through_approval(self):
        self._put(SHEET)
        self.post(f"projects/{self.project.id}/submit/")
        self.post(f"projects/{self.project.id}/approve/")
        project = self.refreshed(self.project)
        self.assertEqual(project.status, ProjectStatus.APPROVED)
        self.assertEqual(project.sanctioned_budget, SHEET_TOTAL)


class ProjectAreaTests(ConstructionTestCase):
    def test_area_and_volume_are_derived(self):
        response = self.post(
            "projects/",
            self.project_payload(length="30", breadth="20", height="12"),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(response.data["area"]), Decimal("600.00"))
        self.assertEqual(Decimal(response.data["volume"]), Decimal("7200.00"))
        self.assertEqual(response.data["area_unit"], "sq ft")
        self.assertEqual(response.data["volume_unit"], "cu ft")

    def test_metres_change_the_derived_units(self):
        response = self.post(
            "projects/",
            self.project_payload(
                length="10", breadth="5", height="3", dimension_unit="M"
            ),
        )
        self.assertEqual(Decimal(response.data["area"]), Decimal("50.00"))
        self.assertEqual(response.data["area_unit"], "sq m")
        self.assertEqual(response.data["volume_unit"], "cu m")

    def test_a_length_alone_is_allowed(self):
        """A boundary wall has a length and no meaningful breadth."""
        response = self.post("projects/", self.project_payload(length="250"))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(response.data["length"]), Decimal("250.00"))
        self.assertIsNone(response.data["area"])
        self.assertIsNone(response.data["volume"])

    def test_dimensions_are_optional_altogether(self):
        response = self.post("projects/", self.project_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data["length"])
        self.assertIsNone(response.data["area"])

    def test_area_without_a_height_still_works(self):
        response = self.post(
            "projects/", self.project_payload(length="30", breadth="20")
        )
        self.assertEqual(Decimal(response.data["area"]), Decimal("600.00"))
        self.assertIsNone(response.data["volume"])
