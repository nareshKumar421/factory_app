"""How fresh the SAP copies are -- for the banner, and for the people who answer for them.

Two readers:

* :func:`freshness` -- the "SAP is down" banner: how old the copy is that the
  app is working from, for the company on screen.
* :func:`watch` -- the SAP posting worker, every few minutes: a copy that has
  stopped refreshing while HANA answers is a copy that would let the floor
  down at the next outage, and nobody would know until then. It is told once,
  to the same people as an SAP outage, and again when it is refreshing.
"""

import logging
from datetime import timedelta

from django.core.cache import caches
from django.utils import timezone

from .models import MirrorDataset

logger = logging.getLogger(__name__)

#: Refreshed at every 15-minute run; overdue after three missed runs.
FREQUENT = ("bills", "purchase_orders")
FREQUENT_OVERDUE = timedelta(minutes=45)
#: Refreshed once a night; overdue once a night has been missed.
NIGHTLY_OVERDUE = timedelta(hours=26)

_STALE_KEY = "sap-mirror:stale"


def _overdue(dataset, now, hana_up_since=None) -> bool:
    """Overdue counting from the later of its last refresh and HANA coming back:
    a copy cannot refresh while HANA is down, so the clock only starts once it
    could have. Without that, the first check after an outage cried wolf over
    copies the next run was about to take (20:42 on 2026-10-06)."""
    if dataset.synced_at is None:
        return True
    since = max(dataset.synced_at, hana_up_since) if hana_up_since else dataset.synced_at
    limit = FREQUENT_OVERDUE if dataset.name in FREQUENT else NIGHTLY_OVERDUE
    return now - since > limit


def freshness(company_code):
    """When the copy a company's screens fall back on was taken, or ``None``.

    ``frequent_as_of`` is the bills and open POs (the older of the two);
    ``nightly_as_of`` the master lists -- items, BOMs, warehouses, vendors.
    """
    rows = list(
        MirrorDataset.objects.filter(company__code=company_code, synced_at__isnull=False)
        .values_list("name", "synced_at")
    )
    if not rows:
        return None
    frequent = [at for name, at in rows if name in FREQUENT]
    nightly = [at for name, at in rows if name not in FREQUENT]
    return {
        "company_code": company_code,
        "frequent_as_of": min(frequent).isoformat() if frequent else None,
        "nightly_as_of": min(nightly).isoformat() if nightly else None,
    }


def stale_copies(now=None, hana_up_since=None):
    """``[(company, list label, synced_at)]`` for every copy that is overdue."""
    from .services import DATASETS

    now = now or timezone.now()
    stale = []
    for dataset in MirrorDataset.objects.select_related("company").order_by("company__code", "name"):
        if dataset.name in DATASETS and _overdue(dataset, now, hana_up_since):
            stale.append((dataset.company.code, DATASETS[dataset.name].label, dataset.synced_at))
    return stale


def watch(snap, now=None):
    """Alert when a copy stops refreshing while HANA answers, and when it recovers.

    ``snap`` is the worker's health snapshot. With HANA down a copy cannot
    refresh and is not expected to -- that is the outage the copy is for, and it
    has its own alert.
    """
    from sap_client import health

    hana = (snap.get("components") or {}).get(health.HANA) or {}
    if hana.get("status") != health.UP:
        return None
    now = now or timezone.now()
    stale = stale_copies(now, _parsed(hana.get("since")))
    keys = sorted(f"{code}:{label}" for code, label, _ in stale)
    try:
        cache = caches["shared"]
        before = cache.get(_STALE_KEY) or []
        cache.set(_STALE_KEY, keys, 7 * 24 * 3600)
    except Exception:  # noqa: BLE001 -- an alert is never the reason the worker stops
        logger.exception("Could not read the SAP copy alert state")
        return None
    if keys and set(keys) - set(before):
        _alert(stale, now, recovered=False)
    elif before and not keys:
        _alert([], now, recovered=True)
    return keys


def _parsed(value):
    """The snapshot's ISO timestamp as an aware datetime, or ``None``."""
    from datetime import datetime

    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return when if timezone.is_aware(when) else timezone.make_aware(when)


def _alert(stale, now, *, recovered):
    from sap_client.health import _alert_recipients

    if recovered:
        title = "The app's copy of SAP is refreshing again"
        body = "Every copy is up to date again; if SAP goes down, the app works from current data."
    else:
        listed = "; ".join(
            f"{code} {label}: "
            + (f"last taken {timezone.localtime(at):%d %b %H:%M}" if at else "never taken")
            for code, label, at in stale
        )
        title = "The app's copy of SAP has stopped refreshing"
        body = (
            f"{listed}. If SAP goes down now, dispatch, the gate and production would "
            "work from data that old. Check the copy job on the server "
            "(journalctl -u factory-sap-copy)."
        )
    try:
        from notifications.services import NotificationService

        NotificationService.send_notification_to_group(
            users=list(_alert_recipients()),
            title=title,
            body=body[:1000],
            reference_type="sap_mirror",
            extra_data={"recovered": recovered},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not send the SAP copy alert")
    logger.warning("%s: %s", title, body)
