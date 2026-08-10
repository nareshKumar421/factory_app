"""The workflow around a daily run.

    GENERATED  ->  REVIEWED  ->  PUBLISHED  ->  verdicts recorded  ->  weekly review

Each transition exists because the playbook names a human step, and each one
records who did it. The two that carry real judgement:

* **Review is blocked when there are too many reds.** The playbook is explicit
  that a flood of alarms means the inputs are wrong, not that the factory is on
  fire, and sending it anyway is how a department learns to ignore the sheet.
* **A verdict is required on red rows.** Without it nobody ever finds out whether
  the alarms were worth raising, which is the entire question the trial exists to
  answer.
"""
import logging
from collections import Counter
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from ..models import (
    CoverVerdict,
    DailyRun,
    DailyRunRow,
    RowVerdict,
    RowVerdictState,
    RunStatus,
)
from .errors import SupplyChainError

logger = logging.getLogger(__name__)


def _actor(user):
    return getattr(user, "email", "") or getattr(user, "username", "") or ""


def get_run(company_code, run_date=None, run_id=None):
    qs = DailyRun.objects.filter(company_code=company_code)
    if run_id:
        run = qs.filter(id=run_id).first()
    elif run_date:
        run = qs.filter(run_date=run_date).first()
    else:
        run = qs.order_by("-run_date", "-id").first()
    if run is None:
        raise SupplyChainError(
            "No daily run found. Generate one first.", code="NO_RUN", status_code=404
        )
    return run


def review_run(run, *, user=None, comment="", override=False):
    """The analyst's 08:00 step.

    ``override`` lets a run be reviewed despite too many reds, but only with a
    written comment — the point is not to make it impossible, it is to make it
    deliberate and attributable.
    """
    if run.status == RunStatus.PUBLISHED:
        raise SupplyChainError(
            "This run has already been published.", code="ALREADY_PUBLISHED", status_code=409
        )
    if not run.is_credible and not override:
        limit = (run.parameters_snapshot or {}).get("max_red_before_block", 25)
        raise SupplyChainError(
            f"{run.red_count} red rows is more than the {limit} this company treats as "
            "credible. That usually means the stock or purchase-order data is wrong, "
            "not that the factory is short. Fix the inputs, or review with an override "
            "and a comment saying why.",
            code="TOO_MANY_REDS", status_code=409,
        )
    if not run.is_credible and not (comment or "").strip():
        raise SupplyChainError(
            "Overriding the alarm limit needs a comment explaining why.",
            code="OVERRIDE_NEEDS_COMMENT", status_code=400,
        )

    run.status = RunStatus.REVIEWED
    run.reviewed_by = _actor(user)
    run.reviewed_at = timezone.now()
    if comment:
        run.comment = comment
    run.save(update_fields=["status", "reviewed_by", "reviewed_at", "comment"])
    return run


def unassigned_red_rows(run):
    """Red rows with nobody's name against them.

    "A red alarm nobody owns will not get done" — so this is surfaced at publish
    time rather than discovered a week later in the verdict log.
    """
    return run.rows.filter(verdict=CoverVerdict.RED, owner="")


def assign_owner(row, owner, *, user=None):
    row.owner = (owner or "").strip()[:150]
    row.save(update_fields=["owner"])
    return row


def publish_run(run, *, user=None, company=None, comment=""):
    """Send the run to the people who have to act on it."""
    from notifications.models import NotificationType
    from notifications.services import NotificationService

    from ..models import AlarmSubscription

    if run.status == RunStatus.GENERATED:
        raise SupplyChainError(
            "Review the run before publishing it — the analyst check is the point.",
            code="NOT_REVIEWED", status_code=409,
        )
    if run.status == RunStatus.BLOCKED:
        raise SupplyChainError(
            "This run is blocked by its alarm count and has not been reviewed.",
            code="BLOCKED", status_code=409,
        )
    if comment:
        run.comment = comment

    reds = list(run.rows.filter(verdict=CoverVerdict.RED))
    title = f"Supply chain {run.run_date:%d %b}: {len(reds)} to order today"
    lines = []
    if run.comment:
        lines.append(run.comment)
        lines.append("")
    for row in reds[:12]:
        late = f" — {row.days_late}d late" if row.days_late else ""
        lines.append(
            f"{row.material_code}: {row.days_of_cover} days of cover vs "
            f"{row.lead_time_days}d lead{late}"
            + (f" [{row.owner}]" if row.owner else "")
        )
    if len(reds) > 12:
        lines.append(f"…and {len(reds) - 12} more.")
    if run.unknown_count:
        lines.append(f"{run.unknown_count} material(s) could not be judged.")
    if run.issue_count:
        lines.append(f"{run.issue_count} data-quality issue(s) — read those first.")
    body = "\n".join(lines) or "Nothing needs ordering today."

    recipients = 0
    subscriptions = list(AlarmSubscription.objects.filter(
        company_code=run.company_code, is_active=True
    ))
    if not subscriptions:
        raise SupplyChainError(
            "No alarm subscriptions are configured, so publishing would tell nobody.",
            code="NO_SUBSCRIBERS", status_code=409,
        )
    for subscription in subscriptions:
        try:
            recipients += NotificationService.send_notification_by_permission(
                permission_codename=subscription.permission_codename,
                title=title,
                body=body,
                notification_type=NotificationType.SUPPLY_CHAIN_ALARM,
                click_action_url=f"/dashboards/supply-chain/runs/{run.id}",
                reference_type="supply_chain_daily_run",
                reference_id=run.id,
                company=company,
                extra_data={"run_date": run.run_date.isoformat(), "red": len(reds)},
            )
        except Exception as exc:  # noqa: BLE001 — one department failing must not
            # stop the rest of the factory being told.
            logger.warning("Daily run publish to %s failed: %s",
                           subscription.permission_codename, exc)

    run.status = RunStatus.PUBLISHED
    run.published_by = _actor(user)
    run.published_at = timezone.now()
    run.recipients = recipients
    run.save(update_fields=[
        "status", "published_by", "published_at", "recipients", "comment",
    ])
    return run, {"title": title, "body": body, "recipients": recipients}


def record_verdict(row, outcome, *, note="", promised_date=None, user=None):
    """The buyer's answer, after the phone call."""
    if outcome not in RowVerdictState.values:
        raise SupplyChainError(
            f"{outcome!r} is not a verdict.", code="BAD_VERDICT", status_code=400
        )
    verdict, _created = RowVerdict.objects.update_or_create(
        row=row,
        defaults={
            "outcome": outcome,
            "note": note or "",
            "supplier_promised_date": promised_date,
            "recorded_by": _actor(user),
        },
    )
    return verdict


def verdict_progress(run):
    """How much of the verdict log is actually filled in.

    The playbook: "if it is empty at the end of the month, we learned nothing."
    """
    reds = run.rows.filter(verdict=CoverVerdict.RED)
    total = reds.count()
    done = RowVerdict.objects.filter(row__in=reds).count()
    return {
        "red_rows": total,
        "verdicts_recorded": done,
        "outstanding": total - done,
        "complete": total == done,
    }


def weekly_review(company_code, *, weeks=4, today=None):
    """The Monday step — is the system getting more trustworthy or less?

    The share of REAL verdicts is the measure that matters: it rising means the
    alarms are worth acting on; it flat or falling means the inputs are still
    wrong and more software will not help.
    """
    today = today or timezone.localdate()
    start = today - timedelta(weeks=weeks)
    runs = DailyRun.objects.filter(
        company_code=company_code, run_date__gte=start, run_date__lte=today
    ).order_by("run_date")

    verdicts = RowVerdict.objects.filter(row__run__in=runs).values_list(
        "row__run__run_date", "outcome"
    )
    by_week = {}
    for run_date, outcome in verdicts:
        key = (run_date - timedelta(days=run_date.weekday())).isoformat()
        by_week.setdefault(key, Counter())[outcome] += 1

    weeks_out = []
    for week_start, counter in sorted(by_week.items()):
        total = sum(counter.values())
        real = counter.get(RowVerdictState.REAL, 0)
        weeks_out.append({
            "week_starting": week_start,
            "total": total,
            "real": real,
            "wrong_data": counter.get(RowVerdictState.WRONG_DATA, 0),
            "already_handled": counter.get(RowVerdictState.ALREADY_HANDLED, 0),
            "real_share_percent": round(real * 100 / total, 1) if total else 0.0,
        })

    overall = Counter(outcome for _d, outcome in verdicts)
    total = sum(overall.values())
    real = overall.get(RowVerdictState.REAL, 0)
    wrong = overall.get(RowVerdictState.WRONG_DATA, 0)

    # The playbook's week-4 decision, stated rather than left to interpretation.
    if total == 0:
        recommendation = (
            "No verdicts recorded yet. Until the buyer fills these in, there is no "
            "evidence either way and the trial has taught us nothing."
        )
    elif wrong > real:
        recommendation = (
            f"{wrong} of {total} alarms were wrong data. Spend another month fixing "
            "stock and purchase-order accuracy — not on software."
        )
    else:
        recommendation = (
            f"{real} of {total} alarms were real. The method is working; widen it "
            "beyond the trial SKU and start acting on the alarms earlier."
        )

    return {
        "from": start.isoformat(),
        "to": today.isoformat(),
        "runs": runs.count(),
        "weeks": weeks_out,
        "totals": {
            "verdicts": total,
            "real": real,
            "wrong_data": wrong,
            "already_handled": overall.get(RowVerdictState.ALREADY_HANDLED, 0),
            "real_share_percent": round(real * 100 / total, 1) if total else 0.0,
        },
        "recommendation": recommendation,
    }
