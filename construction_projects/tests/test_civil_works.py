"""The civil works sheet: projects, the works under them, and their dates.

Rows come straight off the site's "JIVO CIVIL PROJECTS 2026" sheet, so the
arithmetic can be checked against it by eye.
"""

from datetime import date, timedelta
from decimal import Decimal

from rest_framework import status

from company.models import Company
from construction_projects.models import CivilWork

from .base import ALL_PERMISSIONS, ConstructionTestCase

CIVIL = ["can_view_civil_works", "can_edit_civil_works"]


class CivilWorksTestCase(ConstructionTestCase):
    permissions = ALL_PERMISSIONS + CIVIL

    def add(self, name, parent=None, **fields):
        payload = {"name": name, **fields}
        if parent is not None:
            payload["parent"] = parent
        response = self.post("civil-works/", payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def sheet(self):
        response = self.get("civil-works/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data


class SheetShapeTests(CivilWorksTestCase):
    def test_an_empty_sheet(self):
        self.assertEqual(self.sheet(), [])

    def test_works_sit_under_their_project_in_the_order_added(self):
        shed = self.add("40K SHED WORK")
        self.add("SOIL LAYERS", parent=shed["id"])
        self.add("WBM AND GSB LABEL", parent=shed["id"])
        self.add("OUTER SIDE WORK")

        sheet = self.sheet()
        self.assertEqual([row["name"] for row in sheet], ["40K SHED WORK", "OUTER SIDE WORK"])
        self.assertEqual(
            [work["name"] for work in sheet[0]["works"]],
            ["SOIL LAYERS", "WBM AND GSB LABEL"],
        )
        self.assertEqual(sheet[1]["works"], [])

    def test_a_work_cannot_have_works_of_its_own(self):
        shed = self.add("40K SHED WORK")
        soil = self.add("SOIL LAYERS", parent=shed["id"])
        response = self.post("civil-works/", {"name": "Layer 1", "parent": soil["id"]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertCode(response, "civil_work_too_deep")

    def test_another_companys_project_is_not_a_parent(self):
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        theirs = CivilWork.objects.create(company=other, name="Their shed")
        response = self.post("civil-works/", {"name": "Soil", "parent": theirs.id})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_only_this_companys_sheet_is_listed(self):
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        CivilWork.objects.create(company=other, name="Their shed")
        self.add("40K SHED WORK")
        self.assertEqual([row["name"] for row in self.sheet()], ["40K SHED WORK"])


class AreaAndDaysTests(CivilWorksTestCase):
    def test_days_are_end_minus_start_and_per_day_is_area_over_days(self):
        # BRICK WORK: 8,700 sq ft, 30 Sep to 31 Oct -- the sheet's 31 days, 281 a day.
        row = self.add(
            "BRICK WORK",
            area="8700",
            start_date="2026-09-30",
            end_date="2026-10-31",
        )
        self.assertEqual(row["days"], 31)
        self.assertEqual(Decimal(row["per_day"]), Decimal("280.65"))
        self.assertEqual(row["area_unit"], "SQFT")

    def test_typed_days_hold_while_there_are_no_dates(self):
        # DOCK SLAB: "60 days when it starts".
        row = self.add("DOCK SLAB CASTING WORK", area="2800", days=60)
        self.assertEqual(row["days"], 60)
        self.assertEqual(Decimal(row["per_day"]), Decimal("46.67"))

    def test_dates_overrule_typed_days(self):
        row = self.add(
            "ROOF SHEETING",
            area="40000",
            days=99,
            start_date="2026-10-02",
            end_date="2026-10-15",
        )
        self.assertEqual(row["days"], 13)

    def test_a_one_day_job_is_one_day(self):
        row = self.add("BASEMENT CUT OUT", area="110", start_date="2026-10-10", end_date="2026-10-10")
        self.assertEqual(row["days"], 1)
        self.assertEqual(Decimal(row["per_day"]), Decimal("110.00"))

    def test_no_area_means_no_per_day(self):
        row = self.add("SHUTTER", days=11)
        self.assertIsNone(row["per_day"])

    def test_it_cannot_finish_before_it_starts(self):
        response = self.post(
            "civil-works/",
            {"name": "WBM", "start_date": "2026-10-14", "end_date": "2026-09-28"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertCode(response, "end_before_start")

    def test_moving_a_date_recounts_the_days(self):
        row = self.add("PLASTER WORK", area="23000", start_date="2026-10-05", end_date="2026-10-31")
        self.assertEqual(row["days"], 26)
        response = self.patch(f"civil-works/{row['id']}/", {"start_date": "2026-10-15"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["days"], 16)

    def test_an_edit_keeps_what_it_did_not_send(self):
        row = self.add("SOIL LAYERS", area="40500", contractor="DEEPAK JAIN")
        response = self.patch(
            f"civil-works/{row['id']}/",
            {"status": "COMPLETE", "stage": "7th layer"},
        )
        self.assertEqual(response.data["status"], "COMPLETE")
        self.assertEqual(response.data["stage"], "7th layer")
        self.assertEqual(response.data["contractor"], "DEEPAK JAIN")
        self.assertEqual(Decimal(response.data["area"]), Decimal("40500.00"))


class LateTests(CivilWorksTestCase):
    def test_past_its_end_and_not_complete_is_late(self):
        row = self.add(
            "STRUCTURE SHED",
            status="IN_PROGRESS",
            start_date=str(self.today - timedelta(days=20)),
            end_date=str(self.today - timedelta(days=2)),
        )
        self.assertTrue(row["is_late"])

        done = self.patch(f"civil-works/{row['id']}/", {"status": "COMPLETE"}).data
        self.assertFalse(done["is_late"])

    def test_past_its_start_and_not_begun_is_a_late_start(self):
        row = self.add(
            "TRIMAX FLOORING",
            start_date=str(self.today - timedelta(days=1)),
            end_date=str(self.today + timedelta(days=14)),
        )
        self.assertTrue(row["is_late_start"])
        self.assertFalse(row["is_late"])

        begun = self.patch(f"civil-works/{row['id']}/", {"status": "IN_PROGRESS"}).data
        self.assertFalse(begun["is_late_start"])


class OrderAndRemovalTests(CivilWorksTestCase):
    def test_move_up_and_down_among_siblings(self):
        shed = self.add("40K SHED WORK")
        a = self.add("A", parent=shed["id"])
        b = self.add("B", parent=shed["id"])
        c = self.add("C", parent=shed["id"])

        self.post(f"civil-works/{c['id']}/move/", {"direction": "up"})
        self.assertEqual([w["name"] for w in self.sheet()[0]["works"]], ["A", "C", "B"])

        self.post(f"civil-works/{a['id']}/move/", {"direction": "down"})
        self.assertEqual([w["name"] for w in self.sheet()[0]["works"]], ["C", "A", "B"])

        # Already at the bottom: stays put.
        response = self.post(f"civil-works/{b['id']}/move/", {"direction": "down"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([w["name"] for w in self.sheet()[0]["works"]], ["C", "A", "B"])

    def test_projects_move_among_projects(self):
        self.add("40K SHED WORK")
        outer = self.add("OUTER SIDE WORK")
        self.post(f"civil-works/{outer['id']}/move/", {"direction": "up"})
        self.assertEqual(
            [row["name"] for row in self.sheet()], ["OUTER SIDE WORK", "40K SHED WORK"]
        )

    def test_removing_a_project_takes_its_works_with_it(self):
        shed = self.add("40K SHED WORK")
        soil = self.add("SOIL LAYERS", parent=shed["id"])
        self.add("OUTER SIDE WORK")

        response = self.delete(f"civil-works/{shed['id']}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual([row["name"] for row in self.sheet()], ["OUTER SIDE WORK"])
        # Kept for the record, not deleted.
        self.assertFalse(CivilWork.objects.get(pk=soil["id"]).is_active)

    def test_removing_a_work_leaves_the_rest(self):
        shed = self.add("40K SHED WORK")
        soil = self.add("SOIL LAYERS", parent=shed["id"])
        self.add("BRICK WORK", parent=shed["id"])
        self.delete(f"civil-works/{soil['id']}/")
        self.assertEqual([w["name"] for w in self.sheet()[0]["works"]], ["BRICK WORK"])

    def test_a_new_row_goes_after_the_last_one_still_on_the_sheet(self):
        first = self.add("First")
        self.add("Second")
        self.delete(f"civil-works/{first['id']}/")
        self.add("Third")
        self.assertEqual([row["name"] for row in self.sheet()], ["Second", "Third"])


class CivilPermissionTests(ConstructionTestCase):
    permissions = ["can_view_civil_works"]

    def test_viewing_needs_its_own_right(self):
        viewer = self.make_user("nobody@example.com", "CON009", permissions=["can_view_project"])
        self.client.force_authenticate(viewer)
        response = self.get("civil-works/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_viewer_cannot_change_the_sheet(self):
        self.assertEqual(self.get("civil-works/").status_code, status.HTTP_200_OK)
        response = self.post("civil-works/", {"name": "40K SHED WORK"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        work = CivilWork.objects.create(company=self.company, name="Shed", start_date=date(2026, 9, 17))
        self.assertEqual(
            self.patch(f"civil-works/{work.id}/", {"name": "x"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.delete(f"civil-works/{work.id}/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.post(f"civil-works/{work.id}/move/", {"direction": "up"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )
