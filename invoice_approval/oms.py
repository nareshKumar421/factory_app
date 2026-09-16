"""Outbound HTTP client for the external OMS invoice-approval service.

Restored from the deleted ``oms`` app (see commit 6811ddd, which replaced the
proxy with direct SAP reads): the invoice-approval page now shows BOTH sources —
OMS invoice logs (this client) by default, and the SAP approval drafts behind a
toggle. Modelled on ``sap_client/service_layer/file_uploader_client.py``:
settings-driven config, ``is_enabled()``/``_validate_config()``, explicit
``timeout=``, status-code branching, and translation of ``requests`` errors into
the domain exceptions below.

Auth is OFF by default (``OMS_AUTH_ENABLED=False``) because the OMS invoice
read endpoints are treated as open. The JWT login/token-cache path is fully
implemented but dormant — flip ``OMS_AUTH_ENABLED=True`` and set
``OMS_USERNAME``/``OMS_PASSWORD`` (the shared "Factory" service account) to
enable it. Approve/reject on OMS records the request user, so a token-less
PATCH 500s there — decisions effectively require auth to be enabled.

``OMS_SIMULATE=True`` short-circuits every method with fixtures so the whole
flow can be built/demoed/tested with no network — mirrors
``MARKETPLACE_SIMULATE_SAP``.

**OMS rate-limits us, and every factory user shares one bucket.** OMS runs a DRF
throttle that answers ``429`` with a ``Retry-After``; because we call it
anonymously (``OMS_AUTH_ENABLED=False``), DRF keys that bucket on the *client IP*
— and all of this app's OMS traffic leaves the one prod server. So the quota is
shared by every approver at once, and beyond the app throttle the OMS host also
blackholes new TCP connections for a while after a burst (a dropped SYN, not a
refusal, so the caller hangs until its own connect timeout). Three things here
exist because of that, and should not be quietly undone:

* one module-level :data:`_SESSION` so calls reuse a kept-alive connection
  instead of opening a fresh one every time;
* a **connect** timeout separate from — and much shorter than — the read
  timeout, so a blackholed SYN fails fast instead of burning a whole worker;
* :class:`OMSThrottledError`, so "OMS is rate-limiting us, try again in N
  seconds" reaches the approver as itself rather than as a blank 502.

Both timeouts must stay comfortably under the frontend's 30s axios timeout. If
they don't, the browser gives up first and the user sees a generic client-side
timeout instead of whatever we were about to tell them.
"""
import logging

import requests
from django.conf import settings
from django.core.cache import cache
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

_TOKEN_CACHE_KEY = "oms:auth:access"  # single shared "Factory" account token

# One connection pool for the whole process. Keep-alive matters here beyond the
# usual handshake saving: the OMS host counts *connections*, and a new one per
# call is what trips its burst protection.
_SESSION = requests.Session()
_SESSION.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8))
_SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8))

# Full status set OMS may return (superset of the approver tabs).
VALID_STATUSES = {"PENDING", "APPROVED", "EDITED", "REJECTED", "ERROR", "POSTED_TO_SAP", "CL_RAISED"}
# The only statuses an approver can move an entry to.
DECISION_STATUSES = {"APPROVED", "REJECTED"}


class OMSValidationError(Exception):
    """Bad request (invalid input, or OMS returned 400 e.g. REJECTED without a reason)."""
    pass


class OMSConnectionError(Exception):
    """OMS unreachable, timed out, or authentication failed."""
    pass


class OMSThrottledError(OMSConnectionError):
    """OMS refused the call with 429 — we are over its rate limit, not broken.

    Subclasses :class:`OMSConnectionError` so any existing ``except`` clause
    still catches it; callers that want to say *how long* to wait read
    :attr:`retry_after` (seconds, from OMS's ``Retry-After`` header, or ``None``
    when it didn't send one).
    """

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class OMSDataError(Exception):
    """OMS reachable but returned an unexpected/invalid response."""
    pass


# ── Simulate fixtures (no network) ───────────────────────────────────────────
def _sim_line(item_code, qty, batch, warehouse):
    return {
        "LineNum": 1,
        "TaxCode": "IGST@5",
        "ItemCode": item_code,
        "Quantity": qty,
        "BatchNumbers": [{"Quantity": qty, "BatchNumber": batch}],
        "WarehouseCode": warehouse,
    }


def _sim_invoice(pk, so, party, amount, status, warehouse, lines, rejection_reason=None):
    return {
        "id": pk,
        "so_number": so,
        "party_name": party,
        "total_amount": amount,
        "branch": "OIL",
        "warehouse": warehouse,
        "status": status,
        "rejection_reason": rejection_reason,
        "error_message": None,
        "invoice_payload": {
            "Series": 0,
            "DocDate": "2026-07-20",
            "CardCode": "CUST000357",
            "DocDueDate": "2026-07-20",
            "DocObjectCode": "13",
            "DocumentLines": lines,
        },
        "created_at": "2026-07-20T12:35:06Z",
        "created_by": 8,
    }


_SIM_INVOICES = [
    _sim_invoice(74, "1726056787", "G PURE INDIA", "104000.00", "PENDING", "GP-FG",
                 [_sim_line("FG0000008", 50, "L4002040 052631 01", "GP-FG")]),
    _sim_invoice(75, "1726036511", "AVENUE SUPERMARTS LTD", "17200.00", "PENDING", "GP-FG",
                 [_sim_line("FG0000012", 20, "L4002041 052640 02", "GP-FG"),
                  _sim_line("FG0000019", 8, "L4002090 052701 01", "GP-FG")]),
    _sim_invoice(76, "1726036777", "RELIANCE RETAIL LTD", "58900.00", "EDITED", "JB-FG",
                 [_sim_line("FG0000031", 30, "L4102040 062631 01", "JB-FG")]),
    _sim_invoice(70, "1726010112", "D MART WHOLESALE", "42000.00", "APPROVED", "GP-FG",
                 [_sim_line("FG0000008", 25, "L4002040 052631 01", "GP-FG")]),
    _sim_invoice(68, "1726009980", "SPENCER RETAIL", "9800.00", "REJECTED", "GP-FG",
                 [_sim_line("FG0000012", 5, "L4002041 052640 02", "GP-FG")],
                 rejection_reason="Batch not physically available at GP-FG."),
]


class OmsClient:
    """Proxy client for the external OMS invoice-approval service."""

    def __init__(self):
        self.base_url = (getattr(settings, "OMS_BASE_URL", "") or "").rstrip("/")
        self.username = getattr(settings, "OMS_USERNAME", "") or ""
        self.password = getattr(settings, "OMS_PASSWORD", "") or ""
        # (connect, read). Connect is the short one on purpose — a blackholed
        # SYN is the failure mode we actually see, and without its own budget it
        # would hold a worker for the full read timeout before giving up.
        self.timeout = (
            getattr(settings, "OMS_CONNECT_TIMEOUT_SECONDS", 5),
            getattr(settings, "OMS_TIMEOUT_SECONDS", 10),
        )
        self.token_ttl = getattr(settings, "OMS_TOKEN_TTL_SECONDS", 82800)
        self.count_cache_ttl = getattr(settings, "OMS_PENDING_COUNT_CACHE_SECONDS", 60)
        self.simulate = bool(getattr(settings, "OMS_SIMULATE", False))
        self.auth_enabled = bool(getattr(settings, "OMS_AUTH_ENABLED", False))

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(getattr(settings, "OMS_ENABLED", False))

    def _validate_config(self) -> None:
        if self.simulate:
            return
        missing = []
        if not self.base_url:
            missing.append("OMS_BASE_URL")
        if self.auth_enabled:
            if not self.username:
                missing.append("OMS_USERNAME")
            if not self.password:
                missing.append("OMS_PASSWORD")
        if missing:
            raise OMSValidationError(
                "OMS is enabled but missing config: " + ", ".join(missing)
            )

    # ── Auth (dormant unless OMS_AUTH_ENABLED) ────────────────────────────────
    def _login(self) -> str:
        url = f"{self.base_url}/api/auth/login/"
        try:
            resp = _SESSION.post(
                url,
                json={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            # Before ConnectionError: requests' ConnectTimeout inherits from
            # both, and "timed out" is the more useful half of the truth.
            logger.error("OMS login timed out: %s", exc)
            raise OMSConnectionError("OMS login request timeout") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("Could not connect to OMS for login: %s", exc)
            raise OMSConnectionError("Unable to connect to OMS") from exc

        self._raise_if_throttled(resp)
        if resp.status_code in (400, 401, 403):
            raise OMSConnectionError("OMS authentication failed")
        if resp.status_code >= 400:
            raise OMSDataError(f"OMS login failed: {self._err(resp)}")
        try:
            access = resp.json()["data"]["tokens"]["access"]
        except (ValueError, KeyError, TypeError) as exc:
            raise OMSDataError("OMS login returned an unexpected response") from exc

        cache.set(_TOKEN_CACHE_KEY, access, timeout=self.token_ttl)
        return access

    def _token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = cache.get(_TOKEN_CACHE_KEY)
            if cached:
                return cached
        return self._login()

    def _headers(self, token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_enabled:
            headers["Authorization"] = f"Bearer {token or self._token()}"
        return headers

    # ── Generic request with retry-once-on-401 (when auth is enabled) ─────────
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        self._validate_config()
        url = f"{self.base_url}{path}"
        try:
            resp = _SESSION.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
            if resp.status_code == 401 and self.auth_enabled:
                # Cached token was invalidated early — force a fresh login and retry once.
                resp = _SESSION.request(
                    method,
                    url,
                    headers=self._headers(self._token(force_refresh=True)),
                    timeout=self.timeout,
                    **kwargs,
                )
        except requests.exceptions.Timeout as exc:
            # Checked before ConnectionError: ConnectTimeout subclasses both, and
            # logging a connect timeout as "could not connect" hides that we sat
            # there waiting — the difference matters when diagnosing this.
            logger.error("OMS request timed out (%s %s): %s", method, path, exc)
            raise OMSConnectionError("OMS request timeout") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("Could not connect to OMS (%s %s): %s", method, path, exc)
            raise OMSConnectionError("Unable to connect to OMS") from exc
        # Centralised so every caller below gets it — throttling is not a
        # per-endpoint concern, and it must never read as "OMS sent us junk".
        self._raise_if_throttled(resp)
        return resp

    @staticmethod
    def _raise_if_throttled(resp) -> None:
        """Turn OMS's 429 into a throttle error carrying its own Retry-After."""
        if resp.status_code != 429:
            return
        try:
            retry_after = int(resp.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            retry_after = None
        logger.warning(
            "OMS throttled us (429), retry after %ss. This quota is per source IP "
            "and shared by every user of this app.",
            retry_after if retry_after is not None else "?",
        )
        wait = (
            f" Try again in about {retry_after} seconds."
            if retry_after is not None
            else " Try again shortly."
        )
        raise OMSThrottledError(
            "OMS is limiting how often it can be called right now." + wait,
            retry_after=retry_after,
        )

    # ── Public API ────────────────────────────────────────────────────────────
    def list_invoices(self, warehouse: str, status: str | None = None) -> list:
        """GET /api/invoice/all/ — invoices for a warehouse, optionally by status.

        OMS requires the ``whs`` (warehouse code) query param and 400s without it.
        """
        if not (warehouse or "").strip():
            raise OMSValidationError("warehouse (whs) is required")
        if self.simulate:
            return [
                i
                for i in _SIM_INVOICES
                if i["warehouse"] == warehouse and (not status or i["status"] == status)
            ]
        params = {"whs": warehouse}
        if status:
            params["status"] = status
        resp = self._request("GET", "/api/invoice/all/", params=params)
        return self._json_list(resp)

    # ── Pending count (cached — it is a background poll, not a page read) ──────
    @staticmethod
    def _count_cache_key(warehouse: str) -> str:
        return f"oms:pending-count:{(warehouse or '').strip().upper()}"

    def pending_count(self, warehouse: str) -> int:
        """Number of PENDING invoices at ``warehouse``, cached briefly.

        This drives the sidebar badge, which every user polls from every page in
        the app — and OMS has no count endpoint, so each poll used to pull the
        whole PENDING list down again, duplicating the request the page itself
        had just made. Caching the integer collapses all of that into one OMS
        call per warehouse per ``OMS_PENDING_COUNT_CACHE_SECONDS``.

        A badge may lag its list by that window; a decision made through this app
        clears the key (see :meth:`invalidate_pending_count`), so the lag only
        ever applies to work done elsewhere. On the default per-process
        LocMemCache the saving is per worker — configure a shared ``CACHES``
        backend to get the full benefit.
        """
        if self.simulate:
            return len(self.list_invoices(warehouse=warehouse, status="PENDING"))

        key = self._count_cache_key(warehouse)
        cached = cache.get(key)
        if cached is not None:
            return cached
        count = len(self.list_invoices(warehouse=warehouse, status="PENDING"))
        if self.count_cache_ttl:
            cache.set(key, count, timeout=self.count_cache_ttl)
        return count

    @classmethod
    def invalidate_pending_count(cls, warehouse: str) -> None:
        """Drop the cached count so the badge reflects a decision immediately."""
        if warehouse:
            cache.delete(cls._count_cache_key(warehouse))

    def update_status(
        self,
        invoice_id,
        status: str,
        rejection_reason: str | None = None,
        user: str | None = None,
    ) -> dict:
        """PATCH /api/invoice/<id>/update-status/ — approve or reject.

        ``user`` is the approver's display name; OMS stores it as the history
        author (its ``created_by`` is a free-text name, not a linked account).
        """
        if status not in DECISION_STATUSES:
            raise OMSValidationError("status must be APPROVED or REJECTED")
        if status == "REJECTED" and not (rejection_reason or "").strip():
            raise OMSValidationError("rejection_reason is required when status is REJECTED")

        body = {"status": status}
        if user:
            body["user"] = user
        if status == "REJECTED":
            body["rejection_reason"] = rejection_reason

        if self.simulate:
            return {"message": "Status updated successfully"}

        resp = self._request("PATCH", f"/api/invoice/{invoice_id}/update-status/", json=body)
        if resp.status_code == 400:
            raise OMSValidationError(self._err(resp))
        if resp.status_code == 404:
            raise OMSDataError(f"OMS invoice {invoice_id} not found")
        if resp.status_code in (401, 403):
            raise OMSConnectionError("OMS authentication failed")
        if resp.status_code >= 400:
            raise OMSDataError(f"OMS status update failed: {self._err(resp)}")
        return self._json_obj(resp)

    def get_history(self, invoice_id) -> list:
        """GET /api/invoice/history/<id>/ — the audit trail for one invoice."""
        if self.simulate:
            return [
                {
                    "id": 1,
                    "created_by_name": "billing",
                    "so_number": "1726036511",
                    "party_name": "AVENUE SUPERMARTS LTD",
                    "total_amount": "17200.00",
                    "status": "PENDING",
                    "invoice_payload": {},
                    "created_at": "2026-07-20T12:35:06Z",
                    "invoice_log": invoice_id,
                    "created_by": 8,
                },
                {
                    "id": 2,
                    "created_by_name": "Factory",
                    "so_number": "1726036511",
                    "party_name": "AVENUE SUPERMARTS LTD",
                    "total_amount": "17200.00",
                    "status": "APPROVED",
                    "invoice_payload": {},
                    "created_at": "2026-07-21T06:25:33Z",
                    "invoice_log": invoice_id,
                    "created_by": 97,
                },
            ]
        resp = self._request("GET", f"/api/invoice/history/{invoice_id}/")
        if resp.status_code == 404:
            raise OMSDataError(f"OMS invoice {invoice_id} not found")
        return self._json_list(resp)

    # ── Response helpers (mirror FileUploaderClient._extract_error_message) ────
    def _json_list(self, resp) -> list:
        if resp.status_code in (401, 403):
            raise OMSConnectionError("OMS authentication failed")
        if resp.status_code >= 400:
            raise OMSDataError(f"OMS request failed: {self._err(resp)}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise OMSDataError("OMS returned invalid JSON") from exc
        if not isinstance(data, list):
            raise OMSDataError("OMS returned an unexpected (non-array) response")
        return data

    def _json_obj(self, resp) -> dict:
        try:
            payload = resp.json()
        except ValueError:
            return {"message": "Status updated successfully"}
        return payload if isinstance(payload, dict) else {"message": str(payload)}

    @staticmethod
    def _err(resp) -> str:
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                return payload.get("message") or payload.get("detail") or str(payload)
            return str(payload)
        except Exception:
            return resp.text or f"HTTP {resp.status_code}"
