"""
admin_board/alerts.py

The action centre: the board's figures turned into things somebody has to do.

WHAT COUNTS AS AN ALERT
-----------------------
Only a condition with an owner and an action. "Production is 27% of plan" is an
alert because somebody re-plans; "production was 42 tonnes today" is not,
however low it is, because the tile already says so and nobody is asked to do
anything about the number itself.

THE ONE RULE THAT SHAPES EVERYTHING HERE
----------------------------------------
**A tile that could not be read produces NO alerts.** Not a warning, not an
"unknown" — nothing. Every rule below guards on its section being present,
because a silent all-clear derived from a failed SAP read is the single most
dangerous thing this file could emit: it reports a healthy plant on the strength
of having learned nothing about it. Degradation is already reported, separately
and honestly, in ``meta.degraded``.

A SECOND, SUBTLER ONE
---------------------
**Zero with a warning is not zero.** A cost slice reading nil because nobody
configured a rate is a data gap and raises an alert; a slice genuinely nil for
the month is not. ``has_source`` carries that distinction up from the service,
and conflating the two would either cry wolf every month or hide a missing rate
forever.
"""

from datetime import date
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Thresholds — the numbers a business argues about, in one place
# ---------------------------------------------------------------------------

#: A store at or above this share of its rating is critical.
#:
#: 90 rather than 95: the thing that matters is whether there is somewhere to
#: put tomorrow's output, and on a floor turning over in days, 5% of headroom is
#: already less than one day's production.
WAREHOUSE_CRITICAL_PCT = 90.0

#: Above this, worth flagging before it becomes critical.
WAREHOUSE_WARNING_PCT = 80.0

#: Production this far BEHIND the share of the month already elapsed is critical.
#:
#: Ten points, not zero: a plant that is fractionally behind on the 3rd is
#: normal, and an alert that fires every month is an alert nobody reads.
PLAN_CRITICAL_GAP_PCT = 10.0

#: Below this the plant is simply not behind.
#:
#: There must be a floor, and it must not be zero. Output arrives in lumpy
#: batches against a plan spread evenly across the month, so on most days of
#: most months the plan-to-date is fractionally ahead of the actual — a rule
#: firing on any gap at all would raise a warning nearly every day and train
#: everybody to ignore the panel. Five points is roughly a day and a half of a
#: thirty-day plan: past that, being behind is a trend rather than a batch that
#: has not posted yet.
PLAN_WARNING_GAP_PCT = 5.0

#: A stock check older than this is worth chasing.
AUDIT_STALE_DAYS = 30

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


def _alert(
    key: str,
    severity: str,
    title: str,
    detail: str,
    action: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "key": key,
        "severity": severity,
        "title": title,
        "detail": detail,
        "action": action,
    }


def _t(value: Optional[float]) -> str:
    """A tonnage, for prose."""
    if value is None:
        return "—"
    return f"{value:,.1f} T"


def _money(value: Optional[float]) -> str:
    """Rupees at the scale a factory reads them."""
    if value is None:
        return "—"
    if abs(value) >= 10_000_000:
        return f"₹{value / 10_000_000:,.2f} Cr"
    if abs(value) >= 100_000:
        return f"₹{value / 100_000:,.2f} L"
    return f"₹{value:,.0f}"


def build_alerts(board: Dict[str, Any], today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Every rule, most severe first.

    Ordering is by severity and then by the order the rules run, which is the
    order the board is read: output, then storage, then cost. Within a severity
    that puts "we are not making enough" above "a rate is missing", which is the
    order somebody would act in.
    """
    today = today or date.today()
    alerts: List[Dict[str, Any]] = []

    alerts.extend(_production_alerts(board))
    alerts.extend(_storage_alerts(board, today))
    alerts.extend(_cost_alerts(board))

    order = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
    alerts.sort(key=lambda entry: order.get(entry["severity"], 9))
    return alerts


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _production_alerts(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    production = (board.get("output") or {}).get("production")
    if not production:
        return []

    plan_pct = production.get("plan_pct")
    if plan_pct is None:
        # No plan filed is a real gap, but it is the planner's, not the floor's.
        return [
            _alert(
                "production.no_plan",
                SEVERITY_WARNING,
                "No production plan is filed for this month",
                "Output is being reported with nothing to measure it against.",
                "File a plan",
            )
        ]

    elapsed = ((board.get("meta") or {}).get("period") or {}).get("elapsed_pct")
    if elapsed is None:
        return []

    gap = elapsed - plan_pct
    if gap < PLAN_WARNING_GAP_PCT:
        return []

    required = production.get("required_tons_per_day")
    actual = production.get("avg_tons_per_producing_day")
    severity = SEVERITY_CRITICAL if gap >= PLAN_CRITICAL_GAP_PCT else SEVERITY_WARNING

    detail = (
        f"{_t(production.get('mtd_tons'))} of {_t(production.get('plan_tons'))} "
        f"with {int(elapsed)}% of the month gone"
    )
    if required is not None and actual is not None:
        detail += f" · needs {_t(required)}/day against {_t(actual)} actual"

    return [
        _alert(
            "production.behind_plan",
            severity,
            f"Production is at {plan_pct:.0f}% of plan",
            detail,
            "Re-plan",
        )
    ]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _storage_alerts(board: Dict[str, Any], today: date) -> List[Dict[str, Any]]:
    storage = board.get("storage") or {}
    alerts: List[Dict[str, Any]] = []

    fg = storage.get("fg")
    if fg:
        alerts.extend(_fg_alerts(fg, today))

    # One combined alert for every store nobody has rated, rather than one each:
    # they all need the same thing done in the same place, and three separate
    # rows pushes a real problem off the bottom of the list.
    unrated = [
        name
        for name, section in (("PM stores", storage.get("pm")), ("oil tanks", storage.get("oil")))
        if section and section.get("used_pct") is None
    ]
    if unrated:
        reasons = [
            section.get("no_capacity_reason")
            for section in (storage.get("pm"), storage.get("oil"))
            if section and section.get("used_pct") is None and section.get("no_capacity_reason")
        ]
        # Sentence-cased by hand, not `.capitalize()` — that lowercases the rest
        # of the string and turns "PM stores" into "Pm stores".
        subject = " and ".join(unrated)
        alerts.append(
            _alert(
                "storage.unrated",
                SEVERITY_WARNING,
                f"{subject[0].upper()}{subject[1:]} show no % used",
                " ".join(reasons) or "No rated capacity has been entered.",
                "Rate them",
            )
        )

    return alerts


def _fg_alerts(fg: Dict[str, Any], today: date) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    rows = fg.get("rows") or []

    #: The emptiest rated store, named in the full store's alert. A "shift
    #: stock" instruction with nowhere named is not an instruction.
    with_room = [
        row for row in rows if row.get("used_pct") is not None and row.get("free_tons")
    ]
    emptiest = min(with_room, key=lambda row: row["used_pct"]) if with_room else None

    for row in rows:
        used = row.get("used_pct")
        if used is None or used < WAREHOUSE_WARNING_PCT:
            continue
        severity = (
            SEVERITY_CRITICAL if used >= WAREHOUSE_CRITICAL_PCT else SEVERITY_WARNING
        )
        detail = (
            f"{_t(row.get('tons'))} of a rated {_t(row.get('capacity_tons'))} — "
            f"{_t(row.get('free_tons'))} free"
        )
        if emptiest and emptiest["warehouse"] != row["warehouse"]:
            detail += (
                f". {emptiest['label']} is at {emptiest['used_pct']:.0f}% "
                f"with {_t(emptiest['free_tons'])} free"
            )
        alerts.append(
            _alert(
                f"storage.full.{row['warehouse']}",
                severity,
                f"{row['label']} is {used:.1f}% full",
                detail,
                "Shift stock",
            )
        )

    # Stock standing where nobody rated the building. Reported because the
    # tonnage is real and the total deliberately excludes it — see the service.
    for row in fg.get("unrated") or []:
        alerts.append(
            _alert(
                f"storage.unrated_stock.{row['warehouse']}",
                SEVERITY_WARNING,
                f"{_t(row.get('tons'))} sits in {row['warehouse']}, which is on no storage sheet",
                (
                    f"{row['label']} holds finished goods outside the rated total, "
                    "so it is counted nowhere."
                ),
                "Rate it",
            )
        )

    for row in rows:
        audit = row.get("last_audit_date")
        if not audit:
            continue
        try:
            age = (today - date.fromisoformat(audit)).days
        except (TypeError, ValueError):
            continue
        if age > AUDIT_STALE_DAYS:
            alerts.append(
                _alert(
                    f"storage.audit.{row['warehouse']}",
                    SEVERITY_WARNING,
                    f"{row['label']} was last counted {age} days ago",
                    f"Stock was physically verified on {audit}.",
                    "Schedule",
                )
            )

    return alerts


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def _cost_alerts(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    cost = board.get("cost")
    if not cost:
        return []

    alerts: List[Dict[str, Any]] = []

    unsourced = [entry for entry in cost.get("slices") or [] if not entry.get("has_source")]
    if unsourced:
        names = ", ".join(entry["label"].lower() for entry in unsourced)
        alerts.append(
            _alert(
                "cost.unsourced",
                # Two or more empty slices is not a gap, it is a donut that
                # cannot be read as a breakdown at all.
                SEVERITY_CRITICAL if len(unsourced) >= 2 else SEVERITY_WARNING,
                f"{len(unsourced)} of {len(cost.get('slices') or [])} cost lines read nil",
                (
                    f"No rate or entry behind {names} — the total of "
                    f"{_money(cost.get('total'))} is understated by whatever they are worth."
                ),
                "Cost Master",
            )
        )

    # The wall board's own warnings, carried through rather than restated: they
    # name the exact cost type and period that is missing, which is what makes
    # them actionable, and re-deriving that here would let the two drift.
    for warning in cost.get("warnings") or []:
        if any(entry.get("warning") == warning for entry in unsourced):
            continue
        alerts.append(
            _alert(
                f"cost.warning.{abs(hash(warning)) % 100000}",
                SEVERITY_WARNING,
                "A cost rate is missing",
                warning,
                "Cost Master",
            )
        )

    return alerts
