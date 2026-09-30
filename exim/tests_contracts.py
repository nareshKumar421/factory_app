"""Oil contracts, read live from SAP and the gate.

The promises worth pinning down:
  - a contract is every line of one oil on a PO, at the last line's rate:
    purchase splits a line as trucks arrive and the split pieces carry an
    adjusted price;
  - a GRPO's weighed quantity is what was unloaded and its value what was
    billed, so the loaded quantity is value over the contract rate;
  - the landed cost is EXIM's DC sheet to the paisa: (basic + freight +
    brokerage) over the unloaded tonnes, per litre at 1,098.9 L the tonne;
  - a truck the gate has booked and no GRPO has taken in shows as at the gate;
    one a GRPO took in shows once, as that GRPO, with its gate entry named;
  - the terms are the one thing kept here, and EXIM's are copied sheet first.
"""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from grpo.models import GRPOPosting, GRPOStatus
from raw_material_gatein.models import POItemReceipt, POReceipt
from vehicle_management.models import Transporter, Vehicle, VehicleType
from weighment.models import Weighment

from . import services_contract as sc
from .contract_import import import_contract_terms
from .models_contract import ContractTerms
from .services_licence import EximError

D = Decimal
BASE = "/api/v1/exim/"


def po_line(**over):
    line = {
        "doc_entry": 13301, "po_number": "220726109", "po_date": date(2026, 7, 20),
        "vendor_code": "VENDA000614", "vendor_name": "DHANLAXMI EDIBLES PRIVATE LIMITED",
        "line": 0, "item_code": "RM0000066", "item_name": "PEANUT OIL", "unit": "MTS",
        "quantity": D("84"), "open_qty": D("0"), "rate": D("166000"), "value": D("13944000"),
        "closed": True, "warehouse": "BH-GJ",
    }
    line.update(over)
    return line


def grpo(**over):
    row = {
        "grpo_number": "2026076856", "grpo_date": date(2026, 7, 26), "invoice_no": "GT/1014",
        "vehicle_number": "RJ47GA8216", "transporter": "RK TANKER SERVICE", "bilty_number": "8812",
        "po_doc_entry": 13301, "po_line": 0,
        # EXIM's DC sheet: loaded 41.045 MT at 166,000, weighed in at 40.76.
        "quantity": D("40.76"), "value": D("41.045") * D("166000"),
    }
    row.update(over)
    return row


class SapMixin:
    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def sap(self, lines, grpos=()):
        return mock.patch.multiple(
            "exim.hana_reader",
            oil_po_lines=mock.Mock(return_value=list(lines)),
            oil_grpo_lines=mock.Mock(return_value=list(grpos)),
        )


class LandedCostTests(SapMixin, TestCase):
    def test_an_exw_truck_costs_what_exims_dc_sheet_said(self):
        ContractTerms.objects.create(company=self.company, po_number="220726109", delivery_terms="EXW",
                                     freight_per_mt=D("4000"))
        with self.sap([po_line()], [grpo()]):
            [contract] = sc.contract(self.company, "220726109")["lines"]
        [load] = contract["loads"]
        self.assertEqual((load["loaded"], load["unloaded"]), (D("41.045"), D("40.760")))
        self.assertEqual(load["freight"], D("163040.00"))
        self.assertEqual(load["landed_per_mt"], D("171160.70"))  # the sheet: 171160.696762
        self.assertEqual(load["landed_per_litre"], D("155.756"))  # the sheet: 155.756390
        # 0.285 MT short, 0.103 allowed: 0.182 MT at the contract rate.
        self.assertEqual(load["deduction_amount"], D("30276.33"))

    def test_brokerage_is_on_the_loaded_tonnes(self):
        ContractTerms.objects.create(company=self.company, po_number="220726109", delivery_terms="EXW",
                                     freight_per_mt=D("4000"), brokerage_per_mt=D("50"))
        with self.sap([po_line()], [grpo()]):
            [load] = sc.contract(self.company, "220726109")["lines"][0]["loads"]
        self.assertEqual(load["brokerage"], D("2052.25"))  # 41.045 x 50

    def test_without_terms_the_landed_cost_is_the_bill_over_the_unloaded(self):
        with self.sap([po_line()], [grpo()]):
            [load] = sc.contract(self.company, "220726109")["lines"][0]["loads"]
        self.assertEqual(load["freight"], D("0.00"))
        self.assertEqual(load["landed_per_mt"], D("167160.70"))

    def test_split_lines_are_one_contract_at_the_last_lines_rate(self):
        lines = [
            po_line(line=0, quantity=D("21.315"), rate=D("148381.8907"), value=D("3162760")),
            po_line(line=1, quantity=D("218.685"), rate=D("148000"), value=D("32365380")),
        ]
        grpos = [
            grpo(po_line=0, quantity=D("21.315"), value=D("3162760"), grpo_number="1"),
            grpo(po_line=1, quantity=D("38.8"), value=D("5758680"), grpo_number="2"),
        ]
        with self.sap(lines, grpos):
            [contract] = sc.contract(self.company, "220726109")["lines"]
        self.assertEqual((contract["quantity"], contract["rate"], contract["value"]),
                         (D("240.000"), D("148000"), D("35520000.00")))
        self.assertEqual(contract["po_lines"], [0, 1])
        self.assertEqual([load["loaded"] for load in contract["loads"]], [D("21.370"), D("38.910")])

    def test_a_contract_in_pieces_has_no_cost_per_tonne(self):
        tins = po_line(unit="PCS", quantity=D("160"), rate=D("3619.05"), value=D("579048"))
        with self.sap([tins], [grpo(quantity=D("160"), value=D("579048"))]):
            [contract] = sc.contract(self.company, "220726109")["lines"]
        self.assertIsNone(contract["landed_per_mt"])
        self.assertEqual(contract["landed_per_unit"], D("3619.05"))

    def test_the_register_totals_in_tonnes(self):
        lines = [po_line(), po_line(doc_entry=2, po_number="220726110", closed=False, open_qty=D("43"))]
        with self.sap(lines, [grpo()]):
            data = sc.contracts(self.company, year=2026)
        totals = data["totals"]
        self.assertEqual((totals["contracts"], totals["open"]), (2, 1))
        self.assertEqual(totals["contracted_mt"], D("168.000"))
        self.assertEqual(totals["received_mt"], D("40.760"))
        self.assertEqual(totals["to_come_mt"], D("43.000"))

    def test_the_year_is_april_to_march(self):
        with self.sap([]):
            data = sc.contracts(self.company, year=2026)
        self.assertEqual((data["from"], data["to"]), (date(2026, 4, 1), date(2027, 3, 31)))

    def test_sap_down_says_so(self):
        from sap_client.exceptions import SAPConnectionError

        with mock.patch("exim.hana_reader.oil_po_lines", side_effect=SAPConnectionError("down")):
            with self.assertRaises(EximError) as caught:
                sc.contracts(self.company, year=2026)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "sap_unavailable")


class GateTests(SapMixin, TestCase):
    def setUp(self):
        super().setUp()
        transporter = Transporter.objects.create(name="RK TANKER SERVICE")
        self.vehicle = Vehicle.objects.create(vehicle_number="RJ47GB1956", vehicle_type=VehicleType.objects.create(
            name="TANKER"), transporter=transporter)
        self.driver = Driver.objects.create(name="D", mobile_no="9876543210", license_no="DL1")

    def entry(self, no, *, gross=None, tare=None, status=GateEntryStatus.QC_PENDING, billed="40.855"):
        entry = VehicleEntry.objects.create(entry_no=no, company=self.company, vehicle=self.vehicle,
                                            driver=self.driver, entry_type="RAW_MATERIAL", status=status)
        receipt = POReceipt.objects.create(vehicle_entry=entry, po_number="220726109", supplier_code="VENDA000614",
                                           supplier_name="DHANLAXMI", sap_doc_entry=13301, invoice_no="GT/1100")
        POItemReceipt.objects.create(po_receipt=receipt, po_item_code="RM0000066", item_name="PEANUT OIL",
                                     ordered_qty=D("84"), received_qty=D(billed), accepted_qty=D("0"),
                                     uom="MTS", unit_price=D("166000"))
        if gross:
            Weighment.objects.create(vehicle_entry=entry, gross_weight=D(gross), tare_weight=D(tare) if tare else None)
        return entry, receipt

    def test_a_truck_booked_at_the_gate_shows_until_a_grpo_takes_it_in(self):
        self.entry("GE-1", gross="57980", tare="17100")
        with self.sap([po_line(closed=False, open_qty=D("84"))]):
            [contract] = sc.contract(self.company, "220726109")["lines"]
        [truck] = contract["at_gate_loads"]
        self.assertEqual((truck["entry_no"], truck["vehicle_number"], truck["transporter"]),
                         ("GE-1", "RJ47GB1956", "RK TANKER SERVICE"))
        self.assertEqual((truck["billed"], truck["weighed_kg"]), (D("40.855"), D("40880.000")))
        self.assertEqual((contract["trucks_at_gate"], contract["at_gate"]), (1, D("40.855")))
        self.assertEqual(contract["to_come"], D("43.145"))
        self.assertEqual(contract["stage"], "ARRIVING")

    def test_a_truck_a_grpo_took_in_shows_once_with_its_gate_entry(self):
        entry, receipt = self.entry("GE-2", status=GateEntryStatus.COMPLETED)
        GRPOPosting.objects.create(vehicle_entry=entry, po_receipt=receipt, status=GRPOStatus.POSTED,
                                   sap_doc_num=2026076856)
        with self.sap([po_line()], [grpo(vehicle_number="")]):
            [contract] = sc.contract(self.company, "220726109")["lines"]
        self.assertEqual(contract["at_gate_loads"], [])
        [load] = contract["loads"]
        self.assertEqual((load["gate_entry"], load["vehicle_number"]), ("GE-2", "RJ47GB1956"))

    def test_a_cancelled_gate_entry_is_not_a_truck(self):
        self.entry("GE-3", status=GateEntryStatus.CANCELLED)
        with self.sap([po_line(closed=False, open_qty=D("84"))]):
            [contract] = sc.contract(self.company, "220726109")["lines"]
        self.assertEqual(contract["at_gate_loads"], [])


class TermsTests(SapMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(email="t@example.com", password="x", full_name="T",
                                                         employee_code="T1")

    def set(self, **kw):
        values = {"delivery_terms": "EXW", "freight_per_mt": D("4000"), "brokerage_per_mt": D("0")}
        values.update(kw)
        with self.sap([po_line()]):
            return sc.set_terms(self.company, "220726109", user=self.user, **values)

    def test_terms_are_kept_per_po(self):
        terms = self.set()
        self.assertEqual((terms.po_number, terms.delivery_terms, terms.freight_per_mt), ("220726109", "EXW", D("4000")))
        self.assertEqual(self.set(freight_per_mt=D("3500")).pk, terms.pk)

    def test_a_for_contract_carries_no_freight(self):
        with self.assertRaises(EximError) as caught:
            self.set(delivery_terms="FOR")
        self.assertEqual(caught.exception.detail["code"], "freight_on_for")

    def test_terms_need_a_po_sap_has(self):
        with mock.patch("exim.hana_reader.oil_po_lines", return_value=[]):
            with self.assertRaises(EximError) as caught:
                sc.set_terms(self.company, "999", user=self.user, delivery_terms="EXW",
                             freight_per_mt=D("1"), brokerage_per_mt=D("0"))
        self.assertEqual(caught.exception.detail["code"], "contract_not_found")


class ContractImportTests(SapMixin, TestCase):
    SNAPSHOT = {
        "dc": [
            {"po_number": "220726109", "del_terms": "EXW", "freight_rate": D("4000"), "brokerage_rate": D("0")},
            {"po_number": "220726109", "del_terms": "EXW", "freight_rate": D("4000"), "brokerage_rate": D("0")},
            {"po_number": "220726068", "del_terms": "FOR", "freight_rate": D("0"), "brokerage_rate": D("0")},
            {"po_number": "220726200", "del_terms": "EXW", "freight_rate": D("3500"), "brokerage_rate": D("0")},
            {"po_number": "220726200", "del_terms": "EXW", "freight_rate": D("3600"), "brokerage_rate": D("0")},
            {"po_number": "220726200", "del_terms": "EXW", "freight_rate": D("3500"), "brokerage_rate": D("0")},
        ],
        "contracts": [
            {"po_number": "220726109", "frieght_rate": D("3900"), "brokerage_amount": None, "load_qty": None},
            {"po_number": "220526031", "frieght_rate": D("3100"), "brokerage_amount": D("2000"), "load_qty": D("40")},
            {"po_number": "220526052", "frieght_rate": None, "brokerage_amount": None, "load_qty": D("40")},
        ],
    }

    def test_the_sheet_wins_and_the_register_fills_in(self):
        report = import_contract_terms(self.SNAPSHOT, company=self.company)
        terms = {t.po_number: (t.delivery_terms, t.freight_per_mt, t.brokerage_per_mt)
                 for t in ContractTerms.objects.all()}
        self.assertEqual(terms, {
            "220726109": ("EXW", D("4000.00"), D("0.00")),
            "220726068": ("FOR", D("0.00"), D("0.00")),
            "220726200": ("EXW", D("3500.00"), D("0.00")),
            # From the register: brokerage 2,000 over 40 t loaded.
            "220526031": ("EXW", D("3100.00"), D("50.00")),
        })
        self.assertTrue(any("220726200" in n and "disagree" in n for n in report.notes))
        self.assertTrue(any("220726109" in n and "kept the sheet" in n for n in report.notes))

    def test_a_rerun_changes_nothing_and_terms_set_here_are_kept(self):
        import_contract_terms(self.SNAPSHOT, company=self.company)
        self.assertEqual(import_contract_terms(self.SNAPSHOT, company=self.company).counts["unchanged"], 4)
        mine = ContractTerms.objects.get(po_number="220726109")
        mine.freight_per_mt = D("4200")
        mine.save()
        report = import_contract_terms(self.SNAPSHOT, company=self.company)
        self.assertEqual(report.counts["skip"], 1)
        self.assertEqual(ContractTerms.objects.get(po_number="220726109").freight_per_mt, D("4200.00"))


class ContractAPITests(SapMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.role = UserRole.objects.create(name="Import / Export")
        self.headers = {"HTTP_COMPANY_CODE": "JIVO_OIL"}

    def client_for(self, *rights):
        n = get_user_model().objects.count() + 1
        user = get_user_model().objects.create_user(email=f"user{n}@example.com", password="x", full_name="U",
                                                    employee_code=f"U{n}")
        UserCompany.objects.create(user=user, company=self.company, role=self.role, is_default=True)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label="exim", codename__in=rights))
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_either_of_exims_contract_rights_opens_the_register(self):
        for right in ("view_domesticreports", "view_domesticcontractdetails"):
            with self.sap([po_line()], [grpo()]):
                response = self.client_for(right).get(f"{BASE}contracts/", {"year": "2026"}, **self.headers)
            self.assertEqual(response.status_code, 200, right)
            self.assertEqual(response.data["contracts"][0]["po_number"], "220726109")
        self.assertEqual(self.client_for().get(f"{BASE}contracts/", **self.headers).status_code, 403)

    def test_terms_need_the_change_right(self):
        payload = {"delivery_terms": "EXW", "freight_per_mt": "4000"}
        viewer = self.client_for("view_domesticreports")
        self.assertEqual(viewer.put(f"{BASE}contracts/220726109/terms/", payload, format="json",
                                    **self.headers).status_code, 403)
        with self.sap([po_line()]):
            response = self.client_for("change_domesticreports").put(
                f"{BASE}contracts/220726109/terms/", payload, format="json", **self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["freight_per_mt"], D("4000.00"))

    def test_a_bad_year_is_refused(self):
        response = self.client_for("view_domesticreports").get(f"{BASE}contracts/", {"year": "26x"}, **self.headers)
        self.assertEqual(response.status_code, 400)
