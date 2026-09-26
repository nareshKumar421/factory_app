"""From the planning sheet to a machine plan and a pending list.

The steps are the board's, in its order and with its numbers:

1. **The planning sheet** — Net Req per SKU, the newest sheet in charge.
2. **Made since the sheet's date** — FG received into BH-PF, per SKU.
3. **To make** = Net Req − made, never below zero. Units decide priority.
4-5. **Can I even make this?** One SKU at a time, most units first: the
   smallest of the oil, the packaging and the room it can have. What the first
   SKU takes, the next one does not see.
6. **Final list** — what survived step 5.
7-12. **Which machine** — biggest job first; every machine that passes both
   filters is an option with its change and running time; the earliest finish
   is suggested; what does not fit the day waits, with the reason.

A **pick** ("run this first on that machine") puts the item first in line for
its materials and first on that machine, and everything after it is re-timed.
"""

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .. import constants as C
from . import items as I

EPS = 1e-9


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def clock(run_h: Optional[float]) -> Optional[str]:
    """Running hours from 7:30 as the time on the clock ("18:46", "04:50 +1 day")."""
    if run_h is None:
        return None
    start_h, start_m = (int(x) for x in C.DAY_STARTS.split(":"))
    minutes = math.floor(round(run_h * C.CLOCK_PER_RUN_H * 60, 6)) + start_h * 60 + start_m
    days, rest = divmod(minutes, 24 * 60)
    text = f"{rest // 60:02d}:{rest % 60:02d}"
    if days == 1:
        text += " +1 day"
    elif days > 1:
        text += f" +{days} days"
    return text


def _d(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _n0(value: float) -> str:
    return f"{round(value):,}"


def work_days(first: date, last: date) -> int:
    """Working days from ``first`` to ``last`` inclusive, Sundays off."""
    if not first or not last or last < first:
        return 0
    n, d = 0, first
    while d <= last:
        if d.weekday() != 6:
            n += 1
        d += timedelta(days=1)
    return n


def month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def sheet_machines(text: str) -> List[str]:
    """What the sheet's machine column means, in board machine names."""
    low = " ".join((text or "").lower().split())
    if not low or low in ("-", "manual", "na", "n/a"):
        return []
    out: List[str] = []
    for word, names in C.SHEET_MACHINE_WORDS:
        if word in low:
            for n in names:
                if n not in out:
                    out.append(n)
            # "clearpack 1" must not also read as "jp" etc.; the first family wins
            break
    return out


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


class Planner:
    def __init__(self, inputs: Dict[str, Any], picks=(), history=()):
        self.inputs = inputs
        self.picks = [p for p in picks if p.get("machine")]
        self.history = list(history)
        self.for_date = _d(inputs["for_date"])
        self.warnings: List[str] = []
        self.assumed: List[str] = []
        self._item_cache: Dict[str, Optional[I.Item]] = {}
        self.twin_of = {}
        self.twin_name = {}
        for first, second, name, when in C.CARTON_TWINS:
            self.twin_of[second] = first
            self.twin_name[first] = (name, when, second)

    # -- items --------------------------------------------------------------

    def item(self, code: str) -> Optional[I.Item]:
        if code not in self._item_cache:
            meta = (self.inputs.get("items") or {}).get(code) or {}
            self._item_cache[code] = I.describe(code, meta.get("name") or code, meta.get("lp"))
        return self._item_cache[code]

    def name(self, code: str) -> str:
        return ((self.inputs.get("items") or {}).get(code) or {}).get("name") or code

    def lp(self, code: str) -> float:
        return float(((self.inputs.get("items") or {}).get(code) or {}).get("lp") or 0)

    def primary(self, code: str) -> str:
        return self.twin_of.get(code, code)

    # -- step 1-3: the sheet, what was made, what is left -------------------

    def build_pile(self):
        sheet = self.inputs.get("sheet") or {}
        aliases = self.inputs.get("aliases") or {}
        made_pcs = defaultdict(float)
        for code, pcs in ((self.inputs.get("made") or {}).get("pcs") or {}).items():
            made_pcs[self.primary(code)] += float(pcs or 0)

        rows: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        no_code, mart_codes, wrong_size = [], [], []
        sheet_net = 0.0

        for line in sheet.get("lines") or []:
            net = float(line.get("net_l") or 0)
            sheet_net += net
            code = (line.get("code") or "").strip().upper()
            sheet_name = (line.get("name") or "").strip()
            if not code:
                no_code.append({"name": sheet_name, "net_l": net, "row": line.get("row")})
                continue

            planned = code
            via = None
            alias = aliases.get(code)
            if alias:
                planned = alias["planned_as"]
                mart_codes.append({
                    "sheet_code": code, "sheet_name": sheet_name,
                    "oil_name_of_that_code": self.name(code), "planned_as": planned,
                })
                via = (
                    f'on the sheet as {code} "{sheet_name}" — {alias.get("book") or "another book"}\'s code; '
                    f"in Oil {code} is {self.name(code)}, so it is planned as {planned} ({alias.get('basis') or 'the recipe map'})"
                )
            else:
                said = I.sheet_size_litres(sheet_name)
                it = self.item(code)
                if said and it and it.lp and abs(said - it.lp / it.bottles) > 0.011 and abs(said - it.lp) > 0.011:
                    wrong_size.append({"code": code, "sheet_name": sheet_name, "oil_name": it.name, "net_l": net})
                    continue

            planned = self.primary(planned)
            if planned in self.twin_name:
                twin = self.twin_name[planned]
                via = via or (
                    f"{twin[0]}: two carton codes are one bottle — stock and orders of both "
                    f"counted together (your rule, {twin[1]})"
                )
            if planned not in rows:
                order.append(planned)
                rows[planned] = {
                    "code": planned, "sheet_codes": [], "sheet_names": [],
                    "plan_l": 0.0, "ecom_l": 0.0, "stock_l": 0.0, "net_l": 0.0,
                    "sheet_machine": [], "via": via, "rows": [],
                }
            r = rows[planned]
            r["sheet_codes"].append(code)
            r["sheet_names"].append(sheet_name)
            r["rows"].append(line.get("row"))
            for k in ("plan_l", "ecom_l", "stock_l", "net_l"):
                r[k] += float(line.get(k) or 0)
            m = (line.get("machine") or "").strip()
            if m and m not in r["sheet_machine"]:
                r["sheet_machine"].append(m)
            if via and not r["via"]:
                r["via"] = via

        pile, no_volume = [], []
        for code in order:
            r = rows[code]
            it = self.item(code)
            if it is None:
                no_volume.append({"code": code, "name": self.name(code) or r["sheet_names"][0], "net_l": r["net_l"]})
                continue
            need = max(r["net_l"], 0.0)
            made_l = made_pcs.get(code, 0.0) * it.lp
            left = max(need - made_l, 0.0)
            units = math.ceil(left / it.lp - 1e-6) if left > 0 else 0
            said = []
            for s in r["sheet_machine"]:
                for m in sheet_machines(s):
                    if m not in said:
                        said.append(m)
            pile.append({
                "code": code, "name": it.name, "pack": it.pack, "type": it.type, "lp": it.lp,
                "oil": it.oil,
                "plan_l": r["plan_l"], "ecom_l": r["ecom_l"], "stock_l": r["stock_l"],
                "net_l": r["net_l"], "need_l": need, "made_l": made_l, "left_l": left,
                "total": units,
                "sheet_machine": r["sheet_machine"] or ["-"], "sheet_says": said,
                "board_machines": I.machines_for(it),
                "sheet_codes": r["sheet_codes"], "sheet_names": r["sheet_names"],
                "via": r["via"],
            })

        on_sheet = {p["code"] for p in pile}
        made_not_on_sheet = []
        for code, pcs in sorted(made_pcs.items(), key=lambda kv: -kv[1] * (self.lp(kv[0]) or 0)):
            if code in on_sheet or pcs <= 0:
                continue
            litres = pcs * self.lp(code)
            if litres > 0:
                made_not_on_sheet.append({"code": code, "name": self.name(code), "litres": litres})

        self.pile = pile
        self.sheet_facts = {
            "sheet_net_l": sheet_net,
            "no_code": no_code,
            "mart_codes": mart_codes,
            "wrong_size": wrong_size,
            "no_volume": no_volume,
            "made_not_on_sheet": made_not_on_sheet,
        }

    # -- rooms ---------------------------------------------------------------

    def build_rooms(self):
        stock = self.inputs.get("stock") or {}
        days = self.inputs.get("left_days") or []
        left_avg = sum(d["left_l"] for d in days) / len(days) if days else 0.0
        truck_avg = sum(d.get("truck_l") or 0 for d in days) / len(days) if days else 0.0
        rooms = {}
        for room, limit in C.ROOM_LIMIT_L.items():
            per_item = stock.get(room) or {}
            pcs = sum(float(v or 0) for v in per_item.values())
            litres = sum(float(v or 0) * self.lp(c) for c, v in per_item.items())
            if room == C.TRUCK_ROOM:
                left = truck_avg
                basis = f"{room} sends only its own trucks at the gate: the last {len(days)} days' average ({_n0(truck_avg)} L)"
            else:
                left = max(left_avg - truck_avg, 0.0)
                basis = (
                    f"what left both rooms in a day, the average of the last {len(days)} working days "
                    f"({_n0(left_avg)} L), minus {C.TRUCK_ROOM}'s own trucks ({_n0(truck_avg)} L): "
                    "the transfers to Gupta leave from here"
                )
            standing = litres - left
            rooms[room] = {
                "limit_l": limit,
                "stock_tonight_l": litres,
                "stock_tonight_pcs": pcs,
                "left_tomorrow_l": left,
                "left_basis": basis,
                "standing_tomorrow_l": standing,
                "free_l": max(limit - standing, 0.0),
                "skus": sum(1 for v in per_item.values() if float(v or 0) > 0),
            }
        self.rooms = rooms
        self.left_days = days
        self.left_avg = left_avg
        # Where each SKU's new stock goes: the room it already sits in.
        self.home = {}
        for room in C.ROOM_LIMIT_L:
            for code, pcs in (stock.get(room) or {}).items():
                p = self.primary(code)
                if float(pcs or 0) > self.home.get(p, (None, 0))[1]:
                    self.home[p] = (room, float(pcs or 0))

    # -- step 4-6: the material check ---------------------------------------

    def material_check(self):
        oil_pool, oil_meta = {}, {}
        for row in self.inputs.get("oil") or []:
            rm = row["rm"]
            oil_pool[rm] = oil_pool.get(rm, 0.0) + float(row.get("litres") or 0)
            oil_meta[rm] = row
        pm_pool = {code: float(v.get("have") or 0) for code, v in (self.inputs.get("packaging") or {}).items()}
        pm_names = {code: v.get("name") or code for code, v in (self.inputs.get("packaging") or {}).items()}
        room_pool = {r: v["free_l"] for r, v in self.rooms.items()}
        self.oil_start = dict(oil_pool)
        self.pm_start = dict(pm_pool)
        self.room_start = dict(room_pool)
        self.oil_meta = oil_meta
        self.pm_names = pm_names
        self.took = defaultdict(list)       # material code -> [(job id, pcs)]

        picked_codes = []
        for p in self.picks:
            code = (p.get("job") or "").split("-order")[0]
            if code and code not in picked_codes:
                picked_codes.append(code)
        self.picked_codes = picked_codes
        queue = sorted(
            [p for p in self.pile if p["total"] > 0],
            key=lambda p: (0 if p["code"] in picked_codes else 1,
                           picked_codes.index(p["code"]) if p["code"] in picked_codes else 0,
                           -p["total"], p["code"]),
        )

        bom = self.inputs.get("bom") or {}
        self.jobs, self.held = [], []
        for p in queue:
            it = self.item(p["code"])
            want = p["total"]
            base = {"code": p["code"], "name": p["name"], "kind": "order", "want_pcs": want,
                    "pack": p["pack"], "type": p["type"], "lp": p["lp"], "via": p["via"]}
            if not p["board_machines"]:
                reason = I.no_machine_reason(it)
                self.held.append({**base, "reason": reason, "reason_word": "no machine",
                                  "reason_detail": reason, "partial": False,
                                  "tons_wanted": want * it.lp / 1000})
                continue
            lines = bom.get(p["code"]) or bom.get(p["code"].upper())
            if not lines:
                reason = "no production recipe in SAP (OITT) for it"
                self.held.append({**base, "reason": "no recipe: " + reason, "reason_word": "no recipe",
                                  "reason_detail": reason, "partial": False,
                                  "tons_wanted": want * it.lp / 1000})
                continue

            limits = {"oil": [], "packaging": []}
            caps = []           # (cap pcs, word, detail, material dict)
            first_caps = []
            for ln in lines:
                per = float(ln.get("per_piece") or 0)
                if per <= 0:
                    continue
                if ln["kind"] == "oil":
                    have = oil_pool.get(ln["code"], 0.0)
                    meta = oil_meta.get(ln["code"])
                    source = self._oil_source(meta)
                    limits["oil"].append({"rm": ln["code"], "name": ln.get("name") or ln["code"],
                                          "have_l": have, "per_piece_l": per, "source": source})
                    cap = math.floor(have / per + EPS)
                    detail = (
                        f"{ln.get('name') or ln['code']} — {_n0(have)} L on hand ({source}), enough for {_n0(cap)} pcs"
                        if meta else
                        f"{ln.get('name') or ln['code']} ({ln['code']}) — not on the Raw Material Stock page, so none"
                    )
                    caps.append((cap, "no oil", detail, {"kind": "oil", "code": ln["code"],
                                                          "name": ln.get("name") or ln["code"],
                                                          "per": per, "unit": "L"}))
                    first_caps.append(math.floor(self.oil_start.get(ln["code"], 0.0) / per + EPS))
                elif ln["kind"] == "packaging":
                    have = pm_pool.get(ln["code"], 0.0)
                    name = ln.get("name") or pm_names.get(ln["code"]) or ln["code"]
                    limits["packaging"].append({"pm": ln["code"], "name": name, "have": have, "per_piece": per})
                    cap = math.floor(have / per + EPS)
                    detail = (
                        f"{name} ({ln['code']}) — {_n0(have)} on hand on the Packing Material dashboard "
                        f"({' / '.join(C.PACKAGING_ROOMS)}), enough for {_n0(cap)} pcs"
                    )
                    caps.append((cap, I.packaging_word(name), detail,
                                 {"kind": "packaging", "code": ln["code"], "name": name, "per": per,
                                  "unit": ln.get("uom") or "pcs"}))
                    first_caps.append(math.floor(self.pm_start.get(ln["code"], 0.0) / per + EPS))

            home = self.home.get(p["code"], (C.PRODUCTION_ROOM, 0))[0]
            other = next(r for r in C.ROOM_LIMIT_L if r != home)
            room_free = room_pool[home] + room_pool[other]
            room_cap = math.floor(room_free / it.lp + EPS)
            room_detail = (
                f"{home} has {_n0(room_pool[home])} L and {other} {_n0(room_pool[other])} L free tomorrow, "
                f"enough for {_n0(room_cap)} pcs"
            )
            caps.append((room_cap, "no room", room_detail, {"kind": "room", "code": "room", "name": "room",
                                                            "per": it.lp, "unit": "L"}))
            first_caps.append(math.floor((self.room_start[home] + self.room_start[other]) / it.lp + EPS))

            oil_cap = min([c[0] for c in caps if c[3]["kind"] == "oil"], default=None)
            pm_cap = min([c[0] for c in caps if c[3]["kind"] == "packaging"], default=None)
            limits.update({"oil_cap": oil_cap, "packaging_cap": pm_cap, "room_cap": room_cap})

            make = max(0, min([want] + [c[0] for c in caps]))
            could = max(0, min([want] + first_caps))
            # When several run out together, the one there is none of at all is
            # the reason; then room; then a material that is merely short.
            binder = sorted(
                (c for c in caps if c[0] <= make and make < want),
                key=lambda c: 1 if c[3]["kind"] == "room"
                else 0 if self._pool_left(c[3], oil_pool, pm_pool, room_pool) <= EPS else 2,
            )
            binding = binder[0] if binder else None

            # take it
            for ln in lines:
                per = float(ln.get("per_piece") or 0)
                if per <= 0 or make <= 0:
                    continue
                if ln["kind"] == "oil":
                    oil_pool[ln["code"]] = oil_pool.get(ln["code"], 0.0) - make * per
                    self.took[ln["code"]].append((p["code"], make))
                elif ln["kind"] == "packaging":
                    pm_pool[ln["code"]] = pm_pool.get(ln["code"], 0.0) - make * per
                    self.took[ln["code"]].append((p["code"], make))
            need_l = make * it.lp
            rooms_used = []
            for r in (home, other):
                take = min(room_pool[r], need_l)
                if take > 0:
                    room_pool[r] -= take
                    need_l -= take
                    rooms_used.append(r)
            if make > 0:
                self.took["room"].append((p["code"], make))

            material = None
            if binding:
                mt = binding[3]
                material = {
                    "kind": mt["kind"], "code": mt["code"], "name": mt["name"], "unit": mt["unit"],
                    "went_to": [{"code": c, "name": self.name(c), "picked": c in picked_codes}
                                for c, _q in self.took.get(mt["code"], []) if c != p["code"]],
                    "left": max(self._pool_left(mt, oil_pool, pm_pool, room_pool), 0.0),
                }

            job = {
                **base, "id": f"{p['code']}-order", "oil": it.oil,
                "make_pcs": make, "order_pcs": make, "buffer_pcs": 0,
                "short_pcs": want - make, "could_pcs": could,
                "limits": limits, "litres_per_piece": it.lp,
                "binder": [f"{binding[1]}: {binding[2]}"] if binding else [],
                "reason_word": binding[1] if binding else None,
                "reason_detail": binding[2] if binding else None,
                "material": material,
                "room": " then ".join(rooms_used) if rooms_used else home,
                "pile": {"need_l": p["need_l"], "made_l": p["made_l"], "left_l": p["left_l"],
                         "sheet_machine": p["sheet_machine"]},
                "board_machines": p["board_machines"],
            }
            if make > 0:
                self.jobs.append(job)
            if make < want:
                self.held.append({
                    **base, "id": job["id"], "limits": limits, "binder": job["binder"],
                    "reason": job["binder"][0] if job["binder"] else "",
                    "reason_word": job["reason_word"], "reason_detail": job["reason_detail"],
                    "partial": make > 0, "tons_wanted": (want - make) * it.lp / 1000,
                    "want_pcs": want - make, "material": material, "could_pcs": could,
                    "board_machines": p["board_machines"],
                })

        self.oil_left = oil_pool
        self.jobs.sort(key=lambda j: (-j["make_pcs"], -j["want_pcs"], j["code"]))

    def _pool_left(self, mt, oil_pool, pm_pool, room_pool):
        if mt["kind"] == "oil":
            return oil_pool.get(mt["code"], 0.0)
        if mt["kind"] == "packaging":
            return pm_pool.get(mt["code"], 0.0)
        return sum(room_pool.values())

    @staticmethod
    def _oil_source(meta):
        if not meta:
            return "not on the Raw Material Stock page"
        who = f" by {meta['set_by']}" if meta.get("set_by") else ""
        return f"Raw Material Stock page, {meta.get('warehouse') or C.OIL_ROOM}, counted {meta.get('as_of') or '—'}{who}"

    # -- step 7-12: the machines --------------------------------------------

    def running_item(self, machine) -> Optional[I.Item]:
        rn = (self.inputs.get("running_now") or {}).get(machine)
        if not rn or not rn.get("code"):
            return None
        meta = (self.inputs.get("items") or {}).get(rn["code"]) or {}
        return I.describe(rn["code"], meta.get("name") or rn.get("name") or rn["code"], meta.get("lp") or 1.0)

    def place(self):
        self.state = {m: {"t": 0.0, "last": self.running_item(m), "jobs": []} for m in C.MACHINES}
        self.remaining = {j["id"]: j["make_pcs"] for j in self.jobs}
        self.placed = defaultdict(dict)         # job id -> machine -> pcs
        self.no_time = {}
        job_by_id = {j["id"]: j for j in self.jobs}

        # picks first, each on its own machine, from 7:30
        self.pick_status = {}
        for p in self.picks:
            m, jid = p["machine"], p.get("job") or ""
            if jid == "other" or m not in self.state:
                continue
            job = job_by_id.get(jid)
            if not job or m not in job["board_machines"] or self.state[m]["jobs"]:
                self.pick_status[m] = False
                continue
            self._put(job, [(m, self.remaining[jid])])
            self.pick_status[m] = bool(self.placed[jid].get(m))

        for job in self.jobs:
            jid = job["id"]
            job["options"] = self.options(job)
            if self.remaining[jid] <= 0:
                job["chosen"] = "picked"
                job["suggested"] = job["options"][0]["id"] if job["options"] else None
                continue
            best = min(job["options"], key=lambda o: (o["finish_h"], len(o["machines"]))) if job["options"] else None
            job["suggested"] = best["id"] if best else None
            job["chosen"] = job["suggested"]
            if best:
                self._put(job, [(part["machine"], part["pcs"]) for part in best["machines"]], option=best)
            if self.remaining[jid] > 0:
                self.no_time[jid] = self.remaining[jid]

        for job in self.jobs:
            placed = sum(self.placed[job["id"]].values())
            job["placed_pcs"] = placed
            job["placed_l"] = placed * job["lp"]
            job["leftover_pcs"] = job["make_pcs"] - placed
            job["placed_on"] = [m for m, q in self.placed[job["id"]].items() if q > 0]
            job["picked_on"] = [p["machine"] for p in self.picks if p.get("job") == job["id"]]
            job["chosen_by"] = "picked" if job["picked_on"] else "suggested"

    def options(self, job) -> List[Dict[str, Any]]:
        it = self.item(job["code"])
        pcs = self.remaining[job["id"]]
        singles = []
        for m in job["board_machines"]:
            st = self.state[m]
            sp = I.speed(m, it)
            ch, label = I.change(m, st["last"], it)
            run = pcs / sp if pcs > 0 else 0.0
            finish = st["t"] + ch + run
            singles.append({
                "id": m, "label": m, "split": False,
                "machines": [{"machine": m, "pcs": pcs, "change_h": ch, "change": label, "run_h": run,
                              "speed": sp, "start_h": st["t"]}],
                "hours": ch + run, "finish_h": finish,
            })
        singles.sort(key=lambda o: o["finish_h"])
        out = list(singles)
        if len(singles) >= 2 and pcs > 1:
            a, b = singles[0]["machines"][0], singles[1]["machines"][0]
            x = (b["start_h"] + b["change_h"] - a["start_h"] - a["change_h"] + pcs / b["speed"]) / (
                1 / a["speed"] + 1 / b["speed"])
            xa = int(x)
            if 0 < xa < pcs:
                pa = {**a, "pcs": xa, "run_h": xa / a["speed"]}
                pb = {**b, "pcs": pcs - xa, "run_h": (pcs - xa) / b["speed"]}
                fa = pa["start_h"] + pa["change_h"] + pa["run_h"]
                fb = pb["start_h"] + pb["change_h"] + pb["run_h"]
                out.append({
                    "id": f"split:{a['machine']}+{b['machine']}", "label": f"{a['machine']} + {b['machine']}",
                    "split": True, "machines": [pa, pb],
                    "hours": max(pa["change_h"] + pa["run_h"], pb["change_h"] + pb["run_h"]),
                    "finish_h": max(fa, fb),
                })
        for o in out:
            o["finish"] = clock(o["finish_h"])
            o["fits_session"] = o["finish_h"] <= C.SESSION_RUN_H + EPS
            o["fits_two_sessions"] = o["finish_h"] <= C.SECOND_SESSION_RUN_H + EPS
            o["session"] = 1 if o["fits_session"] else 2 if o["fits_two_sessions"] else None
            o["pcs_by_end_of_session"] = sum(
                min(part["pcs"], self._room_in_day(part)) for part in o["machines"])
        return out

    def _room_in_day(self, part) -> int:
        free_h = C.SESSION_RUN_H - part["start_h"] - part["change_h"]
        return max(0, math.floor(free_h * part["speed"] + EPS))

    def _put(self, job, parts, option=None):
        it = self.item(job["code"])
        jid = job["id"]
        for m, pcs in parts:
            if self.remaining[jid] <= 0:
                break
            st = self.state[m]
            sp = I.speed(m, it)
            ch, label = I.change(m, st["last"], it)
            fit = max(0, math.floor((C.SESSION_RUN_H - st["t"] - ch) * sp + EPS))
            q = min(pcs, self.remaining[jid], fit)
            if q <= 0:
                continue
            run = q / sp
            start = st["t"]
            st["t"] = start + ch + run
            st["last"] = it
            st["jobs"].append({
                "id": jid, "code": job["code"], "name": job["name"], "kind": job["kind"],
                "pcs": q, "litres": q * it.lp, "change_h": ch, "change": label, "run_h": run,
                "speed": sp, "start_h": start, "finish_h": st["t"],
                "start": clock(start), "finish": clock(st["t"]),
            })
            self.remaining[jid] -= q
            self.placed[jid][m] = self.placed[jid].get(m, 0) + q

    # -- per machine: everything it could run, ranked ------------------------

    def menus(self):
        job_by_code = {j["code"]: j for j in self.jobs}
        pile_by_code = {p["code"]: p for p in self.pile}
        picks_by_m = {p["machine"]: p for p in self.picks}
        menus = {}
        for m in C.MACHINES:
            last = self.running_item(m)
            rows = []
            for job in self.jobs:
                if m not in job["board_machines"]:
                    continue
                rows.append(self._menu_row(m, last, job, pile_by_code[job["code"]]))
            # Held for its oil or its packaging: a pick can put it first in line
            # for that. Held for room, a recipe or a machine, a pick changes nothing.
            for h in self.held:
                if h["code"] in job_by_code or m not in (h.get("board_machines") or []):
                    continue
                if ((h.get("material") or {}).get("kind")) not in ("oil", "packaging"):
                    continue
                rows.append(self._held_row(m, last, h, pile_by_code[h["code"]]))

            def key(r):
                if r.get("held"):
                    return (2 if r["can_pick"] else 3, -(r.get("could_l") or 0), -r["want_l"], r["code"])
                if r["on_plan_here_l"] > 0:
                    return (0, -r["on_plan_here_l"], 0, r["code"])
                return (1, -r["fresh_l"], -r["want_l"], r["code"])

            rows.sort(key=key)
            for i, r in enumerate(rows, 1):
                r["rank"] = i
            top3 = [r["job"] for r in rows
                    if not r.get("held") and r["can_pick"] and (r["on_plan_here_l"] > 0 or r["fresh_l"] > 0.5)][:3]
            pick = picks_by_m.get(m)
            picked = None
            if pick:
                row = next((r for r in rows if r["job"] == pick.get("job")), None)
                picked = {
                    "job": pick.get("job"), "name": row["name"] if row else (pick.get("other") or pick.get("name") or ""),
                    # the rank it had when it was offered, not the one it has now it runs first
                    "rank": pick.get("rank") or (row["rank"] if row else None),
                    "why": pick.get("why") or "", "by": pick.get("by") or "",
                    "at": pick.get("at"), "other": pick.get("other") or "",
                    "on_plan": bool(self.pick_status.get(m)),
                }
                if row:
                    row["picked_here"] = True
            menus[m] = {"rows": rows, "top3": top3, "picked": picked}
        self.menu = menus

    def _first(self, m, last, it, want_pcs):
        sp = I.speed(m, it)
        ch, label = I.change(m, last, it)
        cap = max(0, math.floor((C.SESSION_RUN_H - ch) * sp + EPS))
        first_pcs = min(want_pcs, cap)
        fits = want_pcs > 0 and ch + want_pcs / sp <= C.SESSION_RUN_H + EPS
        return sp, ch, label, first_pcs, fits, (clock(ch + want_pcs / sp) if fits else None)

    def _common(self, m, p):
        says = p["sheet_says"]
        return {
            "code": p["code"], "name": p["name"], "pack": p["pack"], "type": p["type"], "via": p["via"],
            "sheet_left_l": p["left_l"], "sheet_says": says, "sheet_says_here": m in says,
            "picked_here": False, "small": False,
        }

    def _menu_row(self, m, last, job, p):
        it = self.item(job["code"])
        sp, ch, label, first_pcs, fits, first_finish = self._first(m, last, it, job["make_pcs"])
        here = self.placed[job["id"]].get(m, 0)
        placed_all = sum(self.placed[job["id"]].values())
        waits = (job["make_pcs"] - placed_all) * it.lp
        on_here = next((x for x in self.state[m]["jobs"] if x["id"] == job["id"]), None)
        first_l = first_pcs * it.lp
        more = None
        if job["material"]:
            more_pcs = max(0, job["could_pcs"] - job["make_pcs"])
            more = more_pcs * it.lp if more_pcs else None
        return {
            **self._common(m, p), "job": job["id"], "held": False,
            "want_l": job["make_pcs"] * it.lp, "waits_l": waits, "on_plan_here_l": here * it.lp,
            "made_elsewhere_on": [x for x, q in self.placed[job["id"]].items() if q > 0 and x != m],
            "change_h": ch, "change": label, "speed": sp,
            "start": on_here["start"] if on_here else None, "finish": on_here["finish"] if on_here else None,
            "can_pick": True, "first_l": first_l, "first_fits": fits, "first_finish": first_finish,
            "fresh_l": min(first_l, waits), "material": job["material"], "more_if_first_l": more,
        }

    def _held_row(self, m, last, h, p):
        it = self.item(h["code"])
        want = p["total"]
        could = h.get("could_pcs") or 0
        sp, ch, label, first_pcs, _fits, _ff = self._first(m, last, it, could)
        can = could > 0
        blocked = None
        if not can:
            mt = h.get("material") or {}
            start = (self.oil_start if mt.get("kind") == "oil" else self.pm_start).get(mt.get("code"), 0.0)
            blocked = "none of it exists tonight" if start <= EPS else "nothing of it can be made tonight"
        return {
            **self._common(m, p), "job": h["id"], "held": True,
            "want_l": want * it.lp, "waits_l": want * it.lp, "on_plan_here_l": 0.0,
            "made_elsewhere_on": [], "change_h": ch, "change": label, "speed": sp,
            "start": None, "finish": None, "can_pick": can, "first_l": first_pcs * it.lp,
            "first_fits": None, "first_finish": None, "fresh_l": 0.0, "could_l": could * it.lp,
            "material": h.get("material"), "reason_word": h["reason_word"], "reason_detail": h["reason_detail"],
            "blocked": blocked,
        }

    # -- the learning ---------------------------------------------------------

    def learning(self):
        picks = [p for p in self.history if p.get("job") and p.get("job") != ""]
        n = len(picks)
        return {
            "picks": picks,
            "n": n,
            "was_first": sum(1 for p in picks if p.get("rank") == 1),
            "in_top3": sum(1 for p in picks if p.get("job") in [t.get("job") for t in (p.get("top3") or [])]),
        }

    # -- assemble -------------------------------------------------------------

    def build(self) -> Dict[str, Any]:
        self.build_pile()
        self.build_rooms()
        self.material_check()
        self.place()
        self.menus()

        pending = []
        for h in self.held:
            it = self.item(h["code"])
            pending.append({
                "code": h["code"], "name": h["name"], "pcs": h["want_pcs"], "kind": "order",
                "reason": h["reason"], "partial": h["partial"], "lp": it.lp,
                "reason_word": h["reason_word"], "reason_detail": h["reason_detail"],
                "tons": h["want_pcs"] * it.lp / 1000, "pack": h["pack"], "type": h["type"], "via": h.get("via"),
            })
        for job in self.jobs:
            left = job["leftover_pcs"]
            if left > 0:
                pending.append({
                    "code": job["code"], "name": job["name"], "pcs": left, "kind": "order",
                    "reason": "no free time", "partial": job["placed_pcs"] > 0, "lp": job["lp"],
                    "reason_word": "no time", "reason_detail": "the machines that can make it are full for the day",
                    "tons": left * job["lp"] / 1000, "pack": job["pack"], "type": job["type"], "via": job.get("via"),
                })

        machines = {}
        for m in C.MACHINES:
            st = self.state[m]
            rn = (self.inputs.get("running_now") or {}).get(m) or {
                "code": None, "name": "no run in the MES log this week", "oil": None,
                "basis": "unknown — a change is counted"}
            machines[m] = {
                "running_now": rn,
                "jobs": [{k: v for k, v in x.items() if k not in ("start_h", "finish_h", "speed", "id")}
                         for x in st["jobs"]],
                "hours_used": st["t"],
                "finish": clock(st["t"]) if st["jobs"] else None,
                "litres": sum(x["litres"] for x in st["jobs"]),
                "pcs": sum(x["pcs"] for x in st["jobs"]),
            }
        total = sum(v["litres"] for v in machines.values())

        sheet = self.inputs.get("sheet") or {}
        stock_date = _d(sheet.get("stock_date"))
        from_date = _d(sheet.get("from_date")) or (stock_date + timedelta(days=1) if stock_date else None)
        end = month_end(self.for_date)
        wd = work_days(self.for_date, end)
        need = sum(p["need_l"] for p in self.pile)
        made_all = sum(p["made_l"] for p in self.pile)
        left = sum(p["left_l"] for p in self.pile)
        board_vs_sheet = sorted(
            ({"code": p["code"], "name": p["name"], "sheet": p["sheet_machine"],
              "board": p["board_machines"], "left_l": p["left_l"]}
             for p in self.pile
             if p["sheet_says"] and not set(p["sheet_says"]) & set(p["board_machines"]) and p["left_l"] > 0),
            key=lambda x: -x["left_l"],
        )
        made_window = self.inputs.get("made") or {}

        to_make = [p for p in self.pile if p["total"] > 0]
        final_pcs = sum(j["make_pcs"] for j in self.jobs)
        final_l = sum(j["make_pcs"] * j["lp"] for j in self.jobs)

        oil_rows = []
        for rm, start in sorted(self.oil_start.items(), key=lambda kv: -kv[1]):
            meta = self.oil_meta.get(rm) or {}
            oil_rows.append({"rm": rm, "name": meta.get("name") or rm, "have_l": start,
                             "left_l": self.oil_left.get(rm, start), "source": self._oil_source(meta)})

        self._assumptions(sheet, from_date, stock_date, need, made_all)

        return {
            "for_date": self.for_date.isoformat(),
            "run_at": self.inputs.get("read_at"),
            "frozen": True,
            "clock": {"day_starts": C.DAY_STARTS, "session_ends": C.SESSION_ENDS, "read_at": C.READ_AT,
                      "note": "the plant day starts 7:30 am; the apps are read once, at 7 pm the evening "
                              "before, and that run stands for the next day"},
            "rules": {
                "room_limit_l": dict(C.ROOM_LIMIT_L), "pm_rooms": list(C.PACKAGING_ROOMS),
                "start": C.DAY_STARTS, "session_run_h": C.SESSION_RUN_H,
                "second_session_run_h": C.SECOND_SESSION_RUN_H, "target_l": C.TARGET_L, "good_l": C.GOOD_L,
                "min_run_h": 0.0,
                "min_run_basis": "none — every job is an option however small, veerji chooses (Daman, 22 Sept 2026)",
            },
            "sheet": {
                "id": sheet.get("id"), "file": sheet.get("file"), "tab": sheet.get("tab"),
                "title": sheet.get("title"),
                "stock_date": stock_date.isoformat() if stock_date else None,
                "from_date": from_date.isoformat() if from_date else None,
                "date_basis": sheet.get("date_basis"), "put_in_at": sheet.get("put_in_at"),
                "put_in_by": sheet.get("put_in_by"),
                "month_end": end.isoformat(), "work_days_left": wd,
                "sheet_net_l": self.sheet_facts["sheet_net_l"], "need_l": need, "made_l": made_all,
                "left_l": left, "per_day_needed_l": left / wd if wd else None,
                "made_from": made_window.get("from"), "made_to": made_window.get("to"),
                "made_not_on_sheet": self.sheet_facts["made_not_on_sheet"],
                "mart_codes": self.sheet_facts["mart_codes"], "no_code": self.sheet_facts["no_code"],
                "wrong_size": self.sheet_facts["wrong_size"], "no_volume": self.sheet_facts["no_volume"],
                "sheet_vs_board": board_vs_sheet,
            },
            "pile": {"skus": len(to_make), "pcs": sum(p["total"] for p in to_make),
                     "litres": sum(p["left_l"] for p in to_make), "rows": self.pile},
            "have": {"rooms": self.rooms, "left_days": self.left_days, "left_avg_l": self.left_avg},
            "to_make": {"skus": len(to_make), "pcs": sum(p["total"] for p in to_make),
                        "litres": sum(p["left_l"] for p in to_make),
                        "rows": [{"code": p["code"], "name": p["name"], "pcs": p["total"], "lp": p["lp"]}
                                 for p in to_make]},
            "limits": {"oil": oil_rows, "packaging_items": len(self.pm_start), "rooms": self.rooms},
            "final_list": {"skus": len(self.jobs), "pcs": final_pcs, "litres": final_l},
            "jobs": self.jobs,
            "held_at_step5": self.held,
            "machines": machines,
            # the board's order: the database's JSON does not keep key order
            "machine_order": list(C.MACHINES),
            "machine_menu": self.menu,
            "learning": self.learning(),
            "total_l": total,
            "target": {"target_l": C.TARGET_L, "good_l": C.GOOD_L,
                       "pct_of_target": round(total / C.TARGET_L * 100, 3)},
            "pending": pending,
            "inputs": self.inputs.get("sources") or [],
            "assumed": self.assumed,
            "warnings": self.warnings,
            "picks": self.picks,
        }

    def _assumptions(self, sheet, from_date, stock_date, need, made_all):
        f = self.sheet_facts
        A, W = self.assumed, self.warnings
        if sheet.get("file"):
            A.append(
                f'What to make = the planning sheet "{sheet["file"]}"'
                f'{" (tab " + sheet["tab"] + ")" if sheet.get("tab") else ""}, {len(sheet.get("lines") or [])} lines: '
                f'Net Req {_n0(f["sheet_net_l"])} L — the month\'s plan + ecom − the stock in BH-PF and BH-BT on '
                f'{stock_date.isoformat() if stock_date else "its date"}. The newest sheet put in is in charge '
                "until a newer one is put in."
            )
        else:
            W.append("No planning sheet has been put in yet: there is nothing to plan.")
        A.append(
            f"Left tonight = Net Req − what the plant made since {from_date.isoformat() if from_date else '—'} "
            f"({_n0(made_all)} L of sheet items: finished goods received into BH-PF, SAP's goods receipts), "
            "per item, never below zero. Stock is not taken off again: the sheet already took off the stock "
            "of its own date. One bottle in two cartons counts as one item."
        )
        over = [p for p in self.pile if p["net_l"] < 0]
        if over:
            A.append(
                f"{len(over)} line{'s are' if len(over) > 1 else ' is'} already over-stocked on the sheet "
                "(Net Req below zero), nothing to make: "
                + ", ".join(f"{p['name']} {_n0(p['net_l'])} L" for p in over) + "."
            )
        if f["made_not_on_sheet"]:
            A.append(
                f"Made since {from_date.isoformat() if from_date else '—'} but not on the sheet (counts for nothing here): "
                + ", ".join(f"{x['name']} {_n0(x['litres'])} L" for x in f["made_not_on_sheet"]) + "."
            )
        for mc in f["mart_codes"]:
            A.append(f'{mc["sheet_code"]} "{mc["sheet_name"]}" is another book\'s code: planned as {mc["planned_as"]}.')
        for first, second, name, when in C.CARTON_TWINS:
            A.append(
                f"{name.upper()} — {first} or {second}: the same bottle in two cartons. Orders, stock and what "
                f"was made of both are counted together and planned under {first}; when its carton runs out the "
                f"rest is made under {second} (Daman, {when} 2026)."
            )
        A.append(
            "Oil I have = the Raw Material Stock page in the factory app (Warehouse → Raw Material Stock), "
            f"{C.OIL_ROOM}: the store's own count of each oil on the floor, with the date it was counted — the one "
            "place for oil. EXIM tanks and SAP drums are not read. An oil not on that page counts as none."
        )
        A.append(
            f"Packaging I have = the Packing Material dashboard, its four rooms {', '.join(C.PACKAGING_ROOMS)}, "
            "as read at 7 pm — and nothing else. BH-PP is dead junk, never counted."
        )
        A.append(
            "Room I have: room tomorrow = your limit (561 T / 377 T) − stock tonight + what leaves in a day − "
            f"what we make. What leaves in a day is measured off the two rooms themselves over the last {C.LEFT_DAYS} "
            "working days: stock the night before + received into BH-PF that day − stock that night. BH-BT is "
            "charged only its own trucks; the rest leaves from BH-PF. New production goes to the room where "
            "that SKU already sits, and spills into the other room when that one is full."
        )
        if board_diffs := [p for p in self.pile if p["sheet_says"] and not set(p["sheet_says"]) & set(p["board_machines"]) and p["left_l"] > 0]:
            A.append(
                f"{len(board_diffs)} item{'s' if len(board_diffs) > 1 else ''} where the sheet names a machine the "
                "board does not allow (the board decides until you change it): "
                + "; ".join(f"{p['name']} — sheet {' / '.join(p['sheet_machine'])}, board "
                            f"{' / '.join(p['board_machines']) or 'no machine'}" for p in board_diffs) + "."
            )
        A.append(
            "No minimum run: a job goes to every machine that passes both filters however small it is, and "
            "shows there as an option with its change time and finish; whether a small job is worth its "
            "changeover is your call on the page, not the computer's (Daman, 22 Sept 2026)."
        )
        logged = [m for m in C.MACHINES if ((self.inputs.get("running_now") or {}).get(m) or {}).get("code")]
        silent = [m for m in C.MACHINES if m not in logged]
        A.append(
            "What each machine is running now = its last run in the factory app's production log this week"
            f"{' (' + ', '.join(logged) + ')' if logged else ''}."
            + (f" {', '.join(silent)} {'have' if len(silent) > 1 else 'has'} no run logged, so a change is "
               "counted for the first job." if silent else "")
        )
        A.extend(C.ASSUMED_CHANGES)

        for x in f["no_code"]:
            W.append(f"Not planned — no item code on the sheet: {x['name']} ({_n0(x['net_l'])} L)")
        for x in f["wrong_size"]:
            W.append(
                f'Not planned — the sheet says {x["code"]} "{x["sheet_name"]}", but in Oil {x["code"]} is '
                f'{x["oil_name"]}: a different size. Put the Oil code on the sheet.'
            )
        for x in f["no_volume"]:
            W.append(f"Not planned — SAP holds no litres per piece for {x['code']} {x['name']} (OITM.SalPackUn).")
        counts = [_d(r.get("as_of")) for r in self.inputs.get("oil") or [] if r.get("as_of")]
        if counts:
            oldest = min(counts)
            read = _d(self.inputs.get("read_at")) or self.for_date
            age = (read - oldest).days
            if age > C.OIL_COUNT_STALE_DAYS:
                W.append(
                    f"Raw Material Stock page: the oldest oil count is dated {oldest.isoformat()}, {age} days "
                    "before tonight — the store should set today's quantities"
                )
        for s in self.inputs.get("sources") or []:
            if s.get("ok") is False:
                W.append(f"{s.get('input')}: could not be read ({s.get('error')})")


def build_plan(inputs: Dict[str, Any], picks=(), history=()) -> Dict[str, Any]:
    return Planner(inputs, picks, history).build()
