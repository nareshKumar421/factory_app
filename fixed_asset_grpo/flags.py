"""The Fixed Asset GRPO switches, read from settings without editing settings.py.

Every flag is read with ``getattr(settings, NAME, default)`` on every call, so
``override_settings`` in a test and a settings change on the next start both
take effect without anything being cached here.

| Flag | Default | Meaning |
|---|---|---|
| ``FIXED_ASSET_GRPO_ENABLED`` | ``DEBUG`` | Off: the post endpoint answers 400 ``FA_GRPO_DISABLED`` and the queue handler leaves rows waiting. |
| ``FIXED_ASSET_GRPO_SIMULATE`` | ``DEBUG`` | On: every check runs (SAP is still *read*), nothing is *written* to SAP, and the GRPO is recorded as a simulated post that history can discard. |
| ``FIXED_ASSET_GRPO_ALLOW_LIVE`` | ``False`` | Off: a real send only goes to a company whose Service Layer database and HANA schema both start with ``TEST_``. |
| ``FIXED_ASSET_GRPO_CUTOVER`` | ``""`` | ``YYYY-MM-DD``. FA gate entries that came in before it are hidden from the lists and refused by post: they are keyed in SAP by hand. |
| ``FIXED_ASSET_WAREHOUSES`` | ``"BH-FA,DL-FA,GP-FA"`` | The warehouses whose open PO lines count as fixed-asset lines (read by ``sap_purchasing.fa_po_reader``). |

ORDER OF CHECKS (spec D8/D9), binding for the handler: enabled, then simulate,
then the live guard. Simulate comes before the guard so a dry run works on a
server that points at live SAP: a simulated post writes nothing there.
"""

from datetime import date
from typing import Optional

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

ENABLED = "FIXED_ASSET_GRPO_ENABLED"
SIMULATE = "FIXED_ASSET_GRPO_SIMULATE"
ALLOW_LIVE = "FIXED_ASSET_GRPO_ALLOW_LIVE"
CUTOVER = "FIXED_ASSET_GRPO_CUTOVER"


def enabled() -> bool:
    return bool(getattr(settings, ENABLED, settings.DEBUG))


def simulate() -> bool:
    return bool(getattr(settings, SIMULATE, settings.DEBUG))


def allow_live() -> bool:
    return bool(getattr(settings, ALLOW_LIVE, False))


def cutover() -> Optional[date]:
    """The cut-over date, or None when it is not set.

    A value that is not an ISO date is a configuration mistake, and guessing
    would either hide entries that should post or post entries already keyed
    by hand. So it raises ``ImproperlyConfigured`` at first use instead.
    """
    raw = getattr(settings, CUTOVER, "")
    if raw in (None, ""):
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"{CUTOVER} must be a date as YYYY-MM-DD (or empty); got {raw!r}."
        ) from exc


def warehouses() -> list:
    """The configured fixed-asset warehouse codes (``FIXED_ASSET_WAREHOUSES``)."""
    from sap_purchasing.fa_po_reader import fixed_asset_warehouses

    return fixed_asset_warehouses()


def require_live_allowed(company_code: str) -> None:
    """The live-SAP guard for a real send. Raises ``ValueError`` when refused.

    ``sap_purchasing.live_guard`` reads the company's actual Service Layer
    database and HANA schema (``CompanyContext``), not ``settings.COMPANY_DB``.
    An unknown company code raises ``SAPValidationError``; callers treat both
    as a refusal.
    """
    from sap_purchasing import live_guard

    live_guard.require_test_target(
        company_code, allow_live=allow_live(), switch_name=ALLOW_LIVE
    )


def is_test_db(company_code: str) -> Optional[bool]:
    """Whether the company's SAP targets are TEST_ databases; None if unknown.

    For the screen's badge only, so a registry problem is reported as "not
    known" rather than breaking the entry page. The send itself goes through
    ``require_live_allowed``, which never swallows anything.
    """
    from sap_purchasing import live_guard

    try:
        return live_guard.is_test_target(company_code)
    except Exception:  # noqa: BLE001 -- a badge must not break the page
        return None


def flags_for(company_code: str) -> dict:
    """What the FA screens need to know to enable or explain the Post button."""
    cut = cutover()
    return {
        "enabled": enabled(),
        "simulate": simulate(),
        "allow_live": allow_live(),
        "is_test_db": is_test_db(company_code),
        "cutover": cut.isoformat() if cut else None,
        "warehouses": warehouses(),
    }
