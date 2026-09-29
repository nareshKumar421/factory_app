"""
The copy of EXIM's tank farm and lots: everything arrives, as EXIM stored it,
linked up; a re-run changes nothing; and nothing changed here is overwritten.

EXIM is a stand-in SQLite database with EXIM's own table and column names.
"""

import sqlite3
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from company.models import Company

from . import services_tank
from .models_lot import (
    ContractHistory,
    LotChange,
    LotShortage,
    LotStatus,
    OilLot,
    StockDashboardRow,
    TemporaryVendor,
)
from .models_tank import OilCategory, Tank, TankItem, TankKind, TankLog
from .tank_farm_import import import_tank_farm, read_exim

D = Decimal

SCHEMA = """
CREATE TABLE tank_item (id TEXT PRIMARY KEY, tank_item_code TEXT, tank_item_name TEXT, category TEXT,
    is_active BOOL, created_at TEXT, created_by TEXT, color TEXT);
CREATE TABLE tank_data (tank_code TEXT PRIMARY KEY, item_code_id TEXT, tank_capacity NUMERIC,
    current_capacity NUMERIC, tank_type TEXT, is_active BOOL, created_at TEXT, updated_at TEXT);
CREATE TABLE "Party" (id INTEGER PRIMARY KEY, card_code TEXT, card_name TEXT, state TEXT,
    u_main_group TEXT, country TEXT);
CREATE TABLE stock_status (id INTEGER PRIMARY KEY, item_code_id TEXT, status TEXT, vendor_code_id TEXT,
    rate NUMERIC, quantity NUMERIC, total NUMERIC, rate_in_litres NUMERIC, quantity_in_litre NUMERIC,
    job_work TEXT, vehicle_number TEXT, transporter TEXT, location TEXT, eta DATE, parent_id INT,
    is_accumulator BOOL, arrival_date DATE, remainder_action TEXT, bility_number TEXT, grpo_number TEXT,
    payment_status TEXT, contract_start DATE, contract_end DATE, created_at TEXT, created_by TEXT,
    deleted BOOL);
CREATE TABLE shortage_entries (id INTEGER PRIMARY KEY, stock_id INT, item_code TEXT, item_name TEXT,
    rate NUMERIC, load_qty NUMERIC, unload_qty NUMERIC, shortage_qty NUMERIC, allowed_shortage_qty NUMERIC,
    deducted_shortage_qty NUMERIC, deduction_amount NUMERIC, supplier_code TEXT, supplier TEXT,
    vehicle_number TEXT, transporter TEXT, bility_number TEXT, grpo_number TEXT, created_at TEXT,
    created_by TEXT);
CREATE TABLE tank_logs (id INTEGER PRIMARY KEY, log_type TEXT, quantity NUMERIC, stock_status_id INT,
    vehicle_number TEXT, rate NUMERIC, party TEXT, item_code TEXT, item_name TEXT, arrival DATE,
    created_at TEXT, created_by TEXT);
CREATE TABLE stock_change_sessions (id TEXT PRIMARY KEY, stock_id INT, action TEXT, changed_by_id INT,
    changed_by_label TEXT, timestamp TEXT, note TEXT);
CREATE TABLE stock_field_logs (id INTEGER PRIMARY KEY, session_id TEXT, field_name TEXT, old_value TEXT,
    new_value TEXT);
CREATE TABLE contract_history (id INTEGER PRIMARY KEY, item_code TEXT, item_name TEXT, vendor_code TEXT,
    vendor_name TEXT, rate NUMERIC, contract_start DATE, contract_end DATE, created_at TEXT,
    created_by TEXT);
CREATE TABLE dashboard_order (id INTEGER PRIMARY KEY, item_code_id TEXT, order_number INT,
    created_at TEXT, updated_at TEXT);
"""

T = "2026-06-01 10:00:00+00:00"

ROWS = f"""
INSERT INTO tank_item VALUES
  ('73109f7d-4e6b-46c1-9209-dfcbc463775e', 'RM00CN', 'CANOLA', 'CANOLA', 1, '{T}', 'admin@exim.com', '#d95c26'),
  ('c2c38f74-0000-0000-0000-000000000001', 'RM00POM', 'POMACE', 'OILVE', 1, '{T}', 'admin@exim.com', '#59f37f'),
  ('c2c38f74-0000-0000-0000-000000000002', 'RM00X', 'NO GROUP', NULL, 0, '{T}', 'admin@exim.com', '');
INSERT INTO tank_data VALUES
  ('TNK0001', 'RM00CN', 50000, 21978, 'TANK', 1, '{T}', '{T}'),
  ('TOT001', NULL, 1000, 0, 'TOTES', 1, '{T}', '{T}');
INSERT INTO "Party" VALUES (1, 'VENDA000224', 'AWL AGRI BUSINESS LIMITED', 'GJ', 'PURCHASE OIL', 'IN'),
  (2, 'TEMP0010', 'UKRAINE', 'TMP', 'TMP', 'TMP'), (3, 'VENDATEMP', 'GARG AGRO PRODUCTS LLP', NULL, NULL, NULL);
INSERT INTO stock_status VALUES
  (1, 'RM00CN', 'IN_CONTRACT', 'VENDA000224', 120, 0, 0, 109.2, 0, NULL, NULL, NULL, 'GUJARAT', NULL, NULL,
   0, NULL, NULL, NULL, NULL, 'UNPAID', '2026-05-01', '2026-06-01', '{T}', 'raspreet@exim.com', 1),
  (2, 'RM00CN', 'IN_TANK', 'VENDA000224', 120, 20000, 2400000, 109.2, 21978, NULL, 'HR55X', 'ABC', 'SONIPAT', NULL, 1,
   0, '2026-05-20', NULL, 'BIL1', '77', 'UNPAID', NULL, NULL, '{T}', 'raspreet@exim.com', 0),
  (3, 'RM00POM', 'ON_THE_WAY', 'VENDATEMP', 90, 5000, 450000, 81.9, 5494.5, NULL, 'PB10', NULL, NULL, '2026-06-05',
   NULL, 0, NULL, NULL, NULL, NULL, 'UNPAID', NULL, NULL, '{T}', 'ghost@exim.com', 0);
INSERT INTO shortage_entries VALUES
  (1, 2, 'RM00CN', 'CANOLA', 120000, 20.1, 20, 0.1, 0.05025, 0.04975, 5970, 'VENDA000224', 'AWL', 'HR55X', 'ABC',
   'BIL1', '77', '{T}', 'raspreet@exim.com');
INSERT INTO tank_logs VALUES (1, 'INWARD', 20000, 2, 'HR55X', 120, 'AWL', 'RM00CN', 'CANOLA', NULL, '{T}',
   'raspreet@exim.com');
INSERT INTO stock_change_sessions VALUES
  ('aaaaaaaa-0000-0000-0000-000000000001', 2, 'CREATE', NULL, 'raspreet@exim.com', '{T}', ''),
  ('aaaaaaaa-0000-0000-0000-000000000002', 2, 'UPDATE', NULL, 'raspreet@exim.com', '{T}', 'move → IN_TANK');
INSERT INTO stock_field_logs VALUES
  (1, 'aaaaaaaa-0000-0000-0000-000000000001', '__create__', NULL, '{{"status": "IN_CONTRACT"}}'),
  (2, 'aaaaaaaa-0000-0000-0000-000000000002', 'status', '"OUT_SIDE_FACTORY"', '"IN_TANK"');
INSERT INTO contract_history VALUES (1, 'RM00CN', 'CANOLA', 'VENDA000224', 'AWL', 118.5, '2026-05-01',
   '2026-06-01', '{T}', 'raspreet@exim.com');
INSERT INTO dashboard_order VALUES (1, 'RM00POM', 1, '{T}', '{T}'), (2, 'RM00CN', 2, '{T}', '{T}');
"""


def exim_db(extra=""):
    db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    db.executescript(SCHEMA + ROWS + extra)
    return db


class TankFarmImportTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.raspreet = get_user_model().objects.create_user(
            email="raspreet@exim.com", password="x", full_name="Raspreet", employee_code="R1"
        )
        self.db = exim_db()
        self.addCleanup(self.db.close)

    def run_copy(self):
        return import_tank_farm(read_exim(self.db.cursor()), company=self.company)

    def test_everything_arrives_as_exim_stored_it(self):
        report = self.run_copy()
        self.assertEqual(report.counts["oils"]["create"], 3)
        self.assertEqual(report.counts["lots"]["create"], 3)

        pomace = TankItem.objects.get(code="RM00POM")
        self.assertEqual(pomace.category, OilCategory.OLIVE)  # EXIM's "OILVE"
        self.assertEqual(TankItem.objects.get(code="RM00X").category, "")

        tank = Tank.objects.get(code="TNK0001")
        self.assertEqual((tank.item.code, tank.capacity_l, tank.level_l), ("RM00CN", D("50000.00"), D("21978.00")))
        self.assertEqual(Tank.objects.get(code="TOT001").kind, TankKind.TOTE)

        lot = OilLot.objects.get(exim_id=2)
        self.assertEqual(lot.status, LotStatus.IN_TANK)
        self.assertEqual(lot.quantity_litres, D("21978.00"))
        self.assertEqual(lot.bilty_number, "BIL1")
        self.assertEqual(lot.vendor_name, "AWL AGRI BUSINESS LIMITED")
        self.assertEqual(lot.parent, OilLot.objects.get(exim_id=1))
        self.assertEqual(lot.created_by, self.raspreet)
        self.assertTrue(OilLot.objects.get(exim_id=1).deleted)

    def test_who_did_it_is_kept_even_without_a_login_here(self):
        self.run_copy()
        lot = OilLot.objects.get(exim_id=3)
        self.assertIsNone(lot.created_by)
        self.assertEqual(lot.created_by_label, "ghost@exim.com")

    def test_the_rows_that_hang_off_lots_arrive_linked(self):
        self.run_copy()
        lot = OilLot.objects.get(exim_id=2)
        shortage = LotShortage.objects.get()
        self.assertEqual((shortage.lot, shortage.deduction_amount), (lot, D("5970.000")))
        self.assertEqual(TankLog.objects.get().lot, lot)
        changes = LotChange.objects.filter(lot=lot).order_by("exim_ref")
        self.assertEqual([c.action for c in changes], ["CREATE", "UPDATE"])
        self.assertEqual(changes[1].field_changes.get().field_name, "status")
        self.assertEqual(ContractHistory.objects.get().rate, D("118.500"))

    def test_only_vendors_sap_does_not_know_become_temporary(self):
        self.run_copy()
        self.assertEqual(
            sorted(TemporaryVendor.objects.values_list("code", flat=True)), ["TEMP0010", "VENDATEMP"]
        )

    def test_the_dashboard_order_follows_exim(self):
        self.run_copy()
        order = list(StockDashboardRow.objects.order_by("position").values_list("item__code", flat=True))
        self.assertEqual(order, ["RM00POM", "RM00CN"])

    def test_a_rerun_changes_nothing(self):
        self.run_copy()
        again = self.run_copy()
        self.assertEqual(again.counts["lots"]["unchanged"], 3)
        self.assertEqual(again.counts["tanks"]["unchanged"], 2)
        self.assertEqual(again.counts["lot history"]["create"], 0)
        self.assertEqual(LotChange.objects.count(), 2)

    def test_a_rerun_follows_exim(self):
        self.run_copy()
        self.db.executescript(
            """
            UPDATE tank_data SET current_capacity = 15000 WHERE tank_code = 'TNK0001';
            UPDATE stock_status SET status = 'OUT_SIDE_FACTORY', arrival_date = '2026-06-06' WHERE id = 3;
            INSERT INTO tank_logs VALUES (2, 'INWARD', 5000, 3, 'PB10', 90, 'GARG', 'RM00POM', 'POMACE', NULL,
                '2026-06-06 10:00:00+00:00', 'raspreet@exim.com');
            """
        )
        report = self.run_copy()
        self.assertEqual(report.counts["tanks"]["update"], 1)
        self.assertEqual(Tank.objects.get(code="TNK0001").level_l, D("15000.00"))
        self.assertEqual(OilLot.objects.get(exim_id=3).status, LotStatus.OUT_SIDE_FACTORY)
        self.assertEqual(TankLog.objects.count(), 2)

    def test_what_was_changed_here_is_left_alone(self):
        self.run_copy()
        tank = Tank.objects.get(code="TNK0001")
        services_tank.update_tank(tank, user=self.raspreet, level_l=D("12000"))
        self.db.execute("UPDATE tank_data SET current_capacity = 15000 WHERE tank_code = 'TNK0001'")
        report = self.run_copy()
        self.assertEqual(report.counts["tanks"]["skip"], 1)
        self.assertEqual(Tank.objects.get(code="TNK0001").level_l, D("12000.00"))
        self.assertTrue(any("TNK0001" in note for note in report.notes))


class ImportTankFarmCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def call(self, *args):
        db = exim_db()
        self.addCleanup(db.close)
        out = StringIO()
        with mock.patch(
            "exim.management.commands.import_exim_tank_farm.read_exim", return_value=read_exim(db.cursor())
        ):
            call_command("import_exim_tank_farm", "--database", "default", *args, stdout=out)
        return out.getvalue()

    def test_without_commit_nothing_is_written(self):
        out = self.call()
        self.assertIn("DRY RUN - nothing was written", out)
        self.assertIn("3 create", out)
        self.assertFalse(OilLot.objects.exists())
        self.assertFalse(Tank.objects.exists())

    def test_commit_writes(self):
        self.call("--commit")
        self.assertEqual(OilLot.objects.count(), 3)
        self.assertEqual(Tank.objects.count(), 2)
