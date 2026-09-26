"""A Service Layer client for the screens ported from SAP Portal.

The writers in this package log in on every call and never log out
(``auth.py``). That suits a handful of posts a day. The document browser,
budget screen and approvals inbox ported from SAP Portal read the Service Layer
several times per page view instead, and logging in for each read opens a new
SAP session every time. The portal kept one session cookie per company and
logged in again when SAP answered 401; this does the same, per process, keyed
by (URL, company DB, user) and dropped a little before SAP's default 30-minute
session timeout.

Only code written for the merge uses this client. The existing writers keep
their own per-call login, unchanged.

Error mapping follows ``approval_writer.py`` rather than the older writers:

* 400 → ``SAPValidationError`` with SAP's own words.
* 401/403 → one fresh login and retry (a cached session can expire); if SAP
  refuses again it has answered, so that is a refusal (``SAPValidationError``),
  not an outage.
* 404 → ``SAPValidationError`` naming what was not found (reads can opt into
  ``None`` instead).
* Transport failures and timeouts → ``SAPConnectionError``. A write that timed
  out may still have committed, and the message says so.
"""

import logging
import threading
import time
from decimal import Decimal
from urllib.parse import urlparse

import requests

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .auth import ServiceLayerSession

logger = logging.getLogger(__name__)

# SAP's default Service Layer session timeout is 30 minutes of inactivity.
SESSION_TTL_SECONDS = 20 * 60

READ_TIMEOUT_SECONDS = 30
WRITE_TIMEOUT_SECONDS = 120

_sessions: dict[tuple, tuple] = {}
_sessions_lock = threading.Lock()


def _session_key(sl_config: dict) -> tuple:
    return (sl_config["base_url"], sl_config["company_db"], sl_config["username"])


def _json_ready(obj):
    """Decimals become floats; everything else passes through."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _json_ready(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_ready(v) for v in obj]
    return obj


def odata_string(value) -> str:
    """Quote ``value`` as an OData string literal (single quotes doubled).

    The portal put raw text into keys like ``ProductTrees('<code>')`` and into
    ``$filter`` expressions, so a quote in the value broke out of the literal.
    """
    return "'" + str(value).replace("'", "''") + "'"


def clear_session_cache() -> None:
    """Forget every cached session (tests, or after a credentials change)."""
    with _sessions_lock:
        _sessions.clear()


class ServiceLayerEntityClient:
    """GET/POST/PATCH/PUT/DELETE on Service Layer entities for one company."""

    def __init__(self, context, sl_config: dict | None = None, *, cache_session: bool = True):
        """``cache_session=False`` for credentials a person typed: every call must
        then prove the password again, instead of riding an earlier session."""
        self.context = context
        self.sl_config = sl_config or context.service_layer
        self.cache_session = cache_session
        self.base = f"{self.sl_config['base_url']}/b1s/v2"

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _cookies(self, force: bool = False):
        key = _session_key(self.sl_config)
        now = time.monotonic()
        if not self.cache_session:
            force = True
        if not force:
            with _sessions_lock:
                cached = _sessions.get(key)
            if cached and cached[1] > now:
                return cached[0]
        try:
            cookies = ServiceLayerSession(self.sl_config).login()
        except requests.exceptions.Timeout as e:
            logger.error("Service Layer login timed out: %s", e)
            raise SAPConnectionError("SAP Service Layer login timed out")
        except requests.exceptions.ConnectionError as e:
            logger.error("Service Layer login could not connect: %s", e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.HTTPError as e:
            detail = extract_error_message(e.response) if e.response is not None else str(e)
            logger.error(
                "Service Layer refused the login for %s: %s", self.sl_config.get("username"), detail
            )
            raise SAPValidationError(
                f"SAP refused the Service Layer login for user "
                f"'{self.sl_config.get('username')}': {detail}"
            )
        if self.cache_session:
            with _sessions_lock:
                _sessions[key] = (cookies, now + SESSION_TTL_SECONDS)
        return cookies

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, path: str, params: dict | None = None, *, not_found_ok: bool = False):
        """GET one entity or collection page; ``None`` on 404 when ``not_found_ok``."""
        response = self._send("GET", path, params=params, timeout=READ_TIMEOUT_SECONDS)
        if response.status_code == 404 and not_found_ok:
            return None
        self._raise_for(response, f"read {path}")
        try:
            return response.json()
        except ValueError as e:
            raise SAPDataError(f"SAP returned an unreadable answer for {path}") from e

    def get_page(
        self,
        entity: str,
        *,
        select: str = "",
        filter: str = "",
        orderby: str = "",
        expand: str = "",
        top: int | None = None,
        skip: int | None = None,
    ) -> tuple[list, str | None]:
        """One page of a collection, and the next page's link if SAP has one."""
        params = {}
        if select:
            params["$select"] = select
        if filter:
            params["$filter"] = filter
        if orderby:
            params["$orderby"] = orderby
        if expand:
            params["$expand"] = expand
        if top:
            params["$top"] = int(top)
        if skip:
            params["$skip"] = int(skip)
        data = self.get(entity, params=params) or {}
        return data.get("value") or [], data.get("@odata.nextLink")

    def get_all(self, entity: str, *, max_pages: int = 50, **query) -> list:
        """Every row of a collection, following ``@odata.nextLink`` (capped)."""
        rows, next_link = self.get_page(entity, **query)
        pages = 1
        while next_link and pages < max_pages:
            data = self.get(self._relative(next_link)) or {}
            rows.extend(data.get("value") or [])
            next_link = data.get("@odata.nextLink")
            pages += 1
        return rows

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def post(self, path: str, payload: dict, *, timeout: int = WRITE_TIMEOUT_SECONDS, label: str = ""):
        response = self._send("POST", path, json=_json_ready(payload), timeout=timeout, write=True)
        self._raise_for(response, label or f"create {path}")
        return response.json() if response.content else {}

    def patch(
        self,
        path: str,
        payload: dict,
        *,
        headers: dict | None = None,
        timeout: int = WRITE_TIMEOUT_SECONDS,
        label: str = "",
    ) -> None:
        response = self._send(
            "PATCH", path, json=_json_ready(payload), headers=headers, timeout=timeout, write=True
        )
        self._raise_for(response, label or f"update {path}")

    def put(self, path: str, payload: dict, *, timeout: int = WRITE_TIMEOUT_SECONDS, label: str = "") -> None:
        response = self._send("PUT", path, json=_json_ready(payload), timeout=timeout, write=True)
        self._raise_for(response, label or f"replace {path}")

    def delete(self, path: str, *, timeout: int = WRITE_TIMEOUT_SECONDS, label: str = "") -> None:
        response = self._send("DELETE", path, timeout=timeout, write=True)
        self._raise_for(response, label or f"delete {path}")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _relative(self, link: str) -> str:
        """``@odata.nextLink`` comes back absolute or relative; keep the part after b1s/v2."""
        if link.startswith("http"):
            parsed = urlparse(link)
            path = parsed.path.split("/b1s/v2/", 1)[-1]
            return f"{path}?{parsed.query}" if parsed.query else path
        return link.split("/b1s/v2/", 1)[-1].lstrip("/")

    def _send(self, method, path, *, params=None, json=None, headers=None, timeout, write=False):
        url = f"{self.base}/{path.lstrip('/')}"
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        for attempt in (1, 2):
            cookies = self._cookies(force=attempt == 2)
            try:
                response = requests.request(
                    method, url, params=params, json=json, headers=request_headers,
                    cookies=cookies, timeout=timeout, verify=False,
                )
            except requests.exceptions.Timeout as e:
                logger.error("Service Layer %s %s timed out: %s", method, path, e)
                if write:
                    raise SAPConnectionError(
                        "SAP did not answer in time. It may still have saved the change — "
                        "check SAP before trying again."
                    )
                raise SAPConnectionError("SAP Service Layer request timeout")
            except requests.exceptions.ConnectionError as e:
                logger.error("Service Layer %s %s could not connect: %s", method, path, e)
                raise SAPConnectionError("Unable to connect to SAP Service Layer")
            # A cached session may have expired: log in again once and retry.
            # A 401 means SAP did not process the request, so a retry is safe.
            if response.status_code == 401 and attempt == 1:
                continue
            return response
        return response

    def _raise_for(self, response, action: str) -> None:
        if response.status_code < 400:
            return
        message = extract_error_message(response)
        if response.status_code == 400:
            logger.error("SAP refused to %s: %s", action, message)
            raise SAPValidationError(message)
        if response.status_code in (401, 403):
            logger.error("SAP refused to %s (auth): %s", action, message)
            raise SAPValidationError(f"SAP refused to {action}: {message}")
        if response.status_code == 404:
            raise SAPValidationError(f"SAP could not find what was asked for ({action}): {message}")
        logger.error("SAP error trying to %s: %s", action, message)
        raise SAPDataError(f"SAP could not {action}: {message}")


def extract_error_message(response) -> str:
    """SAP's error text, with its numeric code when it has one."""
    if response is None:
        return ""
    try:
        data = response.json()
    except Exception:
        return response.text or f"HTTP {response.status_code}"
    error = data.get("error") if isinstance(data, dict) else None
    if not error:
        return str(data)
    message = error.get("message")
    if isinstance(message, dict):
        message = message.get("value")
    message = str(message or data)
    code = error.get("code")
    if code not in (None, "") and str(code) not in message:
        return f"({code}) {message}"
    return message
