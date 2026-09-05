"""
Tests for the department ownership chart.

What is worth pinning down here is the save: it is a whole-chart replace, so the
things that can go wrong are rows losing their identity when they only moved,
deletions nobody asked for, and a rename that collides with another row. Plus
the two permissions — the chart is readable widely and editable narrowly, and
those must not blur.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import skipUnlessDBFeature
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .models import OrgDepartment, OrgFunction

User = get_user_model()

URL = "/api/v1/org-chart/chart/"


def _user(*codenames):
    """A user holding the given ``org_chart`` permissions."""
    count = User.objects.count()
    user = User.objects.create_user(
        email=f"org{count}@t.com",
        password="x",
        full_name=f"Org User {count}",
        employee_code=f"O{count}",
    )
    user.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="org_chart", codename__in=codenames
        )
    )
    return User.objects.get(pk=user.pk)


def _client(*codenames):
    client = APIClient()
    client.force_authenticate(user=_user(*codenames))
    return client


def _chart_payload(response):
    """The GET body reshaped into what a PUT expects back."""
    return {
        "departments": [
            {
                "id": department["id"],
                "name": department["name"],
                "functions": [
                    {
                        "id": function["id"],
                        "name": function["name"],
                        "owners": function["owners"],
                        "level_1": function["level_1"],
                        "level_2": function["level_2"],
                    }
                    for function in department["functions"]
                ],
            }
            for department in response.data["departments"]
        ]
    }


class SeedTests(APITestCase):
    def test_seed_loads_the_wall_chart(self):
        call_command("seed_org_chart")

        self.assertEqual(OrgDepartment.objects.count(), 7)
        self.assertEqual(OrgFunction.objects.count(), 17)

        production = OrgDepartment.objects.get(name="Production")
        docking = production.functions.get(name="Dispatch – Docking")
        self.assertEqual(docking.owners, ["Sandeep Veerji"])
        self.assertEqual(docking.level_1, ["Virender Veerji"])

        # A department with no sub-divisions carries one blank-named row.
        qc = OrgDepartment.objects.get(name="Quality Control")
        self.assertEqual([f.name for f in qc.functions.all()], [""])

        # "In & Out" genuinely has nobody behind the owner — no placeholder.
        in_out = production.functions.get(name="In & Out")
        self.assertEqual(in_out.level_1, [])
        self.assertEqual(in_out.level_2, [])

    def test_seed_refuses_to_overwrite_an_edited_chart(self):
        OrgDepartment.objects.create(name="Only Mine", sort_order=0)

        call_command("seed_org_chart")
        self.assertEqual(OrgDepartment.objects.count(), 1)

        call_command("seed_org_chart", replace=True)
        self.assertFalse(OrgDepartment.objects.filter(name="Only Mine").exists())
        self.assertEqual(OrgDepartment.objects.count(), 7)


class ReadTests(APITestCase):
    def setUp(self):
        call_command("seed_org_chart")

    def test_chart_comes_back_in_chart_order(self):
        response = _client("can_view_org_chart").get(URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [d["name"] for d in response.data["departments"]]
        self.assertEqual(names[0], "Purchasing")
        self.assertEqual(names[-1], "Accounts & HR")
        self.assertFalse(response.data["can_manage"])

    def test_editor_is_told_it_may_edit(self):
        response = _client("can_view_org_chart", "can_manage_org_chart").get(URL)
        self.assertTrue(response.data["can_manage"])

    def test_manage_alone_can_still_read(self):
        response = _client("can_manage_org_chart").get(URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_without_a_permission_the_chart_is_closed(self):
        self.assertEqual(_client().get(URL).status_code, status.HTTP_403_FORBIDDEN)


class SaveTests(APITestCase):
    def setUp(self):
        call_command("seed_org_chart")
        self.client = _client("can_view_org_chart", "can_manage_org_chart")

    def _current(self):
        return _chart_payload(self.client.get(URL))

    def test_a_viewer_cannot_save(self):
        payload = self._current()
        response = _client("can_view_org_chart").put(URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_edit_keeps_the_rows_it_did_not_touch(self):
        payload = self._current()
        before = OrgFunction.objects.count()
        it_block = next(d for d in payload["departments"] if d["name"] == "IT")
        software = next(f for f in it_block["functions"] if f["name"] == "Software")
        software["level_1"] = ["Team", "Nikhil"]

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(OrgFunction.objects.count(), before)
        self.assertEqual(
            OrgFunction.objects.get(pk=software["id"]).level_1, ["Team", "Nikhil"]
        )

    def test_reorder_moves_rows_without_recreating_them(self):
        payload = self._current()
        it_block = next(d for d in payload["departments"] if d["name"] == "IT")
        it_block["functions"].reverse()
        ids_before = [f["id"] for f in it_block["functions"]]

        response = self.client.put(URL, payload, format="json")

        saved = next(d for d in response.data["departments"] if d["name"] == "IT")
        self.assertEqual([f["id"] for f in saved["functions"]], ids_before)
        self.assertEqual(
            [f["name"] for f in saved["functions"]], ["Hardware", "Software"]
        )

    @skipUnlessDBFeature("supports_deferrable_unique_constraints")
    def test_swapping_two_names_in_one_save_is_allowed(self):
        payload = self._current()
        it_block = next(d for d in payload["departments"] if d["name"] == "IT")
        software = next(f for f in it_block["functions"] if f["name"] == "Software")
        hardware = next(f for f in it_block["functions"] if f["name"] == "Hardware")
        software["name"], hardware["name"] = "Hardware", "Software"

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(OrgFunction.objects.get(pk=software["id"]).name, "Hardware")

    def test_adding_a_department_and_dropping_a_row(self):
        payload = self._current()
        production = next(
            d for d in payload["departments"] if d["name"] == "Production"
        )
        dropped = next(f for f in production["functions"] if f["name"] == "In & Out")
        production["functions"] = [
            f for f in production["functions"] if f["id"] != dropped["id"]
        ]
        payload["departments"].append(
            {
                "name": "Exports",
                "functions": [
                    {"name": "Documentation", "owners": ["Neha"], "level_1": ["Team"]}
                ],
            }
        )

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(OrgFunction.objects.filter(pk=dropped["id"]).exists())
        exports = OrgDepartment.objects.get(name="Exports")
        self.assertEqual(exports.functions.get().owners, ["Neha"])
        self.assertEqual(exports.functions.get().level_2, [])
        # Appended last on the chart.
        self.assertEqual(response.data["departments"][-1]["name"], "Exports")

    def test_deleting_a_department_takes_its_rows_with_it(self):
        payload = self._current()
        it_block = next(d for d in payload["departments"] if d["name"] == "IT")
        payload["departments"] = [d for d in payload["departments"] if d["name"] != "IT"]

        self.client.put(URL, payload, format="json")

        self.assertFalse(OrgDepartment.objects.filter(name="IT").exists())
        self.assertFalse(
            OrgFunction.objects.filter(
                pk__in=[f["id"] for f in it_block["functions"]]
            ).exists()
        )

    def test_names_are_tidied_and_repeats_dropped(self):
        payload = self._current()
        it_block = next(d for d in payload["departments"] if d["name"] == "IT")
        software = next(f for f in it_block["functions"] if f["name"] == "Software")
        software["owners"] = ["  Jashan  ", "jashan", "", "   ", "Sumit"]

        self.client.put(URL, payload, format="json")

        self.assertEqual(
            OrgFunction.objects.get(pk=software["id"]).owners, ["Jashan", "Sumit"]
        )

    def test_two_departments_cannot_share_a_name(self):
        payload = self._current()
        payload["departments"].append({"name": "it", "functions": []})

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(OrgDepartment.objects.count(), 7)

    def test_two_rows_in_one_department_cannot_share_a_name(self):
        payload = self._current()
        it_block = next(d for d in payload["departments"] if d["name"] == "IT")
        it_block["functions"].append({"name": "software", "owners": ["X"]})

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_row_deleted_elsewhere_is_reported_not_recreated(self):
        payload = self._current()
        it_block = next(d for d in payload["departments"] if d["name"] == "IT")
        software = next(f for f in it_block["functions"] if f["name"] == "Software")
        OrgFunction.objects.filter(pk=software["id"]).delete()

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Nothing else was touched by the refused save.
        self.assertEqual(OrgDepartment.objects.count(), 7)

    def test_a_department_needs_a_name(self):
        payload = self._current()
        payload["departments"][0]["name"] = "   "

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
