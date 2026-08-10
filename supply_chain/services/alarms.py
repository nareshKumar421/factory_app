"""Deliver the procurement alarms.

Step 6 computes WHEN each material must be ordered. That is only half the brief's
promise — "raise alarms early enough for each department to act" means the alarm
has to reach a person. This module turns the action list into a digest and sends
it through the existing ``notifications`` app.

The brief never says who is alarmed, through which channel, or at what threshold,
so none of that is hard-coded: :class:`AlarmSubscription` rows decide, one per
department, addressed by the Django permission that department already holds.

Sends are fingerprinted. A supply-chain alarm is a standing condition, not an
event — an overdue order stays overdue every day until someone places it — so
re-sending an unchanged digest would train people to ignore the channel.
"""
import hashlib
import logging

from ..models import AlarmDispatch, AlarmState, AlarmSubscription
from .planning import capacity_check, material_alarms

logger = logging.getLogger(__name__)

# Alarm states a subscription can ask for, and the flag that switches each on.
_STATE_FLAGS = {
    AlarmState.OVERDUE: "include_overdue",
    AlarmState.ORDER_NOW: "include_order_now",
    AlarmState.NO_LEAD_TIME: "include_missing_lead_time",
}


def _wanted_states(subscription):
    return {state for state, flag in _STATE_FLAGS.items() if getattr(subscription, flag)}


def _matching_rows(rows, subscription):
    states = _wanted_states(subscription)
    out = []
    for row in rows:
        if row["alarm"] not in states:
            continue
        # A packaging buyer should not be paged about bulk oil.
        if subscription.material_type and row["material_type"] != subscription.material_type:
            continue
        out.append(row)
    return out


def _digest(rows, capacity_lines):
    """Fingerprint of WHAT is being alarmed, not when.

    Deliberately covers the item, its state and its order-by date but NOT the
    quantity: a shortage that drifts by a few units is the same alarm, and
    re-sending it every night is how a channel gets muted.
    """
    parts = [f"{r['item_code']}|{r['alarm']}|{r['order_by'] or '-'}" for r in rows]
    parts += [f"CAP|{m['machine_id']}" for m in capacity_lines]
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def _body(rows, capacity_lines, limit=10):
    """A digest a person can act on from a notification, without opening anything."""
    lines = []
    overdue = [r for r in rows if r["alarm"] == AlarmState.OVERDUE]
    order_now = [r for r in rows if r["alarm"] == AlarmState.ORDER_NOW]
    unknown = [r for r in rows if r["alarm"] == AlarmState.NO_LEAD_TIME]

    for label, group in (("OVERDUE", overdue), ("Order now", order_now)):
        for r in group[:limit]:
            lines.append(
                f"{label}: {r['item_code']} — order {r['order_qty']} {r['unit']}".rstrip()
                + (f" by {r['order_by']}" if r["order_by"] else "")
            )
        if len(group) > limit:
            lines.append(f"…and {len(group) - limit} more {label.lower()}")

    if unknown:
        lines.append(
            f"{len(unknown)} material(s) have no lead time on file — cannot be timed."
        )
    for machine in capacity_lines:
        lines.append(
            f"Line {machine['machine_id']} is over capacity by "
            f"{machine['shortfall_hours']}h."
        )
    return "\n".join(lines)


def _title(rows, capacity_lines):
    overdue = sum(1 for r in rows if r["alarm"] == AlarmState.OVERDUE)
    order_now = sum(1 for r in rows if r["alarm"] == AlarmState.ORDER_NOW)
    bits = []
    if overdue:
        bits.append(f"{overdue} overdue")
    if order_now:
        bits.append(f"{order_now} to order now")
    if capacity_lines:
        bits.append(f"{len(capacity_lines)} line(s) over capacity")
    return "Supply chain: " + (", ".join(bits) if bits else "action needed")


def send_supply_chain_alarms(company_code, *, company=None, forecast_id=None,
                             today=None, force=False, dry_run=False):
    """Send each active subscription its digest. Returns a per-subscription report.

    ``force`` re-sends an unchanged digest; ``dry_run`` builds everything and
    sends nothing, so a cron can be verified in production without paging anyone.
    """
    from notifications.models import NotificationType
    from notifications.services import NotificationService

    alarms = material_alarms(company_code, forecast_id=forecast_id, today=today)
    capacity = capacity_check(company_code, forecast_id=forecast_id)
    over_capacity = [m for m in capacity["machines"] if not m["feasible"]]

    results = []
    for subscription in AlarmSubscription.objects.filter(
        company_code=company_code, is_active=True
    ):
        rows = _matching_rows(alarms["rows"], subscription)
        lines = over_capacity if subscription.include_capacity else []
        if not rows and not lines:
            results.append({"subscription": subscription.label or
                            subscription.permission_codename,
                            "sent": False, "reason": "nothing to report"})
            continue

        digest = _digest(rows, lines)
        already = AlarmDispatch.objects.filter(
            company_code=company_code, subscription=subscription, digest=digest
        ).exists()
        if already and not force:
            results.append({"subscription": subscription.label or
                            subscription.permission_codename,
                            "sent": False, "reason": "unchanged since last send"})
            continue

        title, body = _title(rows, lines), _body(rows, lines)
        if dry_run:
            results.append({"subscription": subscription.label or
                            subscription.permission_codename,
                            "sent": False, "reason": "dry run", "title": title,
                            "body": body, "matched": len(rows)})
            continue

        try:
            recipients = NotificationService.send_notification_by_permission(
                permission_codename=subscription.permission_codename,
                title=title,
                body=body,
                notification_type=NotificationType.SUPPLY_CHAIN_ALARM,
                click_action_url="/dashboards/supply-chain",
                reference_type="supply_chain_alarm",
                company=company,
                extra_data={
                    "company_code": company_code,
                    "overdue": sum(1 for r in rows if r["alarm"] == AlarmState.OVERDUE),
                    "order_now": sum(1 for r in rows if r["alarm"] == AlarmState.ORDER_NOW),
                    "items": [r["item_code"] for r in rows[:25]],
                },
            )
        except Exception as exc:  # noqa: BLE001 — one department's failure must not
            # silence every other department's alarm.
            logger.warning(
                "Supply chain alarm to %s failed: %s", subscription.permission_codename, exc
            )
            results.append({"subscription": subscription.label or
                            subscription.permission_codename,
                            "sent": False, "reason": f"delivery failed: {exc}"})
            continue

        AlarmDispatch.objects.create(
            company_code=company_code, subscription=subscription, digest=digest,
            title=title, body=body, recipients=recipients,
            overdue_count=sum(1 for r in rows if r["alarm"] == AlarmState.OVERDUE),
            order_now_count=sum(1 for r in rows if r["alarm"] == AlarmState.ORDER_NOW),
        )
        results.append({"subscription": subscription.label or
                        subscription.permission_codename,
                        "sent": True, "recipients": recipients, "matched": len(rows),
                        "title": title})
    return results
