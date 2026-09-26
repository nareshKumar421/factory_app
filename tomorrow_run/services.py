"""Building the plan, picking on it, and putting in a planning sheet."""

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone

from company.models import Company

from . import constants as C
from .engine import build_plan
from .inputs import gather, next_working_day, user_name
from .models import MachinePick, PlanCheck, PlanningSheet, PlanningSheetLine, TomorrowPlan
from .sheet_parser import ParsedSheet, date_in_name, parse_sheet

logger = logging.getLogger(__name__)


class PickRefused(ValueError):
    """The pick names a machine or a job this plan does not have."""


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def live_picks(plan: TomorrowPlan):
    return plan.picks.filter(cleared_at__isnull=True).order_by("picked_at", "id")


def learning_history(company: Company):
    """Every pick still standing, newest first, with the three we offered."""
    rows = (
        MachinePick.objects.filter(plan__company=company, cleared_at__isnull=True)
        .exclude(job="")
        .select_related("plan")
        .order_by("-picked_at", "-id")[:200]
    )
    return [
        {"for_date": p.plan.for_date.isoformat(), "machine": p.machine, "job": p.job, "name": p.name,
         "rank": p.rank, "other": p.other, "why": p.why, "by": p.picked_by_name,
         "at": p.picked_at.isoformat() if p.picked_at else None, "top3": p.top3}
        for p in rows
    ]


def recompute(plan: TomorrowPlan) -> TomorrowPlan:
    """Re-run the engine over the frozen 7 pm read with the live picks."""
    picks = [p.as_pick() for p in live_picks(plan)]
    plan.plan = build_plan(plan.inputs, picks=picks, history=learning_history(plan.company))
    plan.save(update_fields=["plan", "updated_at"])
    return plan


def build(company: Company, *, user=None, for_date: Optional[date] = None, trigger=TomorrowPlan.TRIGGER_NIGHTLY,
          now=None) -> TomorrowPlan:
    """Read everything (the 7 pm read) and make the plan for the next working day.

    Reading again replaces the frozen numbers but keeps the day's picks: they
    are Gurvinder veerji's decisions, not arithmetic, and are re-applied to the
    new numbers.
    """
    now = now or timezone.localtime()
    for_date = for_date or next_working_day(now.date())
    inputs = gather(company, for_date=for_date, now=now)
    sheet_id = (inputs.get("sheet") or {}).get("id")
    with transaction.atomic():
        plan, _created = TomorrowPlan.objects.select_for_update().get_or_create(
            company=company, for_date=for_date,
            defaults={"read_at": now, "inputs": inputs, "plan": {}, "trigger": trigger, "built_by": user,
                      "sheet_id": sheet_id},
        )
        plan.read_at = now
        plan.inputs = inputs
        plan.trigger = trigger
        plan.built_by = user
        plan.sheet_id = sheet_id
        plan.save()
        recompute(plan)
        _stamp_picks(plan)
    return plan


def current(company: Company, today: Optional[date] = None) -> Optional[TomorrowPlan]:
    """The plan the page shows: the newest one, which is tomorrow's after 7 pm."""
    return TomorrowPlan.objects.filter(company=company).order_by("-for_date").first()


def _stamp_picks(plan: TomorrowPlan):
    """Record on each pick whether the plan could put it on its machine."""
    menus = plan.plan.get("machine_menu") or {}
    for p in live_picks(plan):
        picked = (menus.get(p.machine) or {}).get("picked") or {}
        on_plan = bool(picked.get("on_plan"))
        if p.on_plan != on_plan:
            p.on_plan = on_plan
            p.save(update_fields=["on_plan"])


def pick(plan: TomorrowPlan, *, machine: str, job: str, why: str, other: str = "", user=None) -> TomorrowPlan:
    """Save "run this first on that machine", or clear the machine's pick (``job=""``)."""
    if machine not in C.MACHINES:
        raise PickRefused(f"There is no machine called {machine!r} on the board.")
    menu = (plan.plan.get("machine_menu") or {}).get(machine) or {"rows": [], "top3": []}
    now = timezone.now()
    with transaction.atomic():
        TomorrowPlan.objects.select_for_update().filter(pk=plan.pk).first()
        live_picks(plan).filter(machine=machine).update(cleared_at=now, cleared_by=user)
        if job:
            if not (why or "").strip():
                raise PickRefused("Say why: that is what the plan learns from.")
            if job == "other":
                if not (other or "").strip():
                    raise PickRefused("Type what should run first.")
                row = None
            else:
                row = next((r for r in menu["rows"] if r["job"] == job), None)
                if row is None:
                    raise PickRefused(f"{job} is not something {machine} can run tomorrow.")
                if not row.get("can_pick"):
                    raise PickRefused(f"{row['name']} cannot be made tonight: {row.get('blocked') or 'nothing of it'}.")
            top3 = []
            for jid in menu.get("top3") or []:
                r = next((x for x in menu["rows"] if x["job"] == jid), None)
                if r:
                    top3.append({"job": jid, "name": r["name"], "rank": r["rank"],
                                 "on_plan_here_l": r["on_plan_here_l"], "first_l": r["first_l"]})
            MachinePick.objects.create(
                plan=plan, machine=machine, job=job, name=(row or {}).get("name") or other.strip(),
                other=(other or "").strip()[:120], why=why.strip(), rank=(row or {}).get("rank"), top3=top3,
                picked_by=user, picked_by_name=user_name(user),
            )
        recompute(plan)
        _stamp_picks(plan)
    return plan


# ---------------------------------------------------------------------------
# The planning sheet
# ---------------------------------------------------------------------------


def put_in_sheet(company: Company, upload, *, user=None, stock_date: Optional[date] = None) -> PlanningSheet:
    """Parse and store a planning sheet; it is in charge from now on."""
    parsed: ParsedSheet = parse_sheet(upload)
    upload.seek(0)
    basis = "entered when it was put in"
    if stock_date is None:
        stock_date = date_in_name(getattr(upload, "name", ""))
        basis = "the date in its file name"
    if stock_date is None:
        raise ValueError(
            "The file name carries no date (e.g. 19.09.2026), so say which day's stock the sheet took off."
        )
    with transaction.atomic():
        sheet = PlanningSheet.objects.create(
            company=company, file=upload, file_name=getattr(upload, "name", "planning.xlsx")[:255],
            tab=parsed.tab[:100], title=parsed.title[:255], stock_date=stock_date, date_basis=basis,
            header_row=parsed.header_row, columns=parsed.columns, line_count=len(parsed.lines),
            net_req_l=Decimal(str(round(parsed.net_req_l, 3))), uploaded_by=user,
        )
        PlanningSheetLine.objects.bulk_create([
            PlanningSheetLine(
                sheet=sheet, row=ln["row"], code=ln["code"][:50], name=ln["name"][:255],
                plan_l=Decimal(str(round(ln["plan_l"], 3))), ecom_l=Decimal(str(round(ln["ecom_l"], 3))),
                stock_l=Decimal(str(round(ln["stock_l"], 3))), net_l=Decimal(str(round(ln["net_l"], 3))),
                machine=ln["machine"][:100],
            )
            for ln in parsed.lines
        ])
    return sheet


# ---------------------------------------------------------------------------
# The 7 pm check
# ---------------------------------------------------------------------------


def run_check(company: Company, *, day: Optional[date] = None, now=None) -> Optional[PlanCheck]:
    """Last night's plan for ``day`` beside what the plant did up to now."""
    from .check import build_check

    now = now or timezone.localtime()
    day = day or now.date()
    plan = TomorrowPlan.objects.filter(company=company, for_date=day).first()
    if plan is None:
        return None
    rows = build_check(company, plan, day=day, now=now)
    check, _ = PlanCheck.objects.update_or_create(
        company=company, for_date=day,
        defaults={"run_at": now, "plan": plan, "rows": rows,
                  "red": sum(1 for r in rows if r["state"] == "red")},
    )
    return check


def check_payload(check: Optional[PlanCheck]) -> Optional[Dict[str, Any]]:
    if check is None:
        return None
    return {
        "for_date": check.for_date.isoformat(), "run_at": check.run_at.isoformat(),
        "plan_run_at": check.plan.read_at.isoformat() if check.plan else None,
        "red": check.red, "rows": check.rows,
        "rule": "today's plan (made last night) against what happened today up to 7 pm; 10% off is red, "
                "some rows are red whenever they happen; the plan never reads this",
    }


def latest_check(company: Company) -> Optional[PlanCheck]:
    return PlanCheck.objects.filter(company=company).order_by("-for_date").first()
