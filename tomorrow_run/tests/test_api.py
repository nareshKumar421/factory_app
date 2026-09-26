import io
import json
import shutil
import tempfile
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from tomorrow_run import services
from tomorrow_run.models import MachinePick, PlanningSheet, TomorrowPlan

FIXTURE = Path(__file__).parent / "fixtures" / "inputs_2026-09-24.json"
MEDIA = tempfile.mkdtemp(prefix="tomorrow_run_tests_")


def _xlsx():
    wb = Workbook()
    ws = wb.active
    ws.title = "P"
    ws.append(["PRODUCTION PLANING MONTH OF SEP 2026"])
    ws.append(["CODE", "ITEM NAME", "PLAN", "ECOM", "STOCK", "NET REQ", "MACHINE"])
    ws.append(["FG0000030", "MUSTARD KACHI GHANI 1 LTR 20 PCS", 150000, 70000, 128971, 91029, "JP"])
    ws.append(["FG0000004", "COLD PRESS 5 LTR 4 PCS", 150000, 40000, 1830, 188170, "Clearpack 5"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@override_settings(MEDIA_ROOT=MEDIA)
class TomorrowRunAPITests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        User = get_user_model()
        self.company = Company.objects.create(code="JIVO_OIL", name="Oil")
        self.user = User.objects.create_user(email="veerji@test.com", password="pw123456", full_name="Gurvinder veerji")
        UserCompany.objects.create(user=self.user, company=self.company,
                                   role=UserRole.objects.create(name="Staff"), is_active=True)
        self.client = APIClient()
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")
        self.client.force_authenticate(user=self.user)
        self.plan = TomorrowPlan.objects.create(
            company=self.company, for_date=date(2026, 9, 24), read_at=timezone.now(),
            inputs=json.loads(FIXTURE.read_text()),
        )
        services.recompute(self.plan)

    def grant(self, *codenames):
        self.user.user_permissions.set(
            Permission.objects.filter(content_type__app_label="tomorrow_run", codename__in=codenames))
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client.force_authenticate(user=self.user)

    def choose(self, **body):
        return self.client.post("/api/v1/tomorrow-run/choice/", {"for_date": "2026-09-24", **body}, format="json")

    # -- seeing it --------------------------------------------------------

    def test_the_page_needs_a_right(self):
        self.assertEqual(self.client.get("/api/v1/tomorrow-run/plan/").status_code, 403)

    def test_the_page_shows_the_plan(self):
        self.grant("can_view_tomorrow_run")
        r = self.client.get("/api/v1/tomorrow-run/plan/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["plan"]["total_l"], 135564.0)
        self.assertEqual(body["plan"]["machine_order"], ["JP", "Clear Pack", "10 Head", "6 Head", "Tin",
                                                         "Hitech pouch", "Samarpan pouch"])
        self.assertEqual(set(body["plan"]["machines"]), set(body["plan"]["machine_order"]))
        self.assertFalse(body["meta"]["can_pick"])
        self.assertEqual(body["meta"]["you"], "Gurvinder veerji")

    def test_the_right_to_pick_also_opens_the_page(self):
        self.grant("can_pick_tomorrow_run")
        self.assertEqual(self.client.get("/api/v1/tomorrow-run/plan/").status_code, 200)

    # -- picking ----------------------------------------------------------

    def test_seeing_is_not_picking(self):
        self.grant("can_view_tomorrow_run")
        self.assertEqual(self.choose(machine="10 Head", job="FG0000090-order", why="x").status_code, 403)

    def test_a_pick_is_kept_with_what_we_offered_and_the_day_is_re_timed(self):
        self.grant("can_pick_tomorrow_run")
        r = self.choose(machine="10 Head", job="FG0000090-order", why="Saves a changeover — same bottle")
        self.assertEqual(r.status_code, 200, r.content)
        plan = r.json()["plan"]
        self.assertEqual(plan["machines"]["10 Head"]["jobs"][0]["code"], "FG0000090")
        pick = MachinePick.objects.get()
        self.assertEqual((pick.rank, pick.picked_by_name, pick.on_plan), (2, "Gurvinder veerji", True))
        self.assertEqual([t["job"] for t in pick.top3], ["FG0000091-order", "FG0000090-order", "FG0000306-order"])
        self.assertEqual(plan["learning"]["n"], 1)
        self.assertEqual(plan["machine_menu"]["10 Head"]["picked"]["rank"], 2)

    def test_a_pick_says_why(self):
        self.grant("can_pick_tomorrow_run")
        self.assertEqual(self.choose(machine="10 Head", job="FG0000090-order", why="  ").status_code, 400)
        self.assertFalse(MachinePick.objects.exists())

    def test_going_back_to_our_suggestion(self):
        self.grant("can_pick_tomorrow_run")
        self.choose(machine="10 Head", job="FG0000090-order", why="x")
        r = self.choose(machine="10 Head", job="", why="")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plan"]["machines"]["10 Head"]["jobs"][0]["code"], "FG0000091")
        self.assertIsNotNone(MachinePick.objects.get().cleared_at)
        self.assertEqual(r.json()["plan"]["learning"]["n"], 0)

    def test_a_second_pick_on_a_machine_replaces_the_first(self):
        self.grant("can_pick_tomorrow_run")
        self.choose(machine="10 Head", job="FG0000090-order", why="x")
        self.choose(machine="10 Head", job="FG0000306-order", why="y")
        self.assertEqual(MachinePick.objects.filter(cleared_at__isnull=True).get().job, "FG0000306-order")

    def test_something_that_cannot_be_made_cannot_be_picked(self):
        self.grant("can_pick_tomorrow_run")
        r = self.choose(machine="JP", job="FG0000189-order", why="x")    # no labels: none of it exists
        self.assertEqual(r.status_code, 400)

    def test_typing_something_else(self):
        self.grant("can_pick_tomorrow_run")
        self.assertEqual(self.choose(machine="Tin", job="other", why="Urgent order", other="").status_code, 400)
        r = self.choose(machine="Tin", job="other", why="Urgent order", other="Mustard 15 L")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plan"]["machine_menu"]["Tin"]["picked"]["other"], "Mustard 15 L")

    def test_a_page_left_open_on_an_old_plan(self):
        self.grant("can_pick_tomorrow_run")
        r = self.client.post("/api/v1/tomorrow-run/choice/",
                             {"for_date": "2026-09-23", "machine": "JP", "job": "FG0000030-order", "why": "x"},
                             format="json")
        self.assertEqual(r.status_code, 409)

    # -- the planning sheet ---------------------------------------------------

    def test_putting_in_a_sheet_needs_the_right(self):
        self.grant("can_view_tomorrow_run")
        f = SimpleUploadedFile("PLANNING FOR SEP 2026 19.09.2026.xlsx", _xlsx())
        self.assertEqual(self.client.post("/api/v1/tomorrow-run/sheets/", {"file": f}, format="multipart").status_code, 403)

    def test_putting_in_a_sheet(self):
        self.grant("can_manage_tomorrow_run")
        f = SimpleUploadedFile("PLANNING FOR SEP 2026 19.09.2026.xlsx", _xlsx())
        r = self.client.post("/api/v1/tomorrow-run/sheets/", {"file": f}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual((body["stock_date"], body["line_count"], body["tab"]), ("2026-09-19", 2, "P"))
        self.assertEqual(body["date_basis"], "the date in its file name")
        self.assertTrue(body["file_url"].startswith("http"))
        listed = self.client.get("/api/v1/tomorrow-run/sheets/").json()["results"]
        self.assertTrue(listed[0]["in_charge"])

    def test_a_sheet_with_no_date_in_its_name_asks_for_one(self):
        self.grant("can_manage_tomorrow_run")
        r = self.client.post("/api/v1/tomorrow-run/sheets/",
                             {"file": SimpleUploadedFile("planning.xlsx", _xlsx())}, format="multipart")
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/v1/tomorrow-run/sheets/",
                             {"file": SimpleUploadedFile("planning.xlsx", _xlsx()), "stock_date": "2026-09-21"},
                             format="multipart")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(PlanningSheet.objects.get().date_basis, "entered when it was put in")

    def test_a_file_that_is_not_the_sheet(self):
        self.grant("can_manage_tomorrow_run")
        r = self.client.post("/api/v1/tomorrow-run/sheets/",
                             {"file": SimpleUploadedFile("notes 19.09.2026.xlsx", b"hello")}, format="multipart")
        self.assertEqual(r.status_code, 400)
