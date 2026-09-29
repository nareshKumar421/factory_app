"""Copy EXIM's tank farm and every oil lot into this project, in one go.

The two have to move together: moving a lot into the tanks writes the tank log,
and EXIM's tables point at each other. So one copy brings across, in order:

  oils (``tank_item``) -> tanks (``tank_data``) -> temporary vendors (``Party``)
  -> lots (``stock_status``) and their splits -> shortages (``shortage_entries``)
  -> tank log (``tank_logs``) -> lot history (``stock_change_sessions`` and
  ``stock_field_logs``) -> contract history -> the stock dashboard's row order.

AS THEY ARE. Every figure is copied as EXIM stored it - a lot's litres and
totals are not worked out again, a shortage's debit is not recalculated. Who did
it arrives as EXIM wrote it (an email); where that email is a login here, the
row points at the login too.

RE-RUNNABLE. Everything is found again by its EXIM id, so a second run updates
in place and adds what is new. It never:
 - writes to EXIM;
 - overwrites an oil, tank or lot changed HERE since it was copied (reported and
   left alone - that would lose somebody's work);
 - removes anything: EXIM removes nothing either, it only marks lots deleted.

WHAT STAYS BEHIND
 - EXIM's ``Party`` table except the temporary vendors: a lot keeps its vendor's
   code and name, and real vendors are read from SAP.
 - ``dashboard_snapshot``: nothing reads it, and EXIM's job has written nothing
   into it since June.
 - ``stock_update_logs``: EXIM's first history table, empty.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models_lot import (
    ContractHistory,
    LotChange,
    LotFieldChange,
    LotShortage,
    LotStatus,
    OilLot,
    PaymentStatus,
    StockDashboardRow,
    TemporaryVendor,
)
from .models_tank import OilCategory, Tank, TankItem, TankKind, TankLog

#: EXIM's category spellings that differ from ours.
CATEGORY = {"OILVE": OilCategory.OLIVE}
TANK_KIND = {"TANK": TankKind.TANK, "TOTES": TankKind.TOTE, None: TankKind.TANK, "": TankKind.TANK}


def _dec(value, places="0.01"):
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _when(value):
    """A timestamp as an aware datetime, whatever the driver handed over."""
    if value is None:
        return None
    if isinstance(value, str):
        value = parse_datetime(value)
    if isinstance(value, datetime) and timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


def _text(value) -> str:
    return (value or "").strip() if isinstance(value, str) else ("" if value is None else str(value))


def _rows(cursor, sql):
    cursor.execute(sql)
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


SQL = {
    "items": "SELECT id, tank_item_code, tank_item_name, category, is_active, created_at, color FROM tank_item",
    "tanks": (
        "SELECT tank_code, item_code_id, tank_capacity, current_capacity, tank_type, is_active, "
        "created_at, updated_at FROM tank_data"
    ),
    "parties": 'SELECT card_code, card_name FROM "Party"',
    "lots": (
        "SELECT id, item_code_id, status, vendor_code_id, rate, quantity, total, rate_in_litres, "
        "quantity_in_litre, job_work, vehicle_number, transporter, location, eta, parent_id, is_accumulator, "
        "arrival_date, bility_number, grpo_number, payment_status, contract_start, contract_end, "
        "created_at, created_by, deleted FROM stock_status"
    ),
    "shortages": (
        "SELECT id, stock_id, item_code, item_name, rate, load_qty, unload_qty, shortage_qty, "
        "allowed_shortage_qty, deducted_shortage_qty, deduction_amount, supplier_code, supplier, "
        "vehicle_number, transporter, bility_number, grpo_number, created_at, created_by FROM shortage_entries"
    ),
    "tank_logs": (
        "SELECT id, log_type, quantity, stock_status_id, vehicle_number, rate, party, item_code, item_name, "
        "arrival, created_at, created_by FROM tank_logs"
    ),
    "sessions": "SELECT id, stock_id, action, changed_by_label, timestamp, note FROM stock_change_sessions",
    "field_logs": "SELECT id, session_id, field_name, old_value, new_value FROM stock_field_logs",
    "contracts": (
        "SELECT id, item_code, item_name, vendor_code, vendor_name, rate, contract_start, contract_end, "
        "created_at, created_by FROM contract_history"
    ),
    "dashboard_order": "SELECT item_code_id, order_number FROM dashboard_order",
}


@dataclass
class EximTankFarm:
    tables: dict

    def __getitem__(self, name):
        return self.tables[name]


def read_exim(cursor) -> EximTankFarm:
    """Read everything the copy needs from EXIM. SELECTs only."""
    return EximTankFarm(tables={name: _rows(cursor, sql) for name, sql in SQL.items()})


@dataclass
class ImportReport:
    #: table -> Counter of create / update / unchanged / skip
    counts: dict = field(default_factory=lambda: defaultdict(Counter))
    notes: list = field(default_factory=list)

    def tally(self, table, action, n=1):
        self.counts[table][action] += n


def _changed_here(obj) -> bool:
    return obj.copied_from_exim_at is not None and obj.updated_at > obj.copied_from_exim_at


def _set(obj, values: dict) -> bool:
    changed = False
    for name, value in values.items():
        if getattr(obj, name) != value:
            setattr(obj, name, value)
            changed = True
    return changed


class _Copier:
    def __init__(self, snapshot: EximTankFarm, company):
        self.s = snapshot
        self.company = company
        self.report = ImportReport()
        self.users = {
            u.email.lower(): u for u in get_user_model().objects.filter(email__isnull=False).exclude(email="")
        }
        self.stamped = defaultdict(list)  # model -> pks to stamp at the end

    def user(self, email):
        return self.users.get(_text(email).lower())

    # -- oils ---------------------------------------------------------------
    def items(self):
        self.item_by_code = {}
        for row in self.s["items"]:
            ref = str(row["id"])
            values = {
                "code": _text(row["tank_item_code"]),
                "name": _text(row["tank_item_name"]),
                "category": CATEGORY.get(row["category"], row["category"] or ""),
                "color": _text(row["color"])[:10],
                "is_active": bool(row["is_active"]),
            }
            oil = TankItem.objects.filter(exim_ref=ref).first()
            if oil is None:
                clash = TankItem.objects.filter(company=self.company, code__iexact=values["code"]).first()
                if clash is not None:
                    self.report.tally("oils", "conflict")
                    self.report.notes.append(f"oil {values['code']}: one with this code was added here; left alone")
                    self.item_by_code[values["code"]] = clash
                    continue
                oil = TankItem.objects.create(company=self.company, exim_ref=ref, **values)
                TankItem.objects.filter(pk=oil.pk).update(created_at=_when(row["created_at"]))
                self.report.tally("oils", "create")
            elif _changed_here(oil):
                self.report.tally("oils", "skip")
            elif _set(oil, values):
                oil.save()
                self.report.tally("oils", "update")
            else:
                self.report.tally("oils", "unchanged")
            self.stamped[TankItem].append(oil.pk)
            self.item_by_code[values["code"]] = oil

    # -- tanks --------------------------------------------------------------
    def tanks(self):
        for row in self.s["tanks"]:
            code = _text(row["tank_code"])
            item = self.item_by_code.get(_text(row["item_code_id"])) if row["item_code_id"] else None
            values = {
                "code": code,
                "kind": TANK_KIND.get(row["tank_type"], TankKind.TANK),
                "item": item,
                "capacity_l": _dec(row["tank_capacity"]),
                "level_l": _dec(row["current_capacity"] or 0),
                "is_active": bool(row["is_active"]),
            }
            tank = Tank.objects.filter(exim_ref=code).first()
            if tank is None:
                if Tank.objects.filter(company=self.company, code=code).exists():
                    self.report.tally("tanks", "conflict")
                    self.report.notes.append(f"tank {code}: one with this code was added here; left alone")
                    continue
                tank = Tank.objects.create(company=self.company, exim_ref=code, **values)
                self.report.tally("tanks", "create")
            elif _changed_here(tank):
                self.report.tally("tanks", "skip")
                self.report.notes.append(f"tank {code}: changed here since it was copied; left alone")
            elif _set(tank, values):
                tank.save()
                self.report.tally("tanks", "update")
            else:
                self.report.tally("tanks", "unchanged")
            self.stamped[Tank].append(tank.pk)

    # -- temporary vendors ----------------------------------------------------
    def vendors(self):
        self.vendor_name = {}
        for row in self.s["parties"]:
            code = _text(row["card_code"])
            name = _text(row["card_name"])
            self.vendor_name[code] = name
            # TEMP0010 and friends, and the hand-made VENDATEMP codes: none of
            # them is a vendor SAP knows.
            if "TEMP" not in code.upper():
                continue
            vendor, created = TemporaryVendor.objects.get_or_create(
                company=self.company, code=code, defaults={"name": name or code},
            )
            if created:
                self.report.tally("temporary vendors", "create")
            elif vendor.name != (name or code):
                vendor.name = name or code
                vendor.save(update_fields=["name"])
                self.report.tally("temporary vendors", "update")
            else:
                self.report.tally("temporary vendors", "unchanged")

    # -- lots ---------------------------------------------------------------
    def lots(self):
        self.lot_by_exim = {}
        parents = {}
        for row in self.s["lots"]:
            exim_id = row["id"]
            item = self.item_by_code.get(_text(row["item_code_id"]))
            if item is None:
                self.report.tally("lots", "skip")
                self.report.notes.append(f"lot #{exim_id}: its oil {row['item_code_id']!r} is not in EXIM's oils")
                continue
            status = row["status"] if row["status"] in LotStatus.values else None
            if status is None:
                self.report.tally("lots", "skip")
                self.report.notes.append(f"lot #{exim_id}: status {row['status']!r} is not a lot status")
                continue
            vendor_code = _text(row["vendor_code_id"])
            values = {
                "item": item,
                "status": status,
                "vendor_code": vendor_code,
                "vendor_name": self.vendor_name.get(vendor_code, ""),
                "rate": _dec(row["rate"], "0.001"),
                "quantity": _dec(row["quantity"]),
                "total": _dec(row["total"] or 0),
                "rate_per_litre": _dec(row["rate_in_litres"], "0.001"),
                "quantity_litres": _dec(row["quantity_in_litre"] or 0),
                "job_work": _text(row["job_work"]),
                "vehicle_number": _text(row["vehicle_number"]),
                "transporter": _text(row["transporter"]),
                "location": _text(row["location"]),
                "eta": row["eta"],
                "arrival_date": row["arrival_date"],
                "is_accumulator": bool(row["is_accumulator"]),
                "bilty_number": _text(row["bility_number"]),
                "grpo_number": _text(row["grpo_number"]),
                "payment_status": row["payment_status"] if row["payment_status"] in PaymentStatus.values
                else PaymentStatus.UNPAID,
                "contract_start": row["contract_start"],
                "contract_end": row["contract_end"],
                "deleted": bool(row["deleted"]),
                "created_by_label": _text(row["created_by"]),
                "created_by": self.user(row["created_by"]),
                "created_at": _when(row["created_at"]),
            }
            lot = OilLot.objects.filter(exim_id=exim_id).first()
            if lot is None:
                lot = OilLot.objects.create(company=self.company, exim_id=exim_id, **values)
                self.report.tally("lots", "create")
            elif _changed_here(lot):
                self.report.tally("lots", "skip")
                self.report.notes.append(f"lot #{exim_id}: changed here since it was copied; left alone")
                self.lot_by_exim[exim_id] = lot
                continue
            elif _set(lot, values):
                lot.save()
                self.report.tally("lots", "update")
            else:
                self.report.tally("lots", "unchanged")
            self.lot_by_exim[exim_id] = lot
            self.stamped[OilLot].append(lot.pk)
            parents[lot.pk] = row["parent_id"]

        # Splits point at other lots, so they are linked once every lot exists.
        for pk, parent_exim in parents.items():
            parent = self.lot_by_exim.get(parent_exim) if parent_exim else None
            OilLot.objects.filter(pk=pk).exclude(parent=parent).update(parent=parent)

    # -- append-only tables ----------------------------------------------------
    def _add(self, table, model, rows, key, build):
        have = set(model.objects.filter(**{f"{key}__in": [r["id"] for r in rows]}).values_list(key, flat=True))
        fresh = [build(row) for row in rows if row["id"] not in have]
        fresh = [obj for obj in fresh if obj is not None]
        model.objects.bulk_create(fresh, batch_size=500)
        self.report.tally(table, "create", len(fresh))
        self.report.tally(table, "unchanged", len(have))

    def shortages(self):
        def build(row):
            return LotShortage(
                company=self.company,
                exim_id=row["id"],
                lot=self.lot_by_exim.get(row["stock_id"]),
                item_code=_text(row["item_code"]),
                item_name=_text(row["item_name"]),
                supplier_code=_text(row["supplier_code"]),
                supplier=_text(row["supplier"]),
                vehicle_number=_text(row["vehicle_number"]),
                transporter=_text(row["transporter"]),
                bilty_number=_text(row["bility_number"]),
                grpo_number=_text(row["grpo_number"]),
                rate=_dec(row["rate"] or 0, "0.001"),
                load_qty_mt=_dec(row["load_qty"], "0.001"),
                unload_qty_mt=_dec(row["unload_qty"], "0.001"),
                shortage_mt=_dec(row["shortage_qty"], "0.001"),
                allowed_mt=_dec(row["allowed_shortage_qty"], "0.001"),
                deducted_mt=_dec(row["deducted_shortage_qty"], "0.001"),
                deduction_amount=_dec(row["deduction_amount"], "0.001"),
                created_by=self.user(row["created_by"]),
                created_by_label=_text(row["created_by"]),
                created_at=_when(row["created_at"]),
            )

        self._add("shortages", LotShortage, self.s["shortages"], "exim_id", build)

    def tank_logs(self):
        def build(row):
            return TankLog(
                company=self.company,
                exim_ref=str(row["id"]),
                kind=row["log_type"] or "INWARD",
                lot=self.lot_by_exim.get(row["stock_status_id"]),
                quantity_kg=_dec(row["quantity"] or 0),
                rate=_dec(row["rate"], "0.001"),
                vehicle_number=_text(row["vehicle_number"]),
                party=_text(row["party"]),
                item_code=_text(row["item_code"]),
                item_name=_text(row["item_name"]),
                arrival=row["arrival"],
                created_by=self.user(row["created_by"]),
                created_by_label=_text(row["created_by"]),
                created_at=_when(row["created_at"]),
            )

        rows = [dict(r, id=str(r["id"])) for r in self.s["tank_logs"]]
        self._add("tank log", TankLog, rows, "exim_ref", build)

    def history(self):
        fields_by_session = defaultdict(list)
        for row in self.s["field_logs"]:
            fields_by_session[str(row["session_id"])].append(row)
        sessions = [dict(r, id=str(r["id"])) for r in self.s["sessions"]]
        have = set(
            LotChange.objects.filter(exim_ref__in=[r["id"] for r in sessions]).values_list("exim_ref", flat=True)
        )
        changes, lost = [], 0
        for row in sessions:
            if row["id"] in have:
                continue
            lot = self.lot_by_exim.get(row["stock_id"])
            if lot is None:
                lost += 1
                continue
            changes.append(
                LotChange(
                    lot=lot,
                    exim_ref=row["id"],
                    action=row["action"] if row["action"] in ("CREATE", "UPDATE") else "UPDATE",
                    changed_by=self.user(row["changed_by_label"]),
                    changed_by_label=_text(row["changed_by_label"]),
                    note=_text(row["note"])[:255],
                    timestamp=_when(row["timestamp"]),
                )
            )
        LotChange.objects.bulk_create(changes, batch_size=500)
        by_ref = {c.exim_ref: c for c in LotChange.objects.filter(exim_ref__in=[c.exim_ref for c in changes])}
        LotFieldChange.objects.bulk_create(
            [
                LotFieldChange(
                    change=by_ref[ref],
                    field_name=_text(f["field_name"])[:100],
                    old_value=f["old_value"],
                    new_value=f["new_value"],
                )
                for ref in by_ref
                for f in sorted(fields_by_session.get(ref, []), key=lambda r: r["id"])
            ],
            batch_size=1000,
        )
        self.report.tally("lot history", "create", len(changes))
        self.report.tally("lot history", "unchanged", len(have))
        if lost:
            self.report.notes.append(f"{lost} history entries belong to lots that were not copied")

    def contracts(self):
        def build(row):
            return ContractHistory(
                company=self.company,
                exim_id=row["id"],
                item_code=_text(row["item_code"]),
                item_name=_text(row["item_name"]),
                vendor_code=_text(row["vendor_code"]),
                vendor_name=_text(row["vendor_name"]),
                rate=_dec(row["rate"] or 0, "0.001"),
                contract_start=row["contract_start"],
                contract_end=row["contract_end"],
                created_by_label=_text(row["created_by"]),
                created_at=_when(row["created_at"]),
            )

        self._add("contract history", ContractHistory, self.s["contracts"], "exim_id", build)

    def dashboard_order(self):
        rows = sorted(self.s["dashboard_order"], key=lambda r: r["order_number"])
        wanted = []
        for row in rows:
            oil = self.item_by_code.get(_text(row["item_code_id"]))
            if oil is not None and oil.pk not in wanted:
                wanted.append(oil.pk)
        current = list(
            StockDashboardRow.objects.filter(company=self.company).order_by("position").values_list("item_id", flat=True)
        )
        if current == wanted:
            self.report.tally("dashboard order", "unchanged", len(wanted))
            return
        StockDashboardRow.objects.filter(company=self.company).delete()
        StockDashboardRow.objects.bulk_create(
            [StockDashboardRow(company=self.company, item_id=pk, position=i) for i, pk in enumerate(wanted, 1)]
        )
        self.report.tally("dashboard order", "update", len(wanted))

    def stamp(self):
        # One instant for both, at the END of the copy, so the "changed here"
        # test on the next run measures from when this one finished.
        now = timezone.now()
        for model, pks in self.stamped.items():
            model.objects.filter(pk__in=pks).update(copied_from_exim_at=now, updated_at=now)


@transaction.atomic
def import_tank_farm(snapshot: EximTankFarm, *, company) -> ImportReport:
    copier = _Copier(snapshot, company)
    copier.items()
    copier.tanks()
    copier.vendors()
    copier.lots()
    copier.shortages()
    copier.tank_logs()
    copier.history()
    copier.contracts()
    copier.dashboard_order()
    copier.stamp()
    return copier.report
