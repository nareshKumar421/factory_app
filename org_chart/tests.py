"""
Tests for the department ownership chart.

What is worth pinning down here is the save: it is a whole-chart replace, so the
things that can go wrong are rows losing their identity when they only moved,
deletions nobody asked for, and a rename that collides with another row. Plus
the two permissions — the chart is readable widely and editable narrowly, and
those must not blur. And the chart is per company, so the other thing worth
proving is that Oil's chart and Mart's cannot see or overwrite each other.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import skipUnlessDBFeature
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from .models import OrgChartSettings, OrgDepartment, OrgFunction

User = get_user_model()

URL = "/api/v1/org-chart/chart/"

OIL = "JIVO_OIL"
MART = "JIVO_MART"

#: The block the save tests work in — it carries two rows of one section told
#: apart only by their second line, which is the shape most likely to break.
SUPPORT = "Parallel / supportive"


def _companies():
    """The two companies the tests work across, created once per test."""
    oil, _ = Company.objects.get_or_create(code=OIL, defaults={"name": "Jivo Oil"})
    mart, _ = Company.objects.get_or_create(code=MART, defaults={"name": "Jivo Mart"})
    return oil, mart


def _user(*codenames):
    """A user holding the given ``org_chart`` permissions, in both companies."""
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
    role, _ = UserRole.objects.get_or_create(name="Admin")
    for company in _companies():
        UserCompany.objects.create(user=user, company=company, role=role)
    return User.objects.get(pk=user.pk)


def _client(*codenames, company=OIL):
    """A client already carrying the ``Company-Code`` header the API needs."""
    client = APIClient(headers={"Company-Code": company})
    client.force_authenticate(user=_user(*codenames))
    return client


def _chart_payload(response):
    """The GET body reshaped into what a PUT expects back."""
    return {
        "plant_name": response.data["plant_name"],
        "plant_head": response.data["plant_head"],
        "departments": [
            {
                "id": department["id"],
                "name": department["name"],
                "head": department["head"],
                "functions": [
                    {
                        "id": function["id"],
                        "name": function["name"],
                        "subtitle": function["subtitle"],
                        "owners": function["owners"],
                        "level_1": function["level_1"],
                        "level_2": function["level_2"],
                    }
                    for function in department["functions"]
                ],
            }
            for department in response.data["departments"]
        ],
    }


class SeedTests(APITestCase):
    def setUp(self):
        self.oil, self.mart = _companies()

    def test_seed_loads_the_wall_chart(self):
        call_command("seed_org_chart")

        self.assertEqual(OrgDepartment.objects.count(), 6)
        self.assertEqual(OrgFunction.objects.count(), 18)

        # Everything landed on Oil, and Mart was not touched.
        self.assertEqual(OrgDepartment.objects.filter(company=self.mart).count(), 0)

        heading = OrgChartSettings.load(self.oil)
        self.assertEqual(heading.plant_name, "Oil Plant")
        self.assertEqual(heading.plant_head, "Gagan Veerji")

        supplies = OrgDepartment.objects.get(company=self.oil, name="Supplies")
        self.assertEqual(supplies.head, "Sandeep Veerji")
        docking = supplies.functions.get(name="Despatch", subtitle="Docking")
        self.assertEqual(docking.owners, ["Sandeep Veerji"])
        self.assertEqual(docking.level_1, ["Virender Veerji"])

        # One section, twice, told apart by its second line and its people.
        production = OrgDepartment.objects.get(company=self.oil, name="Production")
        storage = list(production.functions.filter(name="Storage").order_by("sort_order"))
        self.assertEqual([row.subtitle for row in storage], ["OIL", "Packing material"])
        self.assertEqual(
            [row.owners for row in storage], [["Vicky Veerji"], ["Kulbir Veerji"]]
        )

        # A department the chart puts no single person over says so with a blank.
        self.assertEqual(
            OrgDepartment.objects.get(company=self.oil, name="Planning").head, ""
        )

        # "In-Out" genuinely has nobody behind the leader — no placeholder.
        in_out = OrgDepartment.objects.get(
            company=self.oil, name=SUPPORT
        ).functions.get(name="In-Out")
        self.assertEqual(in_out.level_1, [])
        self.assertEqual(in_out.level_2, [])

    def test_seed_refuses_to_overwrite_an_edited_chart(self):
        OrgDepartment.objects.create(company=self.oil, name="Only Mine", sort_order=0)

        call_command("seed_org_chart")
        self.assertEqual(OrgDepartment.objects.count(), 1)

        call_command("seed_org_chart", replace=True)
        self.assertFalse(OrgDepartment.objects.filter(name="Only Mine").exists())
        self.assertEqual(OrgDepartment.objects.count(), 6)

    def test_seeding_one_company_leaves_the_others_alone(self):
        call_command("seed_org_chart")
        call_command("seed_org_chart", company=MART)

        self.assertEqual(OrgDepartment.objects.filter(company=self.oil).count(), 6)
        self.assertEqual(OrgDepartment.objects.filter(company=self.mart).count(), 6)

        # Re-seeding Mart with --replace must not disturb Oil's rows.
        oil_ids = set(
            OrgDepartment.objects.filter(company=self.oil).values_list("pk", flat=True)
        )
        call_command("seed_org_chart", company=MART, replace=True)
        self.assertEqual(
            set(
                OrgDepartment.objects.filter(company=self.oil).values_list(
                    "pk", flat=True
                )
            ),
            oil_ids,
        )

    def test_an_unknown_company_is_refused(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("seed_org_chart", company="NOPE")


class ReadTests(APITestCase):
    def setUp(self):
        self.oil, self.mart = _companies()
        call_command("seed_org_chart")

    def test_chart_comes_back_in_chart_order(self):
        response = _client("can_view_org_chart").get(URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [d["name"] for d in response.data["departments"]]
        self.assertEqual(names[0], "Procurement")
        self.assertEqual(names[-1], "Planning")
        self.assertFalse(response.data["can_manage"])

    def test_the_heading_comes_back_with_the_chart(self):
        response = _client("can_view_org_chart").get(URL)

        self.assertEqual(response.data["plant_name"], "Oil Plant")
        self.assertEqual(response.data["plant_head"], "Gagan Veerji")
        self.assertEqual(response.data["departments"][0]["head"], "Shunty Veerji")

    def test_editor_is_told_it_may_edit(self):
        response = _client("can_view_org_chart", "can_manage_org_chart").get(URL)
        self.assertTrue(response.data["can_manage"])

    def test_manage_alone_can_still_read(self):
        response = _client("can_manage_org_chart").get(URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_without_a_permission_the_chart_is_closed(self):
        self.assertEqual(_client().get(URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_company_with_no_chart_yet_gets_an_empty_one_named_after_itself(self):
        response = _client("can_view_org_chart", company=MART).get(URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["departments"], [])
        # Not "Oil Plant" — a fresh chart is titled after its own company.
        self.assertEqual(response.data["plant_name"], "Jivo Mart")
        self.assertEqual(response.data["plant_head"], "")

    def test_each_company_sees_only_its_own_chart(self):
        call_command("seed_org_chart", company=MART)
        mart = OrgDepartment.objects.get(company=self.mart, name="Planning")
        mart.name = "Mart Planning"
        mart.save(update_fields=["name"])

        oil_names = [
            d["name"]
            for d in _client("can_view_org_chart", company=OIL).get(URL).data[
                "departments"
            ]
        ]
        mart_names = [
            d["name"]
            for d in _client("can_view_org_chart", company=MART).get(URL).data[
                "departments"
            ]
        ]

        self.assertIn("Planning", oil_names)
        self.assertNotIn("Mart Planning", oil_names)
        self.assertIn("Mart Planning", mart_names)

    def test_the_company_header_is_required(self):
        client = APIClient()
        client.force_authenticate(user=_user("can_view_org_chart"))

        self.assertEqual(client.get(URL).status_code, status.HTTP_403_FORBIDDEN)


class SaveTests(APITestCase):
    def setUp(self):
        self.oil, self.mart = _companies()
        call_command("seed_org_chart")
        self.client = _client("can_view_org_chart", "can_manage_org_chart")

    def _current(self):
        return _chart_payload(self.client.get(URL))

    def _support(self, payload):
        return next(d for d in payload["departments"] if d["name"] == SUPPORT)

    @staticmethod
    def _row(department, name, subtitle=""):
        return next(
            f
            for f in department["functions"]
            if f["name"] == name and f["subtitle"] == subtitle
        )

    def test_a_viewer_cannot_save(self):
        payload = self._current()
        response = _client("can_view_org_chart").put(URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_edit_keeps_the_rows_it_did_not_touch(self):
        payload = self._current()
        before = OrgFunction.objects.count()
        software = self._row(self._support(payload), "IT", "Software")
        software["level_2"] = ["Team", "Nikhil"]

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(OrgFunction.objects.count(), before)
        self.assertEqual(
            OrgFunction.objects.get(pk=software["id"]).level_2, ["Team", "Nikhil"]
        )

    def test_the_heading_can_be_changed(self):
        payload = self._current()
        payload["plant_head"] = "  Gagan   Veerji  "
        payload["plant_name"] = "Oil Plant II"

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["plant_head"], "Gagan Veerji")
        self.assertEqual(OrgChartSettings.load(self.oil).plant_name, "Oil Plant II")

    def test_a_client_that_omits_the_heading_does_not_blank_it(self):
        payload = self._current()
        payload.pop("plant_name")
        payload.pop("plant_head")

        self.client.put(URL, payload, format="json")

        heading = OrgChartSettings.load(self.oil)
        self.assertEqual(heading.plant_name, "Oil Plant")
        self.assertEqual(heading.plant_head, "Gagan Veerji")

    def test_a_department_head_can_be_changed(self):
        payload = self._current()
        procurement = next(
            d for d in payload["departments"] if d["name"] == "Procurement"
        )
        procurement["head"] = "Ravinder Veerji"

        self.client.put(URL, payload, format="json")

        self.assertEqual(
            OrgDepartment.objects.get(company=self.oil, name="Procurement").head,
            "Ravinder Veerji",
        )

    def test_reorder_moves_rows_without_recreating_them(self):
        payload = self._current()
        support = self._support(payload)
        support["functions"].reverse()
        ids_before = [f["id"] for f in support["functions"]]

        response = self.client.put(URL, payload, format="json")

        saved = next(d for d in response.data["departments"] if d["name"] == SUPPORT)
        self.assertEqual([f["id"] for f in saved["functions"]], ids_before)
        self.assertEqual(saved["functions"][0]["name"], "In-Out")

    @skipUnlessDBFeature("supports_deferrable_unique_constraints")
    def test_swapping_two_names_in_one_save_is_allowed(self):
        payload = self._current()
        support = self._support(payload)
        software = self._row(support, "IT", "Software")
        hardware = self._row(support, "IT", "Hardware")
        software["subtitle"], hardware["subtitle"] = "Hardware", "Software"

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(OrgFunction.objects.get(pk=software["id"]).subtitle, "Hardware")

    def test_adding_a_department_and_dropping_a_row(self):
        payload = self._current()
        support = self._support(payload)
        dropped = self._row(support, "In-Out")
        support["functions"] = [
            f for f in support["functions"] if f["id"] != dropped["id"]
        ]
        payload["departments"].append(
            {
                "name": "Exports",
                "head": "Neha",
                "functions": [
                    {"name": "Documentation", "owners": ["Neha"], "level_1": ["Team"]}
                ],
            }
        )

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(OrgFunction.objects.filter(pk=dropped["id"]).exists())
        exports = OrgDepartment.objects.get(company=self.oil, name="Exports")
        self.assertEqual(exports.head, "Neha")
        self.assertEqual(exports.functions.get().owners, ["Neha"])
        self.assertEqual(exports.functions.get().subtitle, "")
        self.assertEqual(exports.functions.get().level_2, [])
        # Appended last on the chart.
        self.assertEqual(response.data["departments"][-1]["name"], "Exports")

    def test_deleting_a_department_takes_its_rows_with_it(self):
        payload = self._current()
        support = self._support(payload)
        payload["departments"] = [
            d for d in payload["departments"] if d["name"] != SUPPORT
        ]

        self.client.put(URL, payload, format="json")

        self.assertFalse(OrgDepartment.objects.filter(name=SUPPORT).exists())
        self.assertFalse(
            OrgFunction.objects.filter(
                pk__in=[f["id"] for f in support["functions"]]
            ).exists()
        )

    def test_names_are_tidied_and_repeats_dropped(self):
        payload = self._current()
        software = self._row(self._support(payload), "IT", "Software")
        software["owners"] = ["  Jashan  ", "jashan", "", "   ", "Sumit"]

        self.client.put(URL, payload, format="json")

        self.assertEqual(
            OrgFunction.objects.get(pk=software["id"]).owners, ["Jashan", "Sumit"]
        )

    def test_two_departments_cannot_share_a_name(self):
        payload = self._current()
        payload["departments"].append({"name": "planning", "functions": []})

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(OrgDepartment.objects.filter(company=self.oil).count(), 6)

    def test_two_rows_in_one_department_cannot_share_a_section_and_subtitle(self):
        payload = self._current()
        self._support(payload)["functions"].append(
            {"name": "it", "subtitle": "software", "owners": ["X"]}
        )

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_one_section_may_repeat_under_a_different_second_line(self):
        payload = self._current()
        self._support(payload)["functions"].append(
            {"name": "IT", "subtitle": "Networking", "owners": ["Sumit"]}
        )

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            OrgDepartment.objects.get(company=self.oil, name=SUPPORT)
            .functions.filter(name="IT")
            .count(),
            3,
        )

    def test_a_row_deleted_elsewhere_is_reported_not_recreated(self):
        payload = self._current()
        software = self._row(self._support(payload), "IT", "Software")
        OrgFunction.objects.filter(pk=software["id"]).delete()

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Nothing else was touched by the refused save.
        self.assertEqual(OrgDepartment.objects.filter(company=self.oil).count(), 6)

    def test_a_save_cannot_reach_into_another_company_chart(self):
        call_command("seed_org_chart", company=MART)
        mart_ids = set(
            OrgDepartment.objects.filter(company=self.mart).values_list("pk", flat=True)
        )

        # Oil's editor sends Oil's whole chart. Mart's rows are absent from the
        # payload, and absent must not mean "delete".
        self.client.put(URL, self._current(), format="json")

        self.assertEqual(
            set(
                OrgDepartment.objects.filter(company=self.mart).values_list(
                    "pk", flat=True
                )
            ),
            mart_ids,
        )

    def test_a_row_id_from_another_company_is_refused(self):
        call_command("seed_org_chart", company=MART)
        stolen = OrgDepartment.objects.filter(company=self.mart).first()
        payload = self._current()
        payload["departments"][0]["id"] = stolen.pk

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        stolen.refresh_from_db()
        self.assertEqual(stolen.company_id, self.mart.pk)

    def test_two_companies_may_each_have_a_production_block(self):
        call_command("seed_org_chart", company=MART)

        self.assertEqual(OrgDepartment.objects.filter(name="Production").count(), 2)

    def test_a_department_needs_a_name(self):
        payload = self._current()
        payload["departments"][0]["name"] = "   "

        response = self.client.put(URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
