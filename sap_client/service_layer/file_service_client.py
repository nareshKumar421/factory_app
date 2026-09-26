"""Download SAP document attachments from the attachment file service.

Ported from SAP Portal's ``services/fileServiceClient.js``. SAP stores only a
path for each attachment line (``ATC1``); the file itself sits on a Windows
share this Linux server cannot read directly. A small HTTP service on the SAP
side serves those files. This is the *download* side — JI's existing
``file_uploader_client.py`` talks to a different service (other port, API key)
that only accepts uploads.

Two lookups, as in the portal:

* by entry — ``/files/by-entry/{AbsEntry}/{Line}``. Preferred: short reused
  names such as ``1825.pdf`` collide across documents, so a name lookup can
  return another document's file, while ``AbsEntry + Line`` is unique
  (``backend_v1/docs/attachment-entry-lookup.md``).
* by name — ``/files/{name}``, retrying ``<name>_compressed.<ext>`` on 404,
  because some originals are archived only in compressed form.

The service answers with the raw file or with a ZIP wrapping it; both come back
here as ``{"data", "file_name", "content_type"}``.

The portal's second fallback — reading the share with ``smbclient`` — is not
ported: it needs a system package and share credentials on this server. See
``sap_client/docs/sap_portal_port.md``.
"""

import io
import json
import logging
import mimetypes
import os
import re
import zipfile
from urllib.parse import quote

import requests
from django.conf import settings

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

MAX_BYTES = 100 * 1024 * 1024

# Characters scanners and mail clients leave in SAP file names that the file
# server cannot put in its latin-1 download header. Named so the error says
# exactly which byte to look for (backend_v1/docs/file-server-unicode-filenames.md).
_UNENCODABLE_NAMES = {
    0x202F: "NARROW NO-BREAK SPACE",
    0x2013: "EN DASH",
    0x2014: "EM DASH",
    0x2019: "RIGHT SINGLE QUOTATION MARK",
    0x20B9: "INDIAN RUPEE SIGN",
    0x0964: "DEVANAGARI DANDA",
    0xFFFD: "REPLACEMENT CHARACTER",
}


def describe_unencodable_chars(file_name: str) -> list[str]:
    """``["U+202F NARROW NO-BREAK SPACE", ...]`` for characters above latin-1."""
    seen = []
    for ch in file_name or "":
        cp = ord(ch)
        if cp > 0xFF and cp not in seen:
            seen.append(cp)
    labels = []
    for cp in seen:
        hex_code = f"U+{cp:04X}"
        labels.append(f"{hex_code} {_UNENCODABLE_NAMES[cp]}" if cp in _UNENCODABLE_NAMES else hex_code)
    return labels


def compressed_variant(file_name: str) -> str | None:
    """``2858.pdf`` → ``2858_compressed.pdf``; None if it already is one."""
    name = file_name or ""
    if re.search(r"_compressed(\.[^.]+)?$", name, re.IGNORECASE):
        return None
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{name}_compressed"
    return f"{stem}_compressed.{ext}"


def guess_content_type(file_name: str) -> str:
    return mimetypes.guess_type(file_name or "")[0] or "application/octet-stream"


class SapFileServiceClient:
    """Fetch one attachment file for one company."""

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.base_url = (getattr(settings, "SAP_FILE_SERVICE_BASE_URL", "") or "").rstrip("/")
        self.timeout = getattr(settings, "SAP_FILE_SERVICE_TIMEOUT_SECONDS", 60)
        ids = getattr(settings, "SAP_FILE_SERVICE_COMPANY_IDS", {}) or {}
        self.company_id = ids.get(company_code)

    def _require_configured(self) -> None:
        if not self.base_url:
            raise SAPConnectionError(
                "Attachment downloads are not configured on this server "
                "(SAP_FILE_SERVICE_BASE_URL is empty)."
            )
        if not self.company_id:
            raise SAPValidationError(
                f"No attachment file-service company id is configured for {self.company_code}."
            )

    def fetch_by_entry(self, abs_entry: int, line: int, file_name: str = "") -> dict:
        """The exact file for ``ATC1.AbsEntry`` + ``Line``."""
        self._require_configured()
        try:
            entry = int(abs_entry)
        except (TypeError, ValueError):
            raise SAPValidationError("The attachment entry is missing or not a number.")
        try:
            line_no = int(line)
        except (TypeError, ValueError):
            line_no = 0
        url = f"{self.base_url}/files/by-entry/{entry}/{line_no}"
        response = self._get(url, file_name)
        return self._unwrap(response, file_name or f"attachment-{entry}-{line_no}")

    def fetch_by_name(self, file_name: str) -> dict:
        """A file by its stored name, trying the ``_compressed`` copy on 404."""
        self._require_configured()
        if not file_name:
            raise SAPValidationError("The attachment file name is missing.")
        try:
            response = self._get(f"{self.base_url}/files/{quote(file_name)}", file_name)
        except SAPValidationError as e:
            fallback = compressed_variant(file_name) if getattr(e, "status", None) == 404 else None
            if not fallback:
                raise
            logger.warning("Attachment %s not found, trying %s", file_name, fallback)
            response = self._get(f"{self.base_url}/files/{quote(fallback)}", fallback)
            file_name = fallback
        return self._unwrap(response, file_name)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get(self, url: str, file_name: str):
        try:
            response = requests.get(
                url,
                params={"company": self.company_id},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                stream=True,
            )
        except requests.exceptions.Timeout:
            raise SAPConnectionError("The attachment file service did not answer in time.")
        except requests.exceptions.RequestException as e:
            logger.error("Attachment file service unreachable: %s", e)
            raise SAPConnectionError("The attachment file service is unreachable.")

        if response.status_code < 400:
            return response

        detail = ""
        try:
            detail = (json.loads(response.content.decode("utf-8", "replace")) or {}).get("detail") or ""
        except (ValueError, AttributeError):
            detail = ""
        if re.search(r"latin-1|codec can't encode|not in range\(256\)", detail, re.IGNORECASE):
            chars = describe_unencodable_chars(file_name)
            error = SAPDataError(
                f'"{file_name or "This attachment"}" cannot be served by the file server: its name '
                f"contains {', '.join(chars) if chars else 'a character'}, which the server cannot "
                "put in its download header. The file itself is fine; the file server needs the "
                "RFC 5987 header fix."
            )
            error.status = response.status_code
            raise error
        message = f"File service returned HTTP {response.status_code}" + (f": {detail}" if detail else "")
        error = (SAPValidationError if response.status_code == 404 else SAPDataError)(message)
        error.status = response.status_code
        raise error

    def _unwrap(self, response, file_name: str) -> dict:
        data = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise SAPDataError("The attachment is larger than 100 MB.")
        body = bytes(data)
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "zip" not in content_type and not body.startswith(b"PK"):
            return {
                "data": body,
                "file_name": file_name,
                "content_type": content_type or guess_content_type(file_name),
            }
        try:
            archive = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile as e:
            raise SAPDataError(f"Could not read the file service's ZIP: {e}") from e
        members = [info for info in archive.infolist() if not info.is_dir()]
        if not members:
            raise SAPDataError(f"The file service's ZIP did not contain {file_name}.")
        wanted = os.path.basename(file_name or "").lower()

        def base(info):
            return re.split(r"[\\/]", info.filename)[-1]

        member = (
            next((m for m in members if base(m).lower() == wanted), None)
            or next((m for m in members if m.filename.lower() == wanted), None)
            or members[0]
        )
        if member.file_size > MAX_BYTES:
            raise SAPDataError("The attachment is larger than 100 MB.")
        extracted = base(member) or file_name
        return {
            "data": archive.read(member),
            "file_name": extracted,
            "content_type": guess_content_type(extracted),
        }
