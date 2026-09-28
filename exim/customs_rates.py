"""The exchange rates customs has notified, read from the service EXIM used.

Customs (CBIC) notifies an import and an export rate per currency, usually
twice a month, and those rates, not the market's, are what a bill of entry or
shipping bill is valued at. EXIM read them from eximindiaonline.in, and so does
this: there is no official machine-readable feed.

The service answers for an exact notification date only (any other date comes
back empty), so this asks for the latest notification, as EXIM's page did.
A notification stands for a fortnight, so an answer is cached for an hour: the
page is opened and refreshed far more often than the rates change.
"""

import logging
from datetime import date

import requests
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import APIException

logger = logging.getLogger(__name__)

URL = "https://eximindiaonline.in:4000/customs/get_customs_website"
# The service refuses a request that does not look like it came from its own site.
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://eximin.net",
}
TIMEOUT_SECONDS = 15
CACHE_KEY = "exim:customs-rates:latest"
CACHE_SECONDS = 60 * 60


class CustomsRatesUnavailable(APIException):
    status_code = 502
    default_code = "customs_rates_unavailable"

    def __init__(self, reason):
        super().__init__(
            {
                "detail": "The customs exchange rates could not be read just now. Try again shortly.",
                "code": self.default_code,
                "context": {"reason": reason},
            }
        )


def _notified_on(value) -> str | None:
    """The service sends ``2026-09-18T00:00:00.000Z``; the date is all that matters."""
    parsed = parse_datetime(value or "")
    if parsed is not None:
        return parsed.date().isoformat()
    try:
        return date.fromisoformat((value or "")[:10]).isoformat()
    except ValueError:
        return None


def parse(payload) -> list[dict]:
    """The service's rows, cleaned: its figures carry stray spaces ("69.70 ")."""
    rows = []
    for row in (payload or {}).get("data") or []:
        currency = (row.get("currency") or "").strip()
        if not currency:
            continue
        rows.append(
            {
                "currency": currency,
                "import_rate": (row.get("import") or "").strip() or None,
                "export_rate": (row.get("export") or "").strip() or None,
                "notified_on": _notified_on(row.get("date")),
                "notification_no": (row.get("notification_no") or "").strip() or None,
            }
        )
    return rows


def _fetch() -> list[dict]:
    try:
        response = requests.post(URL, json={}, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return parse(response.json())
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Customs exchange rates could not be read: %s", exc)
        raise CustomsRatesUnavailable(type(exc).__name__) from exc


def latest(*, refresh=False) -> dict:
    """The latest notification's rates, from the cache unless ``refresh``."""
    if not refresh:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached
    rows = _fetch()
    # A currency the latest notification left out keeps an older rate (the
    # Norwegian Krone stood on July's while every other was September's), so
    # the notification is named from the rows of the latest date alone.
    notified = max((r["notified_on"] for r in rows if r["notified_on"]), default=None)
    numbers = sorted(
        {r["notification_no"] for r in rows if r["notified_on"] == notified and r["notification_no"]}
    )
    result = {
        "rates": rows,
        "notified_on": notified,
        "notification_no": ", ".join(numbers) or None,
        "fetched_at": timezone.now().isoformat(),
    }
    # An empty answer is not cached: it is more likely a hiccup than a
    # fortnight without rates.
    if rows:
        cache.set(CACHE_KEY, result, CACHE_SECONDS)
    return result
