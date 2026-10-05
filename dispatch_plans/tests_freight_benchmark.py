"""
The freight benchmark table: its one cross-row rule, its API's rights, and the
workbook import.

The import is tested on a small workbook built in the shapes the real one has --
a transporter block after the benchmark on DELHI NCR, transporter columns beside
the benchmark ones, merged district and state cells, a PIN column whose header
is blank -- because each of those, read wrongly, produces a plausible benchmark
rather than an error: a transporter's quote filed as the company's rate, or a
town filed under the wrong district.
"""

import os
import tempfile
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from .freight_benchmark_import import apply_workbook, parse_workbook
from .freight_benchmark_service import (
    FreightBenchmarkError,
    delete_slab,
    save_destination,
    save_slab,
)
from .models_freight_benchmark import (
    FreightBenchmark,
    FreightDestination,
    FreightRateBasis,
    FreightSlab,
)

User = get_user_model()


def _slabs():
    return {
        "5": FreightSlab.objects.create(label="5 MT", above_kg=0, up_to_kg=5000, sort_order=5),
        "10": FreightSlab.objects.create(
            label="10 MT", above_kg=5000, up_to_kg=10000, sort_order=10
        ),
        "15": FreightSlab.objects.create(
            label="15 MT", above_kg=10000, up_to_kg=15000, sort_order=15
        ),
        "kg8": FreightSlab.objects.create(
            label="5,001-8,000 kg", above_kg=5000, up_to_kg=8000, sort_order=1080
        ),
    }


def _rate(slab, amount, basis=FreightRateBasis.PER_TRIP):
    return {"slab": slab, "basis": basis, "amount": Decimal(str(amount))}


class SaveDestinationTests(TestCase):
    def setUp(self):
        self.slabs = _slabs()

    def test_a_destination_is_saved_with_its_rates_in_the_workbooks_spelling(self):
        destination = save_destination(
            data={
                "state": "punjab",
                "district": " ludhiana ",
                "name": "khanna",
                "pin_code": "141401",
                "distance_km": 213,
                "rates": [_rate(self.slabs["10"], 13500), _rate(self.slabs["15"], 20250)],
            }
        )

        self.assertEqual((destination.state, destination.district, destination.name),
                         ("PUNJAB", "LUDHIANA", "KHANNA"))
        self.assertEqual(
            {b.slab.label: b.amount for b in destination.benchmarks.all()},
            {"10 MT": Decimal("13500"), "15 MT": Decimal("20250")},
        )

    def test_a_slab_left_out_of_the_list_loses_its_rate(self):
        # The edit form clears a box to remove a rate; the list is the whole truth.
        destination = save_destination(
            data={
                "state": "PUNJAB",
                "name": "KHANNA",
                "rates": [_rate(self.slabs["10"], 13500), _rate(self.slabs["15"], 20250)],
            }
        )
        save_destination(
            destination=destination,
            data={"state": "PUNJAB", "name": "KHANNA", "rates": [_rate(self.slabs["10"], 14000)]},
        )

        self.assertEqual(
            list(destination.benchmarks.values_list("slab__label", "amount")),
            [("10 MT", Decimal("14000.00"))],
        )

    def test_rates_on_two_overlapping_slabs_are_refused(self):
        # 10 MT is 5,001-10,000 kg and so is part of 5,001-8,000 kg: a 7 T truck
        # would have two benchmarks.
        with self.assertRaisesMessage(FreightBenchmarkError, "overlap"):
            save_destination(
                data={
                    "state": "DELHI NCR",
                    "name": "DELHI",
                    "rates": [
                        _rate(self.slabs["10"], 9000),
                        _rate(self.slabs["kg8"], 1, FreightRateBasis.PER_KG),
                    ],
                }
            )
        self.assertFalse(FreightDestination.objects.exists())

    def test_the_same_place_twice_in_one_state_is_refused(self):
        save_destination(data={"state": "PUNJAB", "name": "KHANNA", "rates": []})
        with self.assertRaisesMessage(FreightBenchmarkError, "already listed"):
            save_destination(data={"state": "Punjab", "name": "Khanna", "rates": []})

    def test_the_same_name_in_another_state_is_a_different_place(self):
        save_destination(data={"state": "PUNJAB", "name": "JAIPUR", "rates": []})
        save_destination(data={"state": "RAJASTHAN", "name": "JAIPUR", "rates": []})
        self.assertEqual(FreightDestination.objects.count(), 2)


class SaveSlabTests(TestCase):
    def setUp(self):
        self.slabs = _slabs()
        save_destination(
            data={
                "state": "PUNJAB",
                "name": "KHANNA",
                "rates": [_rate(self.slabs["5"], 9000), _rate(self.slabs["10"], 13500)],
            }
        )

    def test_widening_a_band_into_a_neighbour_a_destination_rates_is_refused(self):
        with self.assertRaisesMessage(FreightBenchmarkError, "KHANNA"):
            save_slab(
                slab=self.slabs["10"],
                data={"label": "10 MT", "above_kg": 4000, "up_to_kg": 10000},
            )
        self.slabs["10"].refresh_from_db()
        self.assertEqual(self.slabs["10"].above_kg, 5000)

    def test_a_band_change_nobody_collides_with_is_saved(self):
        save_slab(
            slab=self.slabs["15"],
            data={"label": "16 MT", "above_kg": 10000, "up_to_kg": 16000, "sort_order": 16},
        )
        self.slabs["15"].refresh_from_db()
        self.assertEqual((self.slabs["15"].label, self.slabs["15"].up_to_kg), ("16 MT", 16000))

    def test_an_empty_band_is_refused(self):
        with self.assertRaises(FreightBenchmarkError):
            save_slab(data={"label": "0 MT", "above_kg": 5000, "up_to_kg": 5000})

    def test_a_slab_carrying_rates_cannot_be_deleted(self):
        with self.assertRaisesMessage(FreightBenchmarkError, "1 destination has a rate"):
            delete_slab(self.slabs["10"])
        delete_slab(self.slabs["15"])
        self.assertFalse(FreightSlab.objects.filter(label="15 MT").exists())


class FreightBenchmarkAPITests(TestCase):
    URL = "/api/v1/dispatch/freight-benchmarks/"

    def setUp(self):
        self.slabs = _slabs()
        save_destination(
            data={
                "state": "DELHI NCR",
                "name": "DELHI",
                "rates": [_rate(self.slabs["kg8"], "1.20", FreightRateBasis.PER_KG)],
            }
        )

    def _client(self, *codenames):
        user = User.objects.create_user(
            email=f"{'-'.join(codenames) or 'nobody'}@example.com",
            password="x",
            full_name="Desk",
        )
        for codename in codenames:
            user.user_permissions.add(Permission.objects.get(codename=codename))
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_the_table_reads_without_a_company_and_sends_amounts_as_numbers(self):
        response = self._client("can_view_freight_benchmarks").get(self.URL)

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual([s["label"] for s in body["slabs"]],
                         ["5 MT", "10 MT", "15 MT", "5,001-8,000 kg"])
        self.assertEqual(
            {s["label"]: s["destination_count"] for s in body["slabs"]}["5,001-8,000 kg"], 1
        )
        (delhi,) = body["destinations"]
        self.assertEqual(
            delhi["rates"],
            [{"slab": self.slabs["kg8"].pk, "basis": "PER_KG", "amount": 1.2}],
        )

    def test_nobody_without_the_right_reads_it(self):
        self.assertEqual(self._client().get(self.URL).status_code, 403)
        self.assertEqual(APIClient().get(self.URL).status_code, 401)

    def test_the_linking_desk_reads_it_to_pick_a_destination_but_cannot_edit(self):
        desk = self._client("can_link_dispatch_vehicle")
        self.assertEqual(desk.get(self.URL).status_code, 200)
        response = desk.post(
            self.URL + "destinations/", {"state": "PUNJAB", "name": "MOGA"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_a_viewer_cannot_write_and_a_manager_can(self):
        payload = {
            "state": "Punjab",
            "name": "Ludhiana",
            "pin_code": "141001",
            "rates": [{"slab": self.slabs["10"].pk, "amount": "13000"}],
        }
        url = self.URL + "destinations/"
        viewer = self._client("can_view_freight_benchmarks")
        self.assertEqual(viewer.post(url, payload, format="json").status_code, 403)

        manager = self._client("can_manage_freight_benchmarks")
        self.assertEqual(manager.get(self.URL).status_code, 200)
        response = manager.post(url, payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["name"], "LUDHIANA")
        self.assertEqual(response.json()["updated_by_name"], "Desk")

    def test_a_refusal_comes_back_as_a_sentence(self):
        manager = self._client("can_manage_freight_benchmarks")
        response = manager.post(
            self.URL + "destinations/",
            {
                "state": "DELHI NCR",
                "name": "Delhi",
                "rates": [],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "DELHI is already listed under DELHI NCR.")

    def test_a_pin_that_is_not_six_digits_is_refused(self):
        manager = self._client("can_manage_freight_benchmarks")
        response = manager.post(
            self.URL + "destinations/",
            {"state": "PUNJAB", "name": "MOGA", "pin_code": "1420", "rates": []},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("pin_code", response.json())

    def test_put_replaces_and_delete_removes(self):
        manager = self._client("can_manage_freight_benchmarks")
        delhi = FreightDestination.objects.get(name="DELHI")
        url = f"{self.URL}destinations/{delhi.pk}/"

        response = manager.put(
            url,
            {
                "state": "DELHI NCR",
                "name": "DELHI",
                "rates": [{"slab": self.slabs["5"].pk, "amount": "5500"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [(r["slab"], r["amount"]) for r in response.json()["rates"]],
            [(self.slabs["5"].pk, 5500.0)],
        )

        self.assertEqual(manager.delete(url).status_code, 204)
        self.assertFalse(FreightBenchmark.objects.exists())

    def test_slabs_are_added_and_a_used_one_is_not_deleted(self):
        manager = self._client("can_manage_freight_benchmarks")
        response = manager.post(
            self.URL + "slabs/",
            {"label": "32 MT", "above_kg": 24000, "up_to_kg": 32000, "sort_order": 32},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["destination_count"], 0)

        used = manager.delete(f"{self.URL}slabs/{self.slabs['kg8'].pk}/")
        self.assertEqual(used.status_code, 400)
        self.assertIn("Clear those rates first", used.json()["detail"])


def _workbook(path):
    import openpyxl

    wb = openpyxl.Workbook()
    ncr = wb.active
    ncr.title = "DELHI NCR"
    for row in [
        ["Destination", "UP TO 2000 kg", "2100 -2500 kg", "2501-3500 kg", "3501-5000 kg",
         "5001-8000 kg", "8001-10000 kg"],
        ["DELHI", 3500, 4000, 4500, 5500, "1/KG", 9000],
        ["NCR-GURUGRAM/NOIDA", 4000, 4500, 5500, 6500, "1.20/KG", 10000],
        ["BHARGAVE"],
        ["Destination", "UP TO 2000 kg", "2100 -2500 kg", "2501-3000 kg", "3001-5000 kg",
         "5001-8000 kg", "8001-10000 kg"],
        ["ALL DELHI", None, None, 4500, None, None, 9500],
    ]:
        ncr.append(row)

    punjab = wb.create_sheet("PUNJAB")
    for row in [
        ["DISTR", "Destination", "PIN CODE", "5 MT", "10 MT", "15 MT", "PER MT RATE", "KM",
         "DELHI PUNJAB 10MT"],
        ["LUDHIANA", "KHANNA", 141401, None, 13500, 20250, 1.35, 213, 13000],
        [None, "DORAHA", 141421, None, 13000, 19500, 1.3, 230, 13000],
        ["RUPNAGAR (ROPER", "MORINDA", 140101, None, 15000, 18000, 1.2, 205, 15000],
    ]:
        punjab.append(row)
    punjab.merge_cells("A2:A3")

    haryana = wb.create_sheet("HARYANA")
    for row in [
        ["HARYANA"],
        ["STATE", "Destination", "PIN CODE", "5 MT", "10 MT", "MAHAVIR 10MT"],
        ["HARYANA", "PANIPAT", 132103, 4000, 6500, 6500],
        ["UP"],
        ["STATE", "Destination", None, "5 MT", "10 MT", "MAHAVIR 10MT"],
        ["UTTAR PARDESH", "AGRA", 282001, 16000, 20000, None],
        ["HIMCHAL PARDESH", "BARU SAHIB", 173101, 22000, 26000, None],
        [None, "UNA", 174303, None, None, 21000],
    ]:
        haryana.append(row)
    haryana.merge_cells("A1:E1")
    haryana.merge_cells("A7:A8")
    wb.save(path)


class WorkbookImportTests(TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".xlsx")
        os.close(handle)
        _workbook(self.path)
        self.addCleanup(os.remove, self.path)

    def _by_name(self):
        return {d.name: d for d in parse_workbook(self.path).destinations}

    def test_only_the_benchmark_is_read_and_the_transporters_are_left_out(self):
        parsed = parse_workbook(self.path)
        names = [d.name for d in parsed.destinations]

        # The BHARGAVE block and every transporter column stay out.
        self.assertNotIn("ALL DELHI", names)
        self.assertEqual(len(parsed.skipped_blocks), 1)
        self.assertIn("BHARGAVE", parsed.skipped_blocks[0])
        khanna = self._by_name()["KHANNA"]
        self.assertEqual(
            {r.slab.label: r.amount for r in khanna.rates},
            {"10 MT": Decimal("13500"), "15 MT": Decimal("20250")},
        )
        self.assertEqual(parsed.problems, [])

    def test_delhis_kg_bands_join_up_and_a_per_kg_cell_is_a_per_kg_rate(self):
        delhi = self._by_name()["DELHI"]

        self.assertEqual(
            [(r.slab.label, r.slab.above_kg, r.slab.up_to_kg) for r in delhi.rates],
            [
                ("Up to 2,000 kg", 0, 2000),
                # The workbook's "2100 -2500": nothing else covers 2,001-2,099.
                ("2,001-2,500 kg", 2000, 2500),
                ("2,501-3,500 kg", 2500, 3500),
                ("3,501-5,000 kg", 3500, 5000),
                ("5,001-8,000 kg", 5000, 8000),
                ("8,001-10,000 kg", 8000, 10000),
            ],
        )
        per_kg = {r.slab.label: (r.basis, r.amount) for r in delhi.rates}
        self.assertEqual(per_kg["5,001-8,000 kg"], (FreightRateBasis.PER_KG, Decimal("1")))
        self.assertEqual(per_kg["8,001-10,000 kg"], (FreightRateBasis.PER_TRIP, Decimal("9000")))

    def test_mt_bands_open_where_the_next_smaller_size_closes(self):
        agra = self._by_name()["AGRA"]
        self.assertEqual(
            [(r.slab.label, r.slab.above_kg, r.slab.up_to_kg) for r in agra.rates],
            [("5 MT", 0, 5000), ("10 MT", 5000, 10000)],
        )

    def test_merged_cells_carry_their_district_and_state_down(self):
        by_name = self._by_name()
        self.assertEqual(by_name["DORAHA"].district, "LUDHIANA")
        self.assertEqual(by_name["MORINDA"].district, "RUPNAGAR (ROPER)")
        self.assertEqual(by_name["UNA"].state, "HIMACHAL PRADESH")
        self.assertEqual(by_name["AGRA"].state, "UTTAR PRADESH")
        self.assertEqual(by_name["KHANNA"].state, "PUNJAB")
        self.assertEqual(by_name["DELHI"].state, "DELHI NCR")

    def test_a_pin_under_a_blank_header_is_still_read(self):
        self.assertEqual(self._by_name()["AGRA"].pin_code, "282001")
        self.assertEqual(self._by_name()["KHANNA"].distance_km, 213)

    def test_a_place_with_only_transporter_rates_comes_in_with_none(self):
        self.assertEqual(self._by_name()["UNA"].rates, [])

    def test_importing_twice_changes_nothing_the_second_time(self):
        first = apply_workbook(parse_workbook(self.path))
        second = apply_workbook(parse_workbook(self.path))

        self.assertEqual(first.destinations_created, 9)
        self.assertEqual(second.destinations_created, 0)
        self.assertEqual(second.destinations_updated, 0)
        self.assertEqual(second.destinations_unchanged, 9)
        self.assertEqual(second.slabs_created, [])

    def test_the_workbook_replaces_a_places_rates_and_leaves_other_places_alone(self):
        apply_workbook(parse_workbook(self.path))
        khanna = FreightDestination.objects.get(name="KHANNA")
        five = FreightSlab.objects.get(label="5 MT")
        FreightBenchmark.objects.create(destination=khanna, slab=five, amount=Decimal("9000"))
        FreightBenchmark.objects.filter(destination=khanna, slab__label="10 MT").update(
            amount=Decimal("99999")
        )
        FreightDestination.objects.create(state="GOA", name="GOA")

        result = apply_workbook(parse_workbook(self.path))

        self.assertEqual(
            dict(khanna.benchmarks.values_list("slab__label", "amount")),
            {"10 MT": Decimal("13500.00"), "15 MT": Decimal("20250.00")},
        )
        self.assertEqual((result.rates_changed, result.rates_removed), (1, 1))
        self.assertEqual(result.not_in_workbook, ["GOA (GOA)"])

    def test_a_band_corrected_on_the_page_survives_a_reimport(self):
        apply_workbook(parse_workbook(self.path))
        FreightSlab.objects.filter(label="5 MT").update(up_to_kg=6000)
        apply_workbook(parse_workbook(self.path))
        self.assertEqual(FreightSlab.objects.get(label="5 MT").up_to_kg, 6000)

    def test_the_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("import_freight_benchmarks", self.path, "--dry-run", stdout=out)

        self.assertIn("9 would be created", out.getvalue())
        self.assertFalse(FreightDestination.objects.exists())
        self.assertFalse(FreightSlab.objects.exists())
