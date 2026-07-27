"""Outbound HTTP client for the external OMS invoice-approval service.

Modelled on ``sap_client/service_layer/file_uploader_client.py``: settings-driven
config, ``is_enabled()``/``_validate_config()``, explicit ``timeout=``, status-code
branching, and translation of ``requests`` errors into the domain exceptions in
``oms.exceptions``.

Auth is OFF by default (``OMS_AUTH_ENABLED=False``) because the OMS invoice
endpoints are currently treated as open. The JWT login/token-cache path is fully
implemented but dormant — flip ``OMS_AUTH_ENABLED=True`` and set
``OMS_USERNAME``/``OMS_PASSWORD`` (the shared "Factory" service account) to enable
it. See the plan's Risks #2/#7.

``OMS_SIMULATE=True`` short-circuits every method with fixtures so the whole flow
can be built/demoed/tested with no network — mirrors ``MARKETPLACE_SIMULATE_SAP``.
"""
import logging

import requests
from django.conf import settings
from django.core.cache import cache

from .exceptions import OMSConnectionError, OMSDataError, OMSValidationError

logger = logging.getLogger(__name__)

_TOKEN_CACHE_KEY = "oms:auth:access"  # single shared "Factory" account token

# Full status set OMS may return (superset of the 4 approver tabs).
VALID_STATUSES = {"PENDING", "APPROVED", "EDITED", "REJECTED", "ERROR", "POSTED_TO_SAP", "CL_RAISED"}
# The only statuses an approver can move an entry to.
DECISION_STATUSES = {"APPROVED", "REJECTED"}


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
        self.timeout = getattr(settings, "OMS_TIMEOUT_SECONDS", 30)
        self.token_ttl = getattr(settings, "OMS_TOKEN_TTL_SECONDS", 82800)
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
            resp = requests.post(
                url,
                json={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            logger.error("Could not connect to OMS for login: %s", exc)
            raise OMSConnectionError("Unable to connect to OMS") from exc
        except requests.exceptions.Timeout as exc:
            logger.error("OMS login timed out: %s", exc)
            raise OMSConnectionError("OMS login request timeout") from exc

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
            resp = requests.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
            if resp.status_code == 401 and self.auth_enabled:
                # Cached token was invalidated early — force a fresh login and retry once.
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(self._token(force_refresh=True)),
                    timeout=self.timeout,
                    **kwargs,
                )
        except requests.exceptions.ConnectionError as exc:
            logger.error("Could not connect to OMS: %s", exc)
            raise OMSConnectionError("Unable to connect to OMS") from exc
        except requests.exceptions.Timeout as exc:
            logger.error("OMS request timed out: %s", exc)
            raise OMSConnectionError("OMS request timeout") from exc
        return resp

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
