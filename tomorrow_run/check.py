"""The 7 pm check: last night's plan for today, beside what the plant did.

It only tells. The plan never reads it (Daman, 22 Sept 2026): a check the
engine learned from silently would make the plan chase yesterday's floor.

10% off is red. Some rows are red whenever they happen at all — an item the
plan held for want of material and the plant made anyway says the material
figure was wrong.
"""

from collections import defaultdict
from typing import Any, Dict, List

from django.db.models import Sum

from sap_client.context import CompanyContext

from . import constants as C
from .hana_reader import TomorrowRunReader, left_per_day

OFF = 0.10


def _t(litres: float) -> str:
    return f"{litres / 1000:,.1f} T"


def _nice(name: str) -> str:
    return " ".join((name or "").lower().split())


def _state(plan_l: float, actual_l: float) -> str:
    if plan_l <= 0 and actual_l <= 0:
        return "green"
    if plan_l <= 0 or abs(actual_l - plan_l) / plan_l > OFF:
        return "red"
    return "green"


def _runs_today(company, day):
    from production_execution.models import ProductionLine, ProductionRun, ProductionSegment

    lines = {" ".join(ln.name.lower().split()): ln for ln in ProductionLine.objects.filter(company=company)}
    runs = list(ProductionRun.objects.filter(company=company, date=day).select_related("line"))
    seg = {
        r["production_run"]: float(r["q"] or 0)
        for r in ProductionSegment.objects.filter(production_run__in=runs)
        .values("production_run").annotate(q=Sum("produced_cases"))
    }
    by_line = defaultdict(list)
    for r in runs:
        cases = float(r.total_production or 0) or seg.get(r.id, 0.0)
        pieces = cases * float(r.pieces_per_case or 1)
        litres = pieces * float(r.litres_per_piece or 0)
        by_line[" ".join(r.line.name.lower().split()) if r.line else ""].append({
            "code": r.item_code, "name": r.product, "litres": litres, "running": r.status == "IN_PROGRESS",
        })
    return lines, by_line


def build_check(company, plan, *, day, now) -> List[Dict[str, Any]]:
    P = plan.plan
    rows: List[Dict[str, Any]] = []
    lines, by_line = _runs_today(company, day)

    def add(n, what, state, plan_txt, actual_txt, line):
        rows.append({"n": n, "what": what, "state": state, "plan": plan_txt, "actual": actual_txt, "line": line})

    made_codes = defaultdict(float)
    any_running = False
    total_actual = 0.0
    for m in C.MACHINES:
        v = (P.get("machines") or {}).get(m) or {}
        planned = v.get("jobs") or []
        plan_txt = ", ".join(f"{_nice(j['name'])} {_t(j['litres'])}" for j in planned) or "nothing"
        plan_l = sum(j["litres"] for j in planned)
        names = C.MACHINE_LINES.get(m)
        line_key = next((n for n in (names or ()) if n in lines), None)
        if not line_key:
            if plan_l > 0:
                add(1, m, "no record", plan_txt, "the factory app does not log this machine",
                    f"{m}: planned {plan_txt} · no record, the factory app does not log this machine")
            continue
        ran = by_line.get(line_key, [])
        for r in ran:
            made_codes[r["code"]] += r["litres"]
            total_actual += r["litres"]
            any_running = any_running or r["running"]
        actual_l = sum(r["litres"] for r in ran)
        still = any(r["running"] for r in ran)
        actual_txt = ", ".join(f"{_nice(r['name'])} {_t(r['litres'])}" for r in ran) or "nothing logged"
        if still:
            actual_txt += " (still running)"
        same = {j["code"] for j in planned} == {r["code"] for r in ran}
        if not planned and not ran:
            continue
        if not same:
            state = "red"
        elif still and actual_l < plan_l * (1 - OFF):
            state = "running"
        else:
            state = _state(plan_l, actual_l)
        add(1, m, state, plan_txt, actual_txt, f"{m}: planned {plan_txt} · ran {actual_txt}")

    for h in P.get("held_at_step5") or []:
        if h.get("reason_word") in ("no machine", "no time", None):
            continue
        got = made_codes.get(h["code"], 0.0)
        if got > 0 and not h.get("partial"):
            add(3, _nice(h["name"]), "red", f"held — {h['reason']}", f"made {_t(got)}",
                f"{_nice(h['name'])}: the plan held it ({h['reason']}) · the plant made {_t(got)}")

    plan_total = float(P.get("total_l") or 0)
    add(4, "Total made", "running" if any_running else _state(plan_total, total_actual), _t(plan_total),
        _t(total_actual), f"Total: planned {_t(plan_total)} · made {_t(total_actual)}")

    # the rooms tonight, and what left them today
    try:
        reader = TomorrowRunReader(CompanyContext(company.code))
        rooms = tuple(C.ROOM_LIMIT_L)
        stock = reader.room_stock(rooms)
        lp = {c: float(v.get("lp") or 0) for c, v in (plan.inputs.get("items") or {}).items()}
        flows = left_per_day(reader.room_flows(rooms, day, day), lp, C.PRODUCTION_ROOM, C.TRUCK_ROOM)
    except Exception as e:  # SAP down: the machine rows still stand
        add(5, "Rooms", "no record", "—", f"SAP could not be read ({e})", f"Rooms: SAP could not be read ({e})")
        stock, flows, lp = None, {}, {}

    if stock is not None:
        have = P.get("have") or {}
        home_l = defaultdict(float)
        for j in P.get("jobs") or []:
            home_l[(j.get("room") or C.PRODUCTION_ROOM).split(" then ")[0]] += float(j.get("placed_l") or 0)
        for room, v in (have.get("rooms") or {}).items():
            expected = float(v.get("standing_tomorrow_l") or 0) + home_l.get(room, 0.0)
            actual = sum(q * lp.get(c, 0) for c, q in (stock.get(room) or {}).items())
            add(5, room, _state(expected, actual), _t(expected), _t(actual),
                f"{room} tonight: the plan expected {_t(expected)} · it holds {_t(actual)}")
        today = flows.get(day.isoformat())
        if today is not None:
            planned_left = float(have.get("left_avg_l") or 0)
            add(6, "Left the rooms", _state(planned_left, today["left_l"]), _t(planned_left), _t(today["left_l"]),
                f"Left the rooms today: plan {_t(planned_left)} · measured {_t(today['left_l'])}")

    n_picks = plan.picks.filter(cleared_at__isnull=True).count()
    add(12, "Picks", "green" if n_picks else "red", "veerji picks what runs first",
        f"{n_picks} pick{'s' if n_picks != 1 else ''} today",
        f"Picks: {n_picks} pick{'s' if n_picks != 1 else ''} today" if n_picks else "Picks: nobody tapped a pick today")
    return rows
