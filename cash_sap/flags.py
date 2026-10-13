"""The switches, read where they are used.

``config/settings.py`` is not edited on this branch, so every flag is read with
``getattr(settings, NAME, default)``. The defaults follow ``DEBUG`` for the two
that make a developer's machine useful (on, and simulated) and are safe on a
server: a production settings file with ``DEBUG=False`` and none of these set
gets the app switched off.

=============================== ================ =================================
Setting                         Default          Meaning
=============================== ================ =================================
``CASH_SAP_ENABLED``            ``DEBUG``        Kill switch. Off: nothing is sent,
                                                 queued units wait.
``CASH_SAP_SIMULATE``           ``DEBUG``        Dry run: build and store every
                                                 payload, call SAP for nothing.
``CASH_SAP_ALLOW_LIVE``         ``False``        Allow a non-``TEST_`` company DB.
``CASH_SAP_SEND_SERIES``        ``True``         Send ``Series`` for the doc month.
``CASH_SAP_UPPERCASE_COMMENTS`` ``False``        Upper-case the Comments text.
=============================== ================ =================================

ORDER OF CHECKS (binding, spec D8/D9): enabled -> simulate -> live guard. A
simulated send makes no SAP call at all, so it is allowed on any server; only
a real send has to pass :func:`require_target`.
"""

from django.conf import settings

from sap_purchasing import live_guard


def enabled() -> bool:
    return bool(getattr(settings, "CASH_SAP_ENABLED", settings.DEBUG))


def simulate() -> bool:
    return bool(getattr(settings, "CASH_SAP_SIMULATE", settings.DEBUG))


def allow_live() -> bool:
    return bool(getattr(settings, "CASH_SAP_ALLOW_LIVE", False))


def send_series() -> bool:
    return bool(getattr(settings, "CASH_SAP_SEND_SERIES", True))


def upper_comments() -> bool:
    return bool(getattr(settings, "CASH_SAP_UPPERCASE_COMMENTS", False))


def sap_company_codes() -> list:
    """The SAP companies the app can write into (the registry's keys)."""
    from sap_client.registry import COMPANY_SAP_REGISTRY

    return sorted(COMPANY_SAP_REGISTRY)


def is_known_company(code: str) -> bool:
    return bool(code) and code in sap_company_codes()


def require_target(code: str) -> None:
    """Raise ``ValueError`` unless a real send into ``code`` is allowed.

    Unknown company -> refused. Live company with ``CASH_SAP_ALLOW_LIVE`` off
    -> refused. Patch ``sap_purchasing.live_guard.CompanyContext`` in tests.
    """
    if not is_known_company(code):
        raise ValueError(f"{code or '(blank)'} is not a SAP company this app knows.")
    live_guard.require_test_target(
        code, allow_live=allow_live(), switch_name="CASH_SAP_ALLOW_LIVE"
    )


def is_test_target(code: str) -> bool:
    """True for a TEST_ company; False for live, unknown or unreadable config."""
    if not is_known_company(code):
        return False
    try:
        return live_guard.is_test_target(code)
    except Exception:  # noqa: BLE001 -- the registry refusing reads as "live"
        return False
