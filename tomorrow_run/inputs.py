"""The 7 pm read: every number the plan uses, from where it lives, once.

The apps are read once, at 7 pm the evening before (Daman, 17 Sept 2026), and
that read stands for the whole of the next day. What comes back is a plain,
JSON-safe dict — the plan is built from it, and it is stored with the plan so
a pick later in the evening re-times the day over the very same numbers.

A required input that cannot be read stops the build: a plan made without the
stock or the recipes would look like a plan and be wrong.
"""

import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from django.utils import timezone

from company.models import Company
from planning_purchase.hana_reader import HanaProductionPlanReader, classify_material
from sap_client.context import CompanyContext

from . import constants as C
from .hana_reader import TomorrowRunReader, left_per_day
from .models import PlanningSheet

logger = logging.getLogger(__name__)

APP = "ji.jivo.in"


class InputsUnavailable(RuntimeError):
    """A required input could not be read; the message names which."""


def next_working_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    return nxt + timedelta(days=1) if nxt.weekday() == 6 else nxt


def user_name(user) -> str:
    if not user:
        return ""
    return (getattr(user, "full_name", "") or "").strip() or getattr(user, "email", "") or str(user)


class Reading:
    """One input read, timed, with what it found — the page's "inputs it read" table."""

    def __init__(self):
        self.sources: List[Dict[str, Any]] = []

    def read(self, input_name: str, group: str, app: str, url: Optional[str], what: str,
             fn: Callable[[], Any], number: Callable[[Any], Any] = lambda _v: None, required=True):
        started = time.monotonic()
        row = {"input": input_name, "group": group, "app": app, "url": url, "what": what}
        try:
            value = fn()
        except Exception as e:  # SAP down, a bad query, a missing table
            logger.exception("Tomorrow's run: %s could not be read", input_name)
            row.update({"ok": False, "error": str(e) or e.__class__.__name__, "fetched_at": timezone.now().isoformat(),
                        "number": None, "seconds": round(time.monotonic() - started, 1)})
            self.sources.append(row)
            if required:
                raise InputsUnavailable(f"{input_name}: {e}") from e
            return None
        row.update({"ok": True, "error": None, "fetched_at": timezone.now().isoformat(),
                    "number": number(value), "seconds": round(time.monotonic() - started, 1)})
        self.sources.append(row)
        return value


def gather(company: Company, for_date: Optional[date] = None, now=None) -> Dict[str, Any]:
    now = now or timezone.localtime()
    today = now.date()
    for_date = for_date or next_working_day(today)
    ctx = CompanyContext(company.code)
    R = Reading()
    plan_reader = HanaProductionPlanReader(ctx)
    room_reader = TomorrowRunReader(ctx)

    # --- 1. the planning sheet -----------------------------------------------
    sheet = PlanningSheet.objects.filter(company=company).prefetch_related("lines").first()
    sheet_in = None
    if sheet:
        from_date = sheet.stock_date + timedelta(days=1)
        sheet_in = {
            "id": sheet.id, "file": sheet.file_name, "tab": sheet.tab, "title": sheet.title,
            "stock_date": sheet.stock_date.isoformat(), "from_date": from_date.isoformat(),
            "date_basis": sheet.date_basis, "put_in_at": sheet.uploaded_at.isoformat(),
            "put_in_by": user_name(sheet.uploaded_by),
            "lines": [
                {"row": ln.row, "code": ln.code, "name": ln.name, "plan_l": float(ln.plan_l),
                 "ecom_l": float(ln.ecom_l), "stock_l": float(ln.stock_l), "net_l": float(ln.net_l),
                 "machine": ln.machine}
                for ln in sheet.lines.all()
            ],
        }
        R.sources.append({
            "input": "The planning sheet — what to make", "group": "Plan",
            "app": "the planning sheet (Excel from the planning team), put in on this page", "url": None,
            "what": "What to make: every line of the newest planning sheet — the month's plan, ecom, the stock "
                    "in BH-PF and BH-BT on the sheet's date, Net Req, and the machine it names",
            "ok": True, "error": None, "fetched_at": sheet.uploaded_at.isoformat(),
            "number": {"file": sheet.file_name, "lines": sheet.line_count, "net_req_l": float(sheet.net_req_l),
                       "stock_date": sheet.stock_date.isoformat(), "counts_from": from_date.isoformat()},
        })
    else:
        R.sources.append({
            "input": "The planning sheet — what to make", "group": "Plan", "app": "this page", "url": None,
            "what": "the newest planning sheet put in", "ok": False, "error": "no planning sheet has been put in",
            "fetched_at": None, "number": None,
        })

    # --- item master: litres per piece ------------------------------------
    from production_execution.services.reconciliation_reader import ReconciliationReader

    recon = ReconciliationReader(ctx)
    litre_items = R.read(
        "Litres in one piece", "Other", "SAP item master (OITM.SalPackUn, U_IsLitre)", None,
        "the volume of one piece of every finished good, the one source of litres across the app",
        recon.litre_items, lambda v: {"items": len(v)},
    )
    items = {x["item_code"]: {"name": x["item_name"], "lp": x["litres_per_piece"]} for x in litre_items}
    lp = {c: v["lp"] for c, v in items.items()}

    # --- 2. made since the sheet's date ------------------------------------
    made = {"from": None, "to": today.isoformat(), "pcs": {}}
    if sheet_in:
        made["from"] = sheet_in["from_date"]
        rows = R.read(
            "Made since the sheet's date", "Plan", APP, "/dashboards/production",
            "what the plant made since the planning sheet's date: finished goods received into BH-PF, "
            "pieces per item (the production check's SAP receipts)",
            lambda: recon.fg_by_item(C.PRODUCTION_ROOM, sheet_in["from_date"], today.isoformat()),
            lambda v: {"from": sheet_in["from_date"], "to": today.isoformat(), "items": len(v),
                       "litres": sum(r["sap_qty"] * lp.get(r["item_code"], 0) for r in v)},
        )
        made["pcs"] = {r["item_code"]: r["sap_qty"] for r in rows}

    # --- rooms: stock tonight and what leaves in a day ------------------------
    rooms = tuple(C.ROOM_LIMIT_L)
    stock = R.read(
        "Stock in BH-BT, BH-PF tonight — for room", "Storage", APP, "/dashboards/stock-levels",
        "finished goods in each room, pieces per item",
        lambda: room_reader.room_stock(rooms),
        lambda v: {r: {"pcs": sum(v[r].values()), "litres": sum(q * lp.get(c, 0) for c, q in v[r].items()),
                       "skus": len(v[r])} for r in v},
    )
    days = []
    d = today - timedelta(days=1)
    while len(days) < C.LEFT_DAYS + 3 and d > today - timedelta(days=30):
        if d.weekday() != 6:
            days.append(d)
        d -= timedelta(days=1)
    flows = R.read(
        "What leaves the two rooms in a day", "Storage", APP, "/dashboards/stock-levels",
        f"what left BH-BT + BH-PF each working day, the last {C.LEFT_DAYS}: stock the night before + finished "
        "goods received into BH-PF that day − stock that night; and BH-BT's own trucks (A/R invoices and deliveries)",
        lambda: left_per_day(room_reader.room_flows(rooms, min(days), max(days)), lp, C.PRODUCTION_ROOM, C.TRUCK_ROOM),
        lambda v: None,
    )
    left_days = [
        {"date": day.isoformat(), **flows[day.isoformat()]}
        for day in sorted(days) if day.isoformat() in flows
    ][-C.LEFT_DAYS:]
    R.sources[-1]["number"] = {
        "per_day": [{"date": x["date"], "left_l": x["left_l"]} for x in left_days],
        "average_l": sum(x["left_l"] for x in left_days) / len(left_days) if left_days else 0,
        "truck_room_avg_l": sum(x["truck_l"] for x in left_days) / len(left_days) if left_days else 0,
    }
    R.sources.append({
        "input": "Room limits — BH-BT, BH-PF", "group": "Storage", "app": "you", "url": None,
        "what": "your numbers", "ok": True, "error": None, "fetched_at": "14 Sept 2026",
        "number": dict(C.ROOM_LIMIT_L),
    })

    # --- oil I have: the Raw Material Stock page --------------------------------
    from warehouse.models_rm_stock import RawMaterialStock
    from warehouse.services.rm_stock_service import register_warehouse

    def _oil():
        out = []
        for row in RawMaterialStock.objects.filter(
            company=company, warehouse_code=register_warehouse(), is_active=True,
        ).select_related("set_by"):
            out.append({
                "rm": row.item_code, "name": row.item_name or row.item_code, "litres": float(row.qty),
                "uom": row.uom, "as_of": row.as_of_date.isoformat() if row.as_of_date else None,
                "set_by": user_name(row.set_by), "set_at": row.updated_at.isoformat() if row.updated_at else None,
                "warehouse": row.warehouse_code,
            })
        return out

    oil = R.read(
        "Oil I have — the Raw Material Stock page", "RM / PM", APP, "/warehouse/rm-stock",
        "the store's own count of each oil on the floor, with the date it was counted — the one place for oil "
        "(EXIM tanks and SAP drums are not read)",
        _oil,
        lambda v: {"oils": len(v), "litres": sum(x["litres"] for x in v),
                   "oldest_count": min((x["as_of"] for x in v if x["as_of"]), default=None),
                   "newest_count": max((x["as_of"] for x in v if x["as_of"]), default=None)},
    )

    # --- packaging I have: the Packing Material dashboard ----------------------
    from packing_material.hana_reader import PackingMaterialReader

    pm_reader = PackingMaterialReader(ctx)

    def _packaging():
        names = {r["item_code"]: r["item_name"] for r in pm_reader.pm_master()}
        out: Dict[str, Dict[str, Any]] = {}
        for r in pm_reader.pm_stock_by_warehouse(C.PACKAGING_ROOMS):
            x = out.setdefault(r["item_code"], {"name": names.get(r["item_code"], r["item_code"]), "have": 0.0})
            x["have"] += r["stock_qty"]
        return out

    packaging = R.read(
        f"Packaging I have — the Packing Material dashboard: {', '.join(C.PACKAGING_ROOMS)}", "RM / PM", APP,
        "/dashboards/packing-material",
        "pieces per item, one block per packaging room — the only packaging source",
        _packaging,
        lambda v: {"rooms": list(C.PACKAGING_ROOMS), "items_with_stock": sum(1 for x in v.values() if x["have"] > 0)},
    )

    # --- the recipes -------------------------------------------------------------
    planned = set()
    for ln in (sheet_in or {}).get("lines") or []:
        code = (ln["code"] or "").strip().upper()
        if code:
            planned.add(C.SHEET_CODE_ALIASES.get(code, {}).get("planned_as", code))
    for first, second, _n, _w in C.CARTON_TWINS:
        if first in planned or second in planned:
            planned.update((first, second))

    def _bom():
        out: Dict[str, List[Dict[str, Any]]] = {}
        for r in plan_reader.get_bom_components(sorted(planned)):
            if int(r.get("LineType") or 4) != 4:
                continue          # a resource line: conversion cost, not material
            kind = classify_material(r.get("ItemGroup"))
            if kind not in ("RAW", "PACKAGING"):
                continue
            out.setdefault(r["ParentCode"], []).append({
                "code": r["ComponentCode"], "name": r.get("ComponentName") or r["ComponentCode"],
                "kind": "oil" if kind == "RAW" else "packaging",
                "per_piece": float(r.get("QtyPerUnit") or 0), "uom": r.get("Uom") or "",
            })
        return out

    bom = R.read(
        "BOM — what each SKU needs", "RM / PM", "SAP recipe master (OITT + ITT1, Oil)", None,
        "every component per ONE piece: oil, bottle, cap, labels, carton share, tape",
        _bom, lambda v: {"recipes": len(v), "lines": sum(len(x) for x in v.values())},
    )

    # --- what each machine is running now -----------------------------------
    running = R.read(
        "What each machine is running now", "Other", APP, "/production/execution",
        f"each machine's last run in the production log, the last {C.RUNNING_NOW_DAYS} days",
        lambda: running_now(company, today), lambda v: {m: (x or {}).get("code") for m, x in v.items()},
        required=False,
    ) or {}

    R.sources.append({
        "input": "Machine speeds, packs, changeovers", "group": "Other", "app": "the PDF",
        "url": "https://jivo-plan-drawings.vercel.app/#tables",
        "what": "Machine_Planning_Simple_Tables.pdf + your corrections (tomorrow_run/constants.py)",
        "ok": True, "error": None, "fetched_at": "14 Sept 2026", "number": {"machines": len(C.MACHINES)},
    })
    R.sources.append({
        "input": "Daily target — 100 / 125 ton", "group": "Other", "app": "you", "url": None,
        "what": "your note", "ok": True, "error": None, "fetched_at": "14 Sept 2026",
        "number": {"target_l": C.TARGET_L, "good_l": C.GOOD_L},
    })

    return {
        "for_date": for_date.isoformat(),
        "read_at": now.isoformat(),
        "sheet": sheet_in,
        "aliases": C.SHEET_CODE_ALIASES,
        "items": items,
        "made": made,
        "stock": stock,
        "left_days": left_days,
        "oil": oil,
        "packaging": packaging,
        "bom": bom,
        "running_now": running,
        "sources": R.sources,
    }


def running_now(company: Company, today: date) -> Dict[str, Optional[Dict[str, Any]]]:
    """Each machine's last run on its production line this week, or None."""
    from production_execution.models import ProductionLine, ProductionRun

    lines = list(ProductionLine.objects.filter(company=company, is_active=True))
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for machine in C.MACHINES:
        names = C.MACHINE_LINES.get(machine)
        line = None
        if names:
            for want in names:
                line = next((ln for ln in lines if " ".join(ln.name.lower().split()) == want), None)
                if line:
                    break
        if not line:
            out[machine] = None
            continue
        run = (
            ProductionRun.objects.filter(
                company=company, line=line, date__lte=today,
                date__gte=today - timedelta(days=C.RUNNING_NOW_DAYS),
            )
            .exclude(item_code="")
            .order_by("-date", "-planned_start_at", "-id")
            .first()
        )
        out[machine] = (
            {"code": run.item_code, "name": run.product, "date": run.date.isoformat(), "status": run.status,
             "line": line.name, "basis": "the production log"}
            if run else None
        )
    return out
