"""The trail, running itself.

A dashboard only works for people who open it. The brief asks for a system that
"senses what needs to happen ... and raises alarms early enough for each
department to act", which means the loop has to close without anybody logging
in: read SAP, work out who has to do what, tell them, and stay quiet when
nothing has changed.

That last part is what makes it usable. A supply-chain alarm is a standing
condition, not an event — a material that was short this morning is still short
tonight — so re-sending the same list daily is how a channel gets muted and the
one day it matters gets missed. Each department's digest is fingerprinted on
*what* is being raised (the actions, their severity and their dates), never on
when or on quantities that drift by a few units. An unchanged digest is not
re-sent, and that decision is recorded so a silent night is visibly a quiet one
rather than a broken job.

Addressing needs no configuration to work. A department with an
:class:`AlarmSubscription` bound to it goes to that permission; every other
department falls back to the supply-chain view permission, so a fresh install
delivers to the whole team on day one and can be narrowed later by adding rows —
without a code change or a deploy.
"""
import hashlib
import logging

from django.utils import timezone

from ..models import AlarmDispatch, AlarmSubscription
from .live_trail import PRODUCTION_COMPANY, SCOPE_EXTERNAL, build_live_trail
from .live_trail_actions import CRITICAL, PLAN

logger = logging.getLogger(__name__)

# Everyone who can see the supply chain, when a department has not been bound to
# a narrower permission. Better a buyer sees one extra digest than an overdue
# material goes to nobody because a config row was never created.
FALLBACK_PERMISSION = "supply_chain.can_view_supply_chain"

# How many actions a notification spells out before it starts summarising. A
# digest is meant to be actionable from the notification itself; past this it is
# a wall of text and the link does the work.
DIGEST_ACTIONS = 6

# Production also gets the run list; this is how many SKUs it names.
DIGEST_PLAN_ROWS = 8


def _fingerprint(department, plan_rows):
    """What is being raised, not when or how much.

    Includes each action's identity, its severity and the date it turns on —
    those are the things whose change means "tell them again". Quantities are
    deliberately excluded: a shortage drifting from 19,656 to 19,700 is the same
    alarm, and re-sending it is how the channel gets ignored.
    """
    parts = [
        f"{a['id']}|{a['severity']}|{a['due'] or '-'}"
        for a in department["actions"]
    ]
    parts += [f"PLAN|{r['sku']}|{r['limited_by']}" for r in plan_rows]
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def _title(department):
    critical = department["critical"]
    if critical:
        return f"{department['label']}: {critical} action(s) already past due"
    if department["plan"]:
        return f"{department['label']}: {department['plan']} action(s) to schedule"
    return f"{department['label']}: {department['watch']} thing(s) to decide"


def _body(department, plan_rows, plan):
    """A digest someone can act on without opening anything."""
    lines = []
    for action in department["actions"][:DIGEST_ACTIONS]:
        mark = {CRITICAL: "!", PLAN: "-"}.get(action["severity"], "?")
        due = f" (by {action['due']})" if action["due"] else ""
        lines.append(f"{mark} {action['title']}{due}")
    if len(department["actions"]) > DIGEST_ACTIONS:
        lines.append(f"...and {len(department['actions']) - DIGEST_ACTIONS} more")

    if plan_rows:
        lines.append("")
        lines.append(f"Run plan for {plan['date']}:")
        for row in plan_rows[:DIGEST_PLAN_ROWS]:
            note = {
                "MATERIAL": f" (material-capped; short {row['blocker']['name']})"
                if row.get("blocker") else " (material-capped)",
                "CAPACITY": " (line full)",
            }.get(row["limited_by"], "")
            lines.append(
                f"  {row['planned']:,.0f} {row['uom']} {row['name']}{note}"
            )
        if len(plan_rows) > DIGEST_PLAN_ROWS:
            lines.append(f"  ...and {len(plan_rows) - DIGEST_PLAN_ROWS} more SKUs")
        if plan["totals"]["blocked_skus"]:
            lines.append(
                f"  {plan['totals']['blocked_skus']} SKU(s) cannot run at all "
                f"tomorrow — no material on hand."
            )
    return "\n".join(lines)


def _subscriptions_for(company_code):
    """``{department code: [subscription]}`` for departments that have been bound."""
    bound = {}
    for subscription in AlarmSubscription.objects.filter(
        company_code=company_code, is_active=True
    ).exclude(live_trail_department=""):
        bound.setdefault(subscription.live_trail_department, []).append(subscription)
    return bound


def run_live_trail_autopilot(company_code=PRODUCTION_COMPANY, *, company=None,
                             force=False, dry_run=False, trail=None):
    """Read the trail, route it, and tell each department what it owns.

    ``force`` re-sends an unchanged digest; ``dry_run`` builds everything and
    sends nothing, so the job can be proved in production without paging anyone.
    """
    from notifications.models import NotificationType
    from notifications.services import NotificationService

    trail = trail or build_live_trail(scope=SCOPE_EXTERNAL)
    plan = trail["tomorrow"]
    runnable = [row for row in plan["rows"] if row["planned"] > 0]
    bound = _subscriptions_for(company_code)

    results = []
    for department in trail["departments"]:
        # Production is the only department the run plan is addressed to; for
        # everyone else it is context they did not ask for.
        plan_rows = runnable if department["code"] == "PRODUCTION" else []

        if department["total"] == 0 and not plan_rows:
            results.append({"department": department["label"], "sent": False,
                            "reason": "nothing outstanding"})
            continue

        digest = _fingerprint(department, plan_rows)
        targets = bound.get(department["code"]) or [None]

        for subscription in targets:
            permission = (
                subscription.permission_codename if subscription else FALLBACK_PERMISSION
            )
            already = AlarmDispatch.objects.filter(
                company_code=company_code, subscription=subscription, digest=digest
            ).exists()
            if already and not force:
                results.append({"department": department["label"], "sent": False,
                                "reason": "unchanged since last send"})
                continue

            title = _title(department)
            body = _body(department, plan_rows, plan)
            if dry_run:
                results.append({"department": department["label"], "sent": False,
                                "reason": "dry run", "title": title, "body": body,
                                "permission": permission,
                                "actions": department["total"]})
                continue

            try:
                recipients = NotificationService.send_notification_by_permission(
                    permission_codename=permission,
                    title=title,
                    body=body,
                    notification_type=NotificationType.SUPPLY_CHAIN_ALARM,
                    click_action_url="/supply-chain/live-trail",
                    reference_type="supply_chain_live_trail",
                    company=company,
                    extra_data={
                        "company_code": company_code,
                        "department": department["code"],
                        "critical": department["critical"],
                        "plan": department["plan"],
                        "watch": department["watch"],
                        "items": [a["subject"]["code"] for a in
                                  department["actions"][:25] if a["subject"]["code"]],
                    },
                )
            except Exception as exc:  # noqa: BLE001 — one department failing to
                # deliver must not silence the other four.
                logger.warning("Live trail digest to %s failed: %s", permission, exc)
                results.append({"department": department["label"], "sent": False,
                                "reason": f"delivery failed: {exc}"})
                continue

            AlarmDispatch.objects.create(
                company_code=company_code, subscription=subscription, digest=digest,
                title=title, body=body, recipients=recipients,
                overdue_count=department["critical"],
                order_now_count=department["plan"],
            )
            results.append({"department": department["label"], "sent": True,
                            "recipients": recipients, "actions": department["total"],
                            "title": title})

    logger.info(
        "Live trail autopilot %s: %s department digest(s) sent at %s",
        company_code, sum(1 for r in results if r.get("sent")), timezone.now(),
    )
    return results
