import logging
import mimetypes
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from django.conf import settings

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

# The uploader writes into a Windows share (\\host\Attachments_*) that stalls
# intermittently: the request hangs for a minute or two and then either times
# out or comes back as a bare 500, while uploads a few minutes either side of
# it succeed. Every one of those failures has gone through on a manual retry,
# so the client retries for the operator instead of failing the whole GRPO post.
RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})

# Never start an attempt that has less time left than this - a two-second
# attempt only burns the budget that the previous failure already ate into.
MIN_ATTEMPT_SECONDS = 10

# How far before an attempt started we still accept a file as that attempt's
# own upload, to absorb clock skew between this host and the uploader host.
RECOVERY_SKEW_SECONDS = 60

# The reconciliation listing is small and instant on a healthy uploader, so it
# gets a short leash of its own: it runs on the failure path, where the time it
# spends is time the retry after it no longer has.
RECOVERY_TIMEOUT_SECONDS = 10


class FileUploaderClient:
    """Client for the SAP-server file uploader service."""

    def __init__(self, company_code: str):
        self.company_code = (company_code or "").upper()
        self.base_url = (
            getattr(settings, "SAP_FILE_UPLOADER_BASE_URL", "") or ""
        ).rstrip("/")
        self.api_key = getattr(settings, "SAP_FILE_UPLOADER_API_KEY", "") or ""
        self.timeout = getattr(settings, "SAP_FILE_UPLOADER_TIMEOUT_SECONDS", 30)
        self.max_attempts = max(
            1, getattr(settings, "SAP_FILE_UPLOADER_MAX_ATTEMPTS", 3)
        )
        self.retry_backoff = getattr(
            settings, "SAP_FILE_UPLOADER_RETRY_BACKOFF_SECONDS", 3
        )
        # Total wall clock the retries may use. Must stay clear of nginx's
        # proxy_read_timeout, because the SAP Service Layer calls that follow
        # the upload still have to fit inside the same browser request.
        self.total_budget = getattr(
            settings, "SAP_FILE_UPLOADER_TOTAL_BUDGET_SECONDS", 65
        )
        folder_ids = getattr(settings, "SAP_FILE_UPLOADER_FOLDER_IDS", {}) or {}
        self.folder_id = folder_ids.get(self.company_code)

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(getattr(settings, "SAP_FILE_UPLOADER_ENABLED", False))

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "X-Uploader": "factory_app_v2",
        }

    def _validate_config(self) -> None:
        missing = []
        if not self.base_url:
            missing.append("SAP_FILE_UPLOADER_BASE_URL")
        if not self.api_key:
            missing.append("SAP_FILE_UPLOADER_API_KEY")
        if not self.folder_id:
            missing.append(f"SAP_FILE_UPLOADER_FOLDER_ID_{self.company_code}")
        if missing:
            raise SAPValidationError(
                "SAP file uploader is enabled but missing config: "
                + ", ".join(missing)
            )

    def upload(self, file_path: str, filename: str) -> dict[str, Any]:
        self._validate_config()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        url = f"{self.base_url}/upload"
        deadline = time.monotonic() + self.total_budget

        attempt = 0
        while True:
            attempt += 1
            started_at = datetime.now(timezone.utc)
            try:
                response = self._post_file(
                    url=url,
                    file_path=file_path,
                    filename=filename,
                    content_type=content_type,
                    timeout=self._attempt_timeout(deadline),
                )
            except requests.exceptions.ConnectionError as exc:
                # ConnectTimeout lands here too, and reads the same to an
                # operator: the uploader never took the file.
                failure = SAPConnectionError("Unable to connect to SAP file uploader")
                logger.error("Could not connect to SAP file uploader: %s", exc)
            except requests.exceptions.Timeout as exc:
                failure = SAPConnectionError("SAP file uploader request timeout")
                logger.error("SAP file uploader request timed out: %s", exc)
            else:
                if response.status_code in (401, 403):
                    raise SAPConnectionError("SAP file uploader authentication failed")

                if response.status_code >= 400:
                    message = self._extract_error_message(response)
                    failure = SAPDataError(
                        f"SAP file uploader upload failed: {message}"
                    )
                    if response.status_code not in RETRYABLE_STATUS_CODES:
                        raise failure
                    logger.error(
                        "SAP file uploader returned %s for %s: %s",
                        response.status_code,
                        filename,
                        message,
                    )
                else:
                    return self._parse_upload_response(response)

            # The upload may have reached the SAP folder even though this
            # request failed, so look before uploading the same file again.
            recovered = self._recover_uploaded_file(
                file_path, filename, started_at, deadline
            )
            if recovered is not None:
                logger.warning(
                    "SAP file uploader attempt %s for %s failed (%s), but the "
                    "file did reach the SAP folder as %s (id %s); using it "
                    "instead of uploading a duplicate.",
                    attempt,
                    filename,
                    failure,
                    recovered.get("stored_name"),
                    recovered.get("id"),
                )
                return recovered

            if not self._wait_before_retry(attempt, deadline):
                raise failure

            logger.warning(
                "Retrying SAP file uploader for %s (attempt %s of %s) after: %s",
                filename,
                attempt + 1,
                self.max_attempts,
                failure,
            )

    def _post_file(
        self,
        *,
        url: str,
        file_path: str,
        filename: str,
        content_type: str,
        timeout: float,
    ):
        with open(file_path, "rb") as handle:
            return requests.post(
                url,
                data={"folder_id": self.folder_id},
                files={"files": (filename, handle, content_type)},
                headers=self._headers(),
                timeout=timeout,
            )

    def _parse_upload_response(self, response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SAPDataError("SAP file uploader returned invalid JSON") from exc

        files = (payload.get("data") or {}).get("files") or []
        if not files:
            failures = (payload.get("data") or {}).get("failures") or []
            raise SAPDataError(
                "SAP file uploader did not save the attachment"
                + (f": {failures}" if failures else "")
            )

        return files[0]

    def _attempt_timeout(self, deadline: float) -> float:
        """Per-attempt timeout, never overrunning the retry budget."""
        return max(
            float(MIN_ATTEMPT_SECONDS),
            min(float(self.timeout), deadline - time.monotonic()),
        )

    def _wait_before_retry(self, attempt: int, deadline: float) -> bool:
        """Sleep out the backoff, or report that this failure is now final."""
        if attempt >= self.max_attempts:
            return False
        delay = self.retry_backoff * attempt
        if time.monotonic() + delay + MIN_ATTEMPT_SECONDS > deadline:
            return False
        time.sleep(delay)
        return True

    def _recover_uploaded_file(
        self, file_path: str, filename: str, since: datetime, deadline: float
    ) -> Optional[dict[str, Any]]:
        """
        Return the file a failed attempt may still have saved, if any.

        A stalled share write can commit the file and leave the request hanging
        anyway, and uploading again then leaves a `<name>_v2` duplicate behind
        in the SAP attachments folder. `/files` takes no query filters, so this
        scans the listing for this uploader's own copy of the same file.
        """
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return None

        try:
            response = requests.get(
                f"{self.base_url}/files",
                headers=self._headers(),
                timeout=max(
                    2.0,
                    min(float(RECOVERY_TIMEOUT_SECONDS), deadline - time.monotonic()),
                ),
            )
        except requests.RequestException as exc:
            logger.warning(
                "Could not check whether %s reached the SAP folder: %s", filename, exc
            )
            return None

        if response.status_code >= 400:
            return None

        try:
            rows = response.json().get("data") or []
        except ValueError:
            return None

        cutoff = since - timedelta(seconds=RECOVERY_SKEW_SECONDS)
        uploader = self._headers()["X-Uploader"]
        matches = [
            row
            for row in rows
            if row.get("folder_id") == self.folder_id
            and row.get("original_name") == filename
            and row.get("size") == size
            and row.get("uploader") == uploader
            and self._uploaded_after(row.get("uploaded_at"), cutoff)
        ]
        if not matches:
            return None
        return max(matches, key=lambda row: row.get("id") or 0)

    @staticmethod
    def _uploaded_after(uploaded_at: Any, cutoff: datetime) -> bool:
        if not uploaded_at:
            return False
        try:
            stamp = datetime.fromisoformat(str(uploaded_at))
        except ValueError:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp >= cutoff

    def delete(self, file_id: int) -> None:
        if not file_id:
            return
        self._validate_config()
        try:
            response = requests.delete(
                f"{self.base_url}/files/{file_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning("Could not delete orphan uploader file %s: %s", file_id, exc)
            return

        if response.status_code >= 400:
            logger.warning(
                "Could not delete orphan uploader file %s: %s",
                file_id,
                self._extract_error_message(response),
            )

    @staticmethod
    def _extract_error_message(response) -> str:
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                return error.get("message") or str(error)
            return str(payload)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
