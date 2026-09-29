"""Is SAP answering? One shared answer, so an outage costs one wait, not one per request.

Two components, because they fail apart: on 2026-09-28 the Service Layer (every
posting) hung and then refused connections for five hours while HANA (every
read) answered throughout.

**Where the answer lives.** In the ``shared`` cache (Redis in production, see
``SHARED_CACHE_URL``), not in Postgres. Most SAP calls run inside a transaction
that rolls back when SAP fails, and a "down" written there would roll back with
it. Without Redis each worker keeps its own answer, which is only slower to
learn, never wrong.

**Who says what.** Only :func:`probe` declares a component down: it asks a
question that costs SAP nothing (``GET /b1s/v2/`` without a session answers 401
in milliseconds; ``SELECT 1 FROM DUMMY``), so a failure there is SAP's and not
the document's. Any real call that gets through records it up, so recovery shows
the moment anything succeeds.

**What it changes.** While a component is down and was probed in the last
``RETRY_AFTER`` seconds, a call fails at once instead of waiting out its timeout
to learn the same thing. After that window calls go through again, so a stale
"down" can never hold off an SAP that has come back. Down for ``ALERT_AFTER``
seconds, every active superuser is told -- every manager here is one -- along
with the ``SAP_HEALTH_ALERT_GROUP``, which is for anyone else who should hear;
and all of them again when it is back.

Probes are driven by :func:`snapshot`: the health endpoint calls it for every
open FactoryFlow tab, and the SAP posting worker calls it on every pass, so SAP
is asked about every ``PROBE_INTERVAL`` seconds whether or not anyone is looking.

**Only the worker alerts** (``alert=True``). Every web process records what it
finds, but if each could alert, an outage would be told once per process that
noticed it -- which is what happens the moment the shared cache is missing or
down. A web probe that sees SAP come back leaves the "it's back" owed, for the
worker's next probe to send.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as dt_timezone

import requests
from django.conf import settings
from django.core.cache import caches
from django.utils import timezone

logger = logging.getLogger(__name__)

SERVICE_LAYER = "service_layer"
HANA = "hana"
COMPONENTS = (SERVICE_LAYER, HANA)
LABELS = {SERVICE_LAYER: "Service Layer", HANA: "HANA"}

UP = "up"
DOWN = "down"
UNKNOWN = "unknown"

#: A component last asked longer ago than this is asked again.
PROBE_INTERVAL = 30
#: While down, calls fail fast for this long after the last probe.
RETRY_AFTER = 30
#: Down this long, and the worker tells the superusers and the alert group.
ALERT_AFTER = 300
#: (connect, read) for the Service Layer probe; HANA's in milliseconds.
SL_PROBE_TIMEOUT = (3, 5)
HANA_PROBE_CONNECT_MS = 3000
HANA_PROBE_COMMUNICATION_MS = 5000

_STATE_KEY = "sap-health:{}"
_PROBE_LOCK = "sap-health:probing"
_STATE_TTL = 24 * 3600
# Longer than the slowest probe, so a probe that dies holding it frees it anyway.
_PROBE_LOCK_TTL = 20


# ---------------------------------------------------------------------------
# the stored state
# ---------------------------------------------------------------------------

def _cache():
    return caches["shared"]


def _read(component):
    try:
        return _cache().get(_STATE_KEY.format(component))
    except Exception as exc:  # noqa: BLE001 -- health must never be the outage
        logger.warning("SAP health state unreadable: %s", exc)
        return None


def _write(component, state):
    try:
        _cache().set(_STATE_KEY.format(component), state, _STATE_TTL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SAP health state not saved: %s", exc)


def current(component):
    return _read(component) or {
        "status": UNKNOWN, "since": None, "checked_at": 0, "error": "", "alerted": False,
    }


# ---------------------------------------------------------------------------
# around a real call
# ---------------------------------------------------------------------------

def state_for_call(component):
    """The stored state, read once for :func:`failing_fast` and :func:`record_success`."""
    return _read(component)


def failing_fast(state) -> bool:
    """Down, and probed recently enough that trying again would only wait to learn it."""
    return bool(
        state
        and state.get("status") == DOWN
        and time.time() - (state.get("checked_at") or 0) < RETRY_AFTER
    )


def refusal(component, state) -> str:
    """Why a call was not even tried: the words the operator sees."""
    ago = int(time.time() - (state.get("checked_at") or 0))
    what = "this was not sent" if component == SERVICE_LAYER else "SAP was not asked"
    return (
        f"SAP {LABELS[component]} has not been answering since "
        f"{_clock(state.get('since'))} (checked {ago}s ago), so {what}. "
        f"Try again once SAP is back."
    )


def guard_service_layer():
    """For code that logs in to the Service Layer by hand rather than through
    ``ServiceLayerSession``: raise now if it is known down."""
    state = state_for_call(SERVICE_LAYER)
    if failing_fast(state):
        from .exceptions import SAPUnavailable

        raise SAPUnavailable(refusal(SERVICE_LAYER, state))


def record_success(component, state=None, *, probed=False, alert=False):
    """SAP answered. Returns the state now stored.

    A real call that finds it already up writes nothing, so the hot path is one
    read. A probe always writes, to keep ``checked_at`` honest.
    """
    if state is None:
        state = _read(component)
    state = state or {}
    now = time.time()
    status = state.get("status")
    if status == UP and not probed:
        return state
    # They were told it went down, so they are owed "it's back". Only the
    # worker's probe sends that: a real call may be inside a transaction that
    # rolls back and should not wait on a push, and a web probe is one of
    # several processes. Anything else just leaves it owed.
    sends = probed and alert
    owed = (status == DOWN and state.get("alerted")) or state.get("recovery_pending")
    down_since = state.get("since") if status == DOWN else state.get("down_since")
    new = {
        "status": UP,
        "since": state["since"] if status == UP else now,
        "checked_at": now,
        "error": "",
        "alerted": False,
        "recovery_pending": bool(owed and not sends),
        "down_since": down_since if owed and not sends else None,
    }
    _write(component, new)
    if status == DOWN:
        logger.warning(
            "SAP %s answering again (down since %s)", LABELS[component], _clock(down_since)
        )
    if owed and sends:
        _alert(component, recovered=True, state={"since": down_since})
    return new


def record_failure(component, error, *, alert=False):
    """A probe got no answer. Returns the state now stored.

    ``alert``: this is the worker's probe, which tells people once SAP has
    been down for ``ALERT_AFTER``. Any other probe only records it.
    """
    state = _read(component) or {}
    now = time.time()
    was_down = state.get("status") == DOWN
    new = {
        "status": DOWN,
        "since": state["since"] if was_down else now,
        "checked_at": now,
        "error": str(error)[:300],
        "alerted": bool(was_down and state.get("alerted")),
    }
    if not was_down:
        logger.error("SAP %s stopped answering: %s", LABELS[component], error)
    if alert and not new["alerted"] and now - new["since"] >= ALERT_AFTER:
        _alert(component, recovered=False, state=new)
        new["alerted"] = True
    _write(component, new)
    return new


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------

def _probe_service_layer():
    # No session, no credentials: an SL that is alive refuses it (401) at once
    # and creates nothing. A 5xx is its load balancer with no worker behind it.
    response = requests.get(
        f"{settings.SL_URL}/b1s/v2/", timeout=SL_PROBE_TIMEOUT, verify=False
    )
    if response.status_code >= 500:
        raise RuntimeError(f"HTTP {response.status_code}")


def _probe_hana():
    from hdbcli import dbapi

    conn = dbapi.connect(
        address=settings.HANA_HOST,
        port=settings.HANA_PORT,
        user=settings.HANA_USER,
        password=settings.HANA_PASSWORD,
        connectTimeout=HANA_PROBE_CONNECT_MS,
        communicationTimeout=HANA_PROBE_COMMUNICATION_MS,
    )
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM DUMMY")
        cursor.fetchone()
        cursor.close()
    finally:
        conn.close()


_PROBES = {SERVICE_LAYER: _probe_service_layer, HANA: _probe_hana}


def _describe(exc) -> str:
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "accepted the connection but did not answer"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "did not accept the connection"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "refused the connection"
    return str(exc) or type(exc).__name__


def probe(*, alert=False):
    """Ask both components now, side by side, and record the answers."""
    with ThreadPoolExecutor(max_workers=len(COMPONENTS)) as pool:
        pending = {c: pool.submit(_PROBES[c]) for c in COMPONENTS}
        states = {}
        # Recorded here, not in the pool: an alert touches the database, and
        # the pool's threads have no connection of their own to close.
        for component, future in pending.items():
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 -- any failure is the answer
                states[component] = record_failure(component, _describe(exc), alert=alert)
            else:
                states[component] = record_success(component, probed=True, alert=alert)
    return _render(states)


def snapshot(*, refresh=True, alert=False):
    """The API's answer. Asks SAP first if the stored one is stale.

    One worker probes at a time; the rest answer with what is stored rather
    than queueing behind a probe that may take its full timeout.
    """
    states = {c: current(c) for c in COMPONENTS}
    stale = any(
        time.time() - (s.get("checked_at") or 0) >= PROBE_INTERVAL for s in states.values()
    )
    if refresh and stale and _take_probe_lock():
        try:
            return probe(alert=alert)
        finally:
            _release_probe_lock()
    return _render(states)


def _take_probe_lock() -> bool:
    try:
        return bool(_cache().add(_PROBE_LOCK, 1, _PROBE_LOCK_TTL))
    except Exception:  # noqa: BLE001 -- no shared lock: probe anyway
        return True


def _release_probe_lock():
    try:
        _cache().delete(_PROBE_LOCK)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# presentation and alerts
# ---------------------------------------------------------------------------

def _iso(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=dt_timezone.utc).isoformat()


def _clock(epoch) -> str:
    if not epoch:
        return "a moment ago"
    moment = timezone.localtime(datetime.fromtimestamp(epoch, tz=dt_timezone.utc))
    if moment.date() == timezone.localdate():
        return moment.strftime("%H:%M")
    return moment.strftime("%d %b %H:%M")


def _render(states):
    components = {
        c: {
            "status": s.get("status", UNKNOWN),
            "since": _iso(s.get("since")),
            "checked_at": _iso(s.get("checked_at")),
            "error": s.get("error", ""),
        }
        for c, s in states.items()
    }
    return {
        "ok": all(v["status"] != DOWN for v in components.values()),
        "components": components,
    }


def _alert_recipients():
    """Every active superuser, and anyone put in the alert group.

    Superusers are the managers, so they hear without being added to anything;
    the group is only for the people who should hear and are not superusers.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    who = Q(is_superuser=True)
    group = getattr(settings, "SAP_HEALTH_ALERT_GROUP", "")
    if group:
        who |= Q(groups__name=group)
    return get_user_model().objects.filter(who, is_active=True).distinct()


def _alert(component, *, recovered, state):
    """Tell the people who answer for SAP. Never the reason a probe fails."""
    label = LABELS[component]
    if recovered:
        title = f"SAP {label} is answering again"
        body = f"It was down from {_clock(state.get('since'))}. Postings to SAP can be retried."
    else:
        title = f"SAP {label} is down"
        body = (
            f"Not answering since {_clock(state.get('since'))}: {state.get('error')}. "
            f"Postings to SAP fail until it is restarted on the SAP server."
            if component == SERVICE_LAYER
            else f"Not answering since {_clock(state.get('since'))}: {state.get('error')}. "
            f"SAP lists and stock figures in the app are empty until it is back."
        )
    try:
        from notifications.services import NotificationService

        NotificationService.send_notification_to_group(
            users=list(_alert_recipients()),
            title=title,
            body=body,
            reference_type="sap_health",
            extra_data={"component": component, "recovered": recovered},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not send the SAP health alert for %s", component)
