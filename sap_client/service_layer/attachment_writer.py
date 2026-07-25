import logging
import mimetypes
import os
import re
import shutil
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Optional
import requests
from django.conf import settings

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from ..hana.connection import HanaConnection
from .auth import ServiceLayerSession
from .file_uploader_client import FileUploaderClient

logger = logging.getLogger(__name__)


class AttachmentWriter:
    """Attachment Writer for SAP Service Layer Attachments2 endpoint"""

    def __init__(self, context):
        self.context = context
        self.sl_config = context.service_layer

    def _get_session_cookies(self):
        """Get authenticated session cookies from Service Layer"""
        try:
            session = ServiceLayerSession(self.sl_config)
            return session.login()
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Failed to connect to SAP Service Layer: {e}")
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error(f"SAP Service Layer connection timeout: {e}")
            raise SAPConnectionError("SAP Service Layer connection timeout")
        except requests.exceptions.HTTPError as e:
            logger.error(f"SAP Service Layer authentication failed: {e}")
            raise SAPConnectionError("SAP Service Layer authentication failed")

    def _get_attachment_source_path(self, cookies=None) -> str:
        """
        Read SAP's configured attachment path.

        Multipart upload is preferred. This path is used only as a fallback for
        SAP systems where Service Layer cannot resolve the attachment-folder
        mount, but the backend can write to the same Windows/shared folder.
        """
        hana_config = getattr(self.context, "hana", None)
        if hana_config:
            connection = HanaConnection(hana_config)
            conn = None
            cursor = None
            try:
                conn = connection.connect()
                cursor = conn.cursor()
                cursor.execute(
                    f'SELECT "AttachPath" FROM "{connection.schema}"."OADP"'
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
            except Exception as exc:
                logger.warning("Failed to read SAP attachment path from OADP: %s", exc)
            finally:
                if cursor:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        if cookies is None:
            cookies = self._get_session_cookies()
        url = (
            f"{self.sl_config['base_url']}/b1s/v2/Attachments2"
            "?$top=1&$select=Attachments2_Lines"
        )
        try:
            response = requests.get(url, cookies=cookies, timeout=30, verify=False)
            if response.status_code == 200:
                data = response.json()
                entries = data.get("value", [])
                if entries:
                    lines = entries[0].get("Attachments2_Lines", [])
                    if lines and lines[0].get("SourcePath"):
                        return str(lines[0]["SourcePath"]).strip()
        except Exception as exc:
            logger.warning("Failed to read SAP attachment path from Attachments2: %s", exc)
        return ""

    @staticmethod
    def _safe_sap_filename(filename: str) -> str:
        """Return an extension-less filename that is safe for SAP Attachments2."""
        stem = Path(filename).stem or "attachment"
        stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" ._")
        if not stem:
            stem = "attachment"
        return f"{stem[:80]}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _is_attachment_folder_error(error_msg: str, response_text: str = "") -> bool:
        value = f"{error_msg} {response_text}".lower()
        return (
            "-43" in value
            or "-5002" in value
            or "attachments folder" in value
            or "linux mount point" in value
            or "attachmentsfolderpath" in value
            or "internal error (-43)" in value
        )

    @staticmethod
    def _hana_fallback_enabled() -> bool:
        """Direct-to-HANA attachment writes on SAP folder errors (opt-in)."""
        return bool(getattr(settings, "SAP_ATTACHMENT_HANA_FALLBACK", False))

    @staticmethod
    def _normalize_attachment_path(path: str) -> str:
        return str(path or "").strip().replace("/", "\\").rstrip("\\").lower()

    @staticmethod
    def _sap_source_path(path: str) -> str:
        return str(path or "").strip().rstrip("\\/")

    def _get_file_uploader_source_path(self, cookies=None) -> str:
        company_code = getattr(self.context, "company_code", "").upper()
        source_paths = getattr(settings, "SAP_FILE_UPLOADER_SOURCE_PATHS", {}) or {}
        configured_path = (source_paths.get(company_code) or "").strip()
        if configured_path:
            return self._sap_source_path(configured_path)
        return self._sap_source_path(self._get_attachment_source_path(cookies=cookies))

    @staticmethod
    def _split_stored_filename(stored_name: str, fallback_filename: str) -> tuple[str, str]:
        stored_name = Path(stored_name or fallback_filename).name
        fallback_filename = Path(fallback_filename or stored_name).name
        file_stem = Path(stored_name).stem or Path(fallback_filename).stem
        file_extension = (
            Path(stored_name).suffix.lstrip(".")
            or Path(fallback_filename).suffix.lstrip(".")
        )
        return file_stem, file_extension

    @staticmethod
    @lru_cache(maxsize=16)
    def _get_attachment_udf_columns(company_code: str) -> frozenset[str]:
        fallback_columns = {
            "JIVO_OIL": frozenset({"U_CHK", "U_CHK2"}),
            "JIVO_BEVERAGES": frozenset({"U_CHK", "U_CHK2"}),
            "JIVO_MART": frozenset(),
        }
        if not isinstance(company_code, str):
            return frozenset()
        company_code = company_code.upper()
        try:
            from ..context import CompanyContext

            context = CompanyContext(company_code)
            connection = HanaConnection(context.hana)
            conn = None
            cursor = None
            try:
                conn = connection.connect()
                cursor = conn.cursor()
                cursor.execute(
                    """
                        SELECT "COLUMN_NAME"
                        FROM "SYS"."TABLE_COLUMNS"
                        WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = 'ATC1'
                          AND "COLUMN_NAME" LIKE 'U_%'
                    """,
                    [connection.schema],
                )
                return frozenset(row[0] for row in cursor.fetchall())
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
        except Exception as exc:
            logger.warning(
                "Could not inspect SAP ATC1 UDF columns for %s: %s",
                company_code,
                exc,
            )
            return fallback_columns.get(company_code, frozenset())

    def _attachment_approval_fields(self) -> dict:
        company_code = getattr(self.context, "company_code", "").upper()
        udf_columns = self._get_attachment_udf_columns(company_code)
        fields = {}
        if "U_CHK" in udf_columns:
            fields["U_CHK"] = "1"
        if "U_CHK2" in udf_columns:
            fields["U_CHK2"] = "OK"
        return fields

    def _metadata_line_for_uploaded_file(
        self,
        uploaded_file: dict,
        filename: str,
        cookies,
        source_path: str = "",
    ) -> dict:
        source_path = self._sap_source_path(
            source_path or self._get_file_uploader_source_path(cookies=cookies)
        )
        if not source_path:
            raise SAPValidationError(
                "SAP file uploader saved the attachment, but SAP AttachPath could "
                "not be resolved for Attachments2 metadata."
            )

        stored_name = uploaded_file.get("stored_name") or filename
        file_stem, file_extension = self._split_stored_filename(stored_name, filename)
        if not file_stem:
            raise SAPDataError("SAP file uploader returned an empty stored filename")

        return {
            "SourcePath": source_path,
            "FileName": file_stem,
            "FileExtension": file_extension,
            "Override": "tYES",
            "CopyToTargetDoc": "tYES",
            **self._attachment_approval_fields(),
        }

    def _attachment_line_matches(self, line: dict, expected_line: dict) -> bool:
        return (
            self._normalize_attachment_path(line.get("SourcePath"))
            == self._normalize_attachment_path(expected_line.get("SourcePath"))
            and str(line.get("FileName") or "").strip().lower()
            == str(expected_line.get("FileName") or "").strip().lower()
            and str(line.get("FileExtension") or "").strip().lower()
            == str(expected_line.get("FileExtension") or "").strip().lower()
        )

    def _get_attachment_entry(self, absolute_entry: int, cookies) -> dict:
        response = requests.get(
            f"{self.sl_config['base_url']}/b1s/v2/Attachments2({absolute_entry})",
            cookies=cookies,
            timeout=30,
            verify=False,
        )
        if response.status_code == 200:
            return response.json()
        error_msg = self._extract_error_message(response)
        raise SAPDataError(
            f"Failed to read back SAP Attachments2({absolute_entry}): {error_msg}"
        )

    def _verify_attachment_line(
        self,
        absolute_entry: int,
        expected_line: dict,
        cookies,
    ) -> None:
        entry = self._get_attachment_entry(absolute_entry, cookies)
        lines = entry.get("Attachments2_Lines", []) or []
        if any(self._attachment_line_matches(line, expected_line) for line in lines):
            return

        expected_file = (
            f"{expected_line.get('FileName')}.{expected_line.get('FileExtension')}"
            if expected_line.get("FileExtension")
            else str(expected_line.get("FileName") or "")
        )
        raise SAPDataError(
            f"SAP Attachments2({absolute_entry}) was created, but no attachment "
            f"line points to uploaded file '{expected_file}' in "
            f"'{expected_line.get('SourcePath')}'."
        )

    @staticmethod
    def _attach_uploader_details(result: dict, uploaded_file: dict, line: dict) -> dict:
        result["UploaderFileId"] = uploaded_file.get("id")
        result["StoredFileName"] = uploaded_file.get("stored_name")
        result["AttachmentSourcePath"] = line.get("SourcePath")
        result["AttachmentFileName"] = line.get("FileName")
        result["AttachmentFileExtension"] = line.get("FileExtension")
        return result

    def _create_attachment_from_file_uploader(
        self,
        file_path: str,
        filename: str,
        cookies,
        url: str,
    ) -> dict:
        uploader = FileUploaderClient(getattr(self.context, "company_code", ""))
        uploaded_file = uploader.upload(file_path=file_path, filename=filename)
        file_id = uploaded_file.get("id")
        line = self._metadata_line_for_uploaded_file(
            uploaded_file=uploaded_file,
            filename=filename,
            cookies=cookies,
        )
        payload = {"Attachments2_Lines": [line]}

        try:
            response = requests.post(
                url,
                json=payload,
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                timeout=30,
                verify=False,
            )
        except Exception:
            uploader.delete(file_id)
            raise

        if response.status_code != 201:
            error_msg = self._extract_error_message(response)
            if self._hana_fallback_enabled() and self._is_attachment_folder_error(
                error_msg, response.text
            ):
                logger.warning(
                    "SL Attachments2 create failed with folder error (%s); writing "
                    "the attachment directly to HANA (file already in SAP folder).",
                    error_msg,
                )
                try:
                    result = self._create_attachment_via_hana(line)
                except Exception as hana_exc:
                    uploader.delete(file_id)
                    raise SAPValidationError(
                        f"SL folder error ({error_msg}) and HANA-direct attachment "
                        f"fallback failed: {hana_exc}"
                    ) from hana_exc
                self._verify_attachment_line(result["AbsoluteEntry"], line, cookies)
                logger.info(
                    "Attachment registered via HANA fallback. AbsoluteEntry: %s, "
                    "file: %s",
                    result.get("AbsoluteEntry"),
                    uploaded_file.get("stored_name"),
                )
                return self._attach_uploader_details(result, uploaded_file, line)
            uploader.delete(file_id)
            raise SAPValidationError(
                "SAP attachment metadata registration failed after uploader "
                f"saved the file: {error_msg}"
            )

        result = response.json()
        absolute_entry = result.get("AbsoluteEntry")
        if not absolute_entry:
            raise SAPDataError("SAP did not return AbsoluteEntry for attachment")

        self._verify_attachment_line(absolute_entry, line, cookies)
        logger.info(
            "Attachment uploaded through SAP file uploader and registered. "
            "AbsoluteEntry: %s, file: %s",
            absolute_entry,
            uploaded_file.get("stored_name"),
        )
        return self._attach_uploader_details(result, uploaded_file, line)

    def _add_line_from_file_uploader(
        self,
        absolute_entry: int,
        file_path: str,
        filename: str,
        cookies,
        patch_url: str,
    ) -> dict:
        existing_data = self._get_attachment_entry(absolute_entry, cookies)
        existing_lines = existing_data.get("Attachments2_Lines", []) or []
        source_path = ""
        if existing_lines:
            source_path = existing_lines[0].get("SourcePath") or ""

        uploader = FileUploaderClient(getattr(self.context, "company_code", ""))
        uploaded_file = uploader.upload(file_path=file_path, filename=filename)
        file_id = uploaded_file.get("id")
        line = self._metadata_line_for_uploaded_file(
            uploaded_file=uploaded_file,
            filename=filename,
            cookies=cookies,
            source_path=source_path,
        )

        try:
            response = requests.patch(
                patch_url,
                json={"Attachments2_Lines": [*existing_lines, line]},
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                timeout=30,
                verify=False,
            )
        except Exception:
            uploader.delete(file_id)
            raise

        if response.status_code not in (200, 204):
            error_msg = self._extract_error_message(response)
            if self._hana_fallback_enabled() and self._is_attachment_folder_error(
                error_msg, response.text
            ):
                logger.warning(
                    "SL Attachments2 add-line failed with folder error (%s); adding "
                    "the line directly to HANA (file already in SAP folder).",
                    error_msg,
                )
                try:
                    self._add_line_via_hana(absolute_entry, line)
                except Exception as hana_exc:
                    uploader.delete(file_id)
                    raise SAPValidationError(
                        f"SL folder error ({error_msg}) and HANA-direct add-line "
                        f"fallback failed: {hana_exc}"
                    ) from hana_exc
                self._verify_attachment_line(absolute_entry, line, cookies)
                logger.info(
                    "Attachment line registered via HANA fallback on "
                    "Attachments2(%s): %s",
                    absolute_entry,
                    uploaded_file.get("stored_name"),
                )
                return self._attach_uploader_details(
                    {"AbsoluteEntry": absolute_entry, "FileName": filename},
                    uploaded_file,
                    line,
                )
            uploader.delete(file_id)
            raise SAPValidationError(
                "SAP attachment metadata line registration failed after uploader "
                f"saved the file: {error_msg}"
            )

        self._verify_attachment_line(absolute_entry, line, cookies)
        logger.info(
            "Attachment line uploaded through SAP file uploader and registered "
            "on Attachments2(%s): %s",
            absolute_entry,
            uploaded_file.get("stored_name"),
        )
        return self._attach_uploader_details(
            {"AbsoluteEntry": absolute_entry, "FileName": filename},
            uploaded_file,
            line,
        )

    def _get_direct_copy_path(self, sap_source_path: str) -> str:
        direct_copy_paths = getattr(settings, "SAP_ATTACHMENT_DIRECT_COPY_PATHS", {})
        company_code = getattr(self.context, "company_code", "").upper()
        configured_path = direct_copy_paths.get(company_code) if company_code else ""
        return (configured_path or sap_source_path or "").strip()

    def _get_direct_copy_credentials(self) -> tuple[str, str]:
        credentials = getattr(settings, "SAP_ATTACHMENT_DIRECT_COPY_CREDENTIALS", {})
        company_code = getattr(self.context, "company_code", "").upper()
        company_credentials = credentials.get(company_code, {}) if company_code else {}
        return (
            (company_credentials.get("username") or "").strip(),
            company_credentials.get("password") or "",
        )

    def _resolve_attachment_paths(self, sap_source_path: str) -> tuple[str, str]:
        sap_path = os.path.normpath(sap_source_path)
        copy_path = os.path.normpath(self._get_direct_copy_path(sap_source_path))
        return sap_path, copy_path

    @staticmethod
    def _unc_share_root(path: str) -> str:
        match = re.match(r"^(\\\\[^\\]+\\[^\\]+)", path)
        return match.group(1) if match else ""

    def _ensure_direct_copy_path_access(self, copy_path: str) -> None:
        if os.path.isdir(copy_path):
            return

        share_root = self._unc_share_root(copy_path)
        if not share_root or os.name != "nt":
            return

        username, password = self._get_direct_copy_credentials()
        if not username or not password:
            return

        command = [
            "net",
            "use",
            share_root,
            password,
            f"/user:{username}",
            "/persistent:no",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0 or os.path.isdir(copy_path):
            return

        message = (result.stderr or result.stdout or "").strip()
        raise SAPValidationError(
            "SAP attachment direct-copy share login failed for "
            f"'{share_root}' as '{username}': {message}"
        )

    def _upload_from_accessible_source_path(
        self,
        file_path: str,
        filename: str,
        cookies,
        url: str,
        original_error: str,
    ) -> dict:
        """
        Fallback for SAP systems whose Service Layer cannot multipart-copy files.

        This is intentionally stricter than the old metadata fallback: it first
        copies the actual file to SAP's configured attachment folder, then creates
        the Attachments2 metadata row. If the folder is not accessible from the
        backend, we fail with a useful error instead of creating an SAP row that
        points at a missing file.
        """
        source_path = self._get_attachment_source_path(cookies=cookies)
        if not source_path:
            raise SAPValidationError(
                "SAP attachment upload failed because Service Layer cannot resolve "
                f"the attachment folder ({original_error}), and no SAP AttachPath "
                "could be read from OADP."
            )

        sap_source_path, copy_path = self._resolve_attachment_paths(source_path)
        self._ensure_direct_copy_path_access(copy_path)
        if not os.path.isdir(copy_path):
            raise SAPValidationError(
                "SAP attachment upload failed because Service Layer cannot copy the "
                f"file ({original_error}). SAP AttachPath is '{source_path}', but "
                f"the backend copy path '{copy_path}' is not accessible from the "
                "backend host. Configure the Service Layer attachment-folder mount, "
                "run the backend where that path exists, or set "
                "SAP_ATTACHMENT_DIRECT_COPY_PATH for this company to a writable "
                "network/local path that maps to the SAP attachment folder."
            )

        extension = Path(filename).suffix.lstrip(".") or Path(file_path).suffix.lstrip(".")
        sap_file_stem = self._safe_sap_filename(filename)
        sap_filename = f"{sap_file_stem}.{extension}" if extension else sap_file_stem
        target_path = os.path.join(copy_path, sap_filename)

        try:
            shutil.copyfile(file_path, target_path)
        except OSError as exc:
            raise SAPValidationError(
                "SAP attachment upload failed because the backend could not copy "
                f"the file into backend copy path '{copy_path}': {exc}"
            ) from exc

        payload = {
            "Attachments2_Lines": [
                {
                    "SourcePath": sap_source_path,
                    "FileName": sap_file_stem,
                    "FileExtension": extension,
                    "Override": "tYES",
                    "CopyToTargetDoc": "tYES",
                    **self._attachment_approval_fields(),
                }
            ]
        }
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(
                url,
                json=payload,
                cookies=cookies,
                headers=headers,
                timeout=30,
                verify=False,
            )
        except Exception:
            try:
                os.unlink(target_path)
            except OSError:
                pass
            raise

        if response.status_code == 201:
            result = response.json()
            logger.info(
                "Attachment copied to SAP AttachPath and registered. "
                "AbsoluteEntry: %s",
                result.get("AbsoluteEntry"),
            )
            return result

        try:
            os.unlink(target_path)
        except OSError:
            pass
        error_msg = self._extract_error_message(response)
        raise SAPValidationError(
            "SAP attachment upload failed after copying the file into "
            f"backend copy path '{copy_path}': {error_msg}"
        )

    def _add_line_from_accessible_source_path(
        self,
        absolute_entry: int,
        file_path: str,
        filename: str,
        cookies,
        patch_url: str,
        original_error: str,
    ) -> dict:
        """
        Add a line to an existing Attachments2 entry after copying the file to
        an attachment folder that the backend can access.
        """
        get_response = requests.get(
            patch_url,
            cookies=cookies,
            timeout=30,
            verify=False,
        )
        if get_response.status_code != 200:
            error_msg = self._extract_error_message(get_response)
            raise SAPDataError(f"Failed to get existing attachment entry: {error_msg}")

        existing_data = get_response.json()
        existing_lines = existing_data.get("Attachments2_Lines", [])
        source_path = ""
        if existing_lines:
            source_path = existing_lines[0].get("SourcePath") or ""
        if not source_path:
            source_path = self._get_attachment_source_path(cookies=cookies)
        if not source_path:
            raise SAPValidationError(
                "SAP attachment upload failed because Service Layer cannot resolve "
                f"the attachment folder ({original_error}), and no SAP AttachPath "
                "could be read from OADP."
            )

        sap_source_path, copy_path = self._resolve_attachment_paths(source_path)
        self._ensure_direct_copy_path_access(copy_path)
        if not os.path.isdir(copy_path):
            raise SAPValidationError(
                "SAP attachment upload failed because Service Layer cannot copy the "
                f"file ({original_error}). SAP AttachPath is '{source_path}', but "
                f"the backend copy path '{copy_path}' is not accessible from the "
                "backend host. Configure the Service Layer attachment-folder mount, "
                "run the backend where that path exists, or set "
                "SAP_ATTACHMENT_DIRECT_COPY_PATH for this company to a writable "
                "network/local path that maps to the SAP attachment folder."
            )

        extension = Path(filename).suffix.lstrip(".") or Path(file_path).suffix.lstrip(".")
        sap_file_stem = self._safe_sap_filename(filename)
        sap_filename = f"{sap_file_stem}.{extension}" if extension else sap_file_stem
        target_path = os.path.join(copy_path, sap_filename)

        try:
            shutil.copyfile(file_path, target_path)
            existing_lines.append(
                {
                    "SourcePath": sap_source_path,
                    "FileName": sap_file_stem,
                    "FileExtension": extension,
                    "Override": "tYES",
                    "CopyToTargetDoc": "tYES",
                    **self._attachment_approval_fields(),
                }
            )
            response = requests.patch(
                patch_url,
                json={"Attachments2_Lines": existing_lines},
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                timeout=30,
                verify=False,
            )
        except Exception:
            try:
                os.unlink(target_path)
            except OSError:
                pass
            raise

        if response.status_code in (200, 204):
            logger.info("Added copied attachment line to Attachments2(%s)", absolute_entry)
            return {"AbsoluteEntry": absolute_entry, "FileName": filename}

        try:
            os.unlink(target_path)
        except OSError:
            pass
        error_msg = self._extract_error_message(response)
        raise SAPValidationError(
            "SAP attachment upload failed after copying the file into "
            f"backend copy path '{copy_path}': {error_msg}"
        )

    def _create_metadata_attachment_entry(
        self,
        filename: str,
        cookies,
        url: str,
    ) -> dict:
        """
        Create only the SAP Attachments2 metadata row.

        This preserves the legacy material-GRPO behavior: SAP gets an attachment
        line/AbsoluteEntry even when the actual file was not copied into SAP's
        attachment folder. The file remains stored in our app.
        """
        source_path = self._get_attachment_source_path(cookies=cookies)
        name_without_ext = os.path.splitext(filename)[0]
        file_ext = os.path.splitext(filename)[1].lstrip(".")
        payload = {
            "Attachments2_Lines": [
                {
                    "SourcePath": source_path,
                    "FileName": name_without_ext,
                    "FileExtension": file_ext,
                    "Override": "tYES",
                    **self._attachment_approval_fields(),
                }
            ]
        }

        response = requests.post(
            url,
            json=payload,
            cookies=cookies,
            headers={"Content-Type": "application/json"},
            timeout=30,
            verify=False,
        )
        if response.status_code == 201:
            result = response.json()
            logger.warning(
                "Created legacy metadata-only SAP attachment entry. "
                "AbsoluteEntry: %s",
                result.get("AbsoluteEntry"),
            )
            return result

        error_msg = self._extract_error_message(response)
        raise SAPDataError(f"Failed to create metadata-only attachment entry: {error_msg}")

    def _add_metadata_line_to_existing_attachment(
        self,
        absolute_entry: int,
        filename: str,
        cookies,
        patch_url: str,
    ) -> dict:
        """Append only a metadata line to an existing SAP Attachments2 entry."""
        get_response = requests.get(
            patch_url,
            cookies=cookies,
            timeout=30,
            verify=False,
        )
        if get_response.status_code != 200:
            error_msg = self._extract_error_message(get_response)
            raise SAPDataError(f"Failed to get existing attachment entry: {error_msg}")

        existing_data = get_response.json()
        existing_lines = existing_data.get("Attachments2_Lines", [])
        source_path = ""
        if existing_lines:
            source_path = existing_lines[0].get("SourcePath") or ""
        if not source_path:
            source_path = self._get_attachment_source_path(cookies=cookies)

        name_without_ext = os.path.splitext(filename)[0]
        file_ext = os.path.splitext(filename)[1].lstrip(".")
        existing_lines.append(
            {
                "SourcePath": source_path,
                "FileName": name_without_ext,
                "FileExtension": file_ext,
                "Override": "tYES",
                **self._attachment_approval_fields(),
            }
        )

        response = requests.patch(
            patch_url,
            json={"Attachments2_Lines": existing_lines},
            cookies=cookies,
            headers={"Content-Type": "application/json"},
            timeout=30,
            verify=False,
        )
        if response.status_code in (200, 204):
            logger.warning(
                "Added legacy metadata-only line '%s' to Attachments2(%s)",
                filename,
                absolute_entry,
            )
            return {"AbsoluteEntry": absolute_entry, "FileName": filename}

        error_msg = self._extract_error_message(response)
        raise SAPDataError(f"Failed to add metadata-only attachment line: {error_msg}")

    # ------------------------------------------------------------------ #
    # Direct-to-HANA fallback (OATC/ATC1) for SAP folder errors (-5002/-43)
    #
    # SAP's Service Layer rejects Attachments2 create/patch when its own
    # Attachments Folder is unreachable, even though the file is already
    # sitting in that folder (placed by the SAP file uploader). In that case
    # we write the same rows SAP would have written, ourselves:
    #   OATC (AbsEntry from the OATC_S sequence) + ATC1 line(s).
    # AbsEntry is minted via OATC_S.NEXTVAL so it stays in lockstep with
    # SAP's own allocation -- no MAX+1 race, no collision.
    # ------------------------------------------------------------------ #
    def _hana_connection(self):
        hana_config = getattr(self.context, "hana", None)
        if not hana_config:
            raise SAPDataError(
                "HANA attachment fallback requested but this company has no "
                "HANA configuration."
            )
        return HanaConnection(hana_config)

    def _resolve_hana_usr_id(self, cursor, schema: str) -> int:
        """A valid OUSR.USERID to stamp on the attachment line (prefer superuser)."""
        override = getattr(settings, "SAP_ATTACHMENT_HANA_USER_ID", None)
        if isinstance(override, dict):
            override = override.get(getattr(self.context, "company_code", "").upper())
        if override:
            return int(override)
        try:
            cursor.execute(
                f'SELECT MIN("USERID") FROM "{schema}"."OUSR" WHERE "SUPERUSER" = \'Y\''
            )
            row = cursor.fetchone()
            if row and row[0] is not None:
                return int(row[0])
        except Exception:
            pass
        cursor.execute(f'SELECT MIN("USERID") FROM "{schema}"."OUSR"')
        return int(cursor.fetchone()[0])

    def _insert_atc1_line(self, cursor, schema: str, absolute_entry: int,
                          line_no: int, line: dict) -> None:
        """Insert one ATC1 row mirroring the app/SL-created line format."""
        src = self._sap_source_path(line.get("SourcePath"))
        columns = [
            "AbsEntry", "Line", "srcPath", "trgtPath", "FileName", "FileExt",
            "Date", "UsrID", "Copied", "Override", "CopyToTrgt", "CopyToProd",
            "EDocSign", "SendInMail", "CopyFile", "FileSuffix",
        ]
        # "Date" is filled by CURRENT_TIMESTAMP (no bind value).
        placeholders = [
            "?", "?", "?", "?", "?", "?",
            "CURRENT_TIMESTAMP", "?", "?", "?", "?", "?",
            "?", "?", "?", "?",
        ]
        values = [
            absolute_entry, line_no, src, src,
            line.get("FileName"), line.get("FileExtension"),
            self._resolve_hana_usr_id(cursor, schema),
            "Y", "N", "Y", "N", "N", "N", "N", "N",
        ]
        approval = self._attachment_approval_fields()
        if "U_CHK" in approval:
            columns.append("U_CHK"); placeholders.append("?")
            values.append(int(approval["U_CHK"]))
        if "U_CHK2" in approval:
            columns.append("U_CHK2"); placeholders.append("?")
            values.append(approval["U_CHK2"])

        col_sql = ", ".join(f'"{c}"' for c in columns)
        ph_sql = ", ".join(placeholders)
        cursor.execute(
            f'INSERT INTO "{schema}"."ATC1" ({col_sql}) VALUES ({ph_sql})', values
        )

    def _create_attachment_via_hana(self, line: dict) -> dict:
        """Create OATC header + first ATC1 line directly on HANA. Returns AbsoluteEntry."""
        connection = self._hana_connection()
        conn = connection.connect()
        cursor = conn.cursor()
        schema = connection.schema
        try:
            conn.setautocommit(False)
            cursor.execute(f'SELECT MAX("AbsEntry") FROM "{schema}"."OATC"')
            current_max = cursor.fetchone()[0] or 0
            cursor.execute(f'SELECT "{schema}"."OATC_S".NEXTVAL FROM DUMMY')
            absolute_entry = int(cursor.fetchone()[0])
            if absolute_entry <= current_max:
                raise SAPDataError(
                    f"OATC_S.NEXTVAL returned {absolute_entry} which is not above "
                    f"the current max {current_max}; aborting to avoid a collision."
                )
            cursor.execute(
                f'INSERT INTO "{schema}"."OATC" ("AbsEntry") VALUES (?)', [absolute_entry]
            )
            self._insert_atc1_line(cursor, schema, absolute_entry, 1, line)
            conn.commit()
            logger.warning(
                "HANA-direct attachment created (SL folder error fallback). "
                "AbsoluteEntry: %s, file: %s.%s",
                absolute_entry, line.get("FileName"), line.get("FileExtension"),
            )
            return {"AbsoluteEntry": absolute_entry}
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _add_line_via_hana(self, absolute_entry: int, line: dict) -> dict:
        """Append an ATC1 line to an existing OATC entry directly on HANA."""
        connection = self._hana_connection()
        conn = connection.connect()
        cursor = conn.cursor()
        schema = connection.schema
        try:
            conn.setautocommit(False)
            cursor.execute(
                f'SELECT COUNT(*) FROM "{schema}"."OATC" WHERE "AbsEntry" = ?',
                [absolute_entry],
            )
            if not cursor.fetchone()[0]:
                raise SAPDataError(
                    f"OATC({absolute_entry}) does not exist; cannot add HANA line."
                )
            cursor.execute(
                f'SELECT MAX("Line") FROM "{schema}"."ATC1" WHERE "AbsEntry" = ?',
                [absolute_entry],
            )
            max_line = cursor.fetchone()[0]
            next_line = int(max_line) + 1 if max_line is not None else 1
            self._insert_atc1_line(cursor, schema, absolute_entry, next_line, line)
            conn.commit()
            logger.warning(
                "HANA-direct attachment line added (SL folder error fallback) on "
                "Attachments2(%s): %s.%s",
                absolute_entry, line.get("FileName"), line.get("FileExtension"),
            )
            return {"AbsoluteEntry": absolute_entry, "FileName": line.get("FileName")}
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def upload(
        self,
        file_path: str,
        filename: str,
        *,
        allow_metadata_fallback: bool = False,
    ) -> dict:
        """
        Upload a file to SAP Attachments2 endpoint.
        Uses multipart upload so SAP receives and copies the actual file bytes.

        Args:
            file_path: Absolute path to the file on disk (from FileField.path)
            filename: Original filename to use in the upload

        Returns:
            dict: SAP response containing AbsoluteEntry
        """
        cookies = self._get_session_cookies()
        url = f"{self.sl_config['base_url']}/b1s/v2/Attachments2"

        try:
            if FileUploaderClient.is_enabled():
                return self._create_attachment_from_file_uploader(
                    file_path=file_path,
                    filename=filename,
                    cookies=cookies,
                    url=url,
                )

            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            with open(file_path, "rb") as f:
                files = {
                    "files": (filename, f, content_type)
                }
                response = requests.post(
                    url,
                    files=files,
                    cookies=cookies,
                    timeout=60,
                    verify=False
                )

            if response.status_code == 201:
                result = response.json()
                logger.info(
                    f"Attachment uploaded to SAP. "
                    f"AbsoluteEntry: {result.get('AbsoluteEntry')}"
                )
                return result

            if response.status_code == 400:
                error_msg = self._extract_error_message(response)
                if self._is_attachment_folder_error(error_msg, response.text):
                    logger.warning(
                        "Multipart attachment upload failed with SAP folder error; "
                        "trying accessible source-path fallback: %s",
                        error_msg,
                    )
                    try:
                        return self._upload_from_accessible_source_path(
                            file_path=file_path,
                            filename=filename,
                            cookies=cookies,
                            url=url,
                            original_error=error_msg,
                        )
                    except SAPValidationError:
                        if allow_metadata_fallback:
                            logger.warning(
                                "Direct-copy attachment fallback failed; creating "
                                "legacy metadata-only SAP attachment entry for "
                                "material GRPO."
                            )
                            return self._create_metadata_attachment_entry(
                                filename=filename,
                                cookies=cookies,
                                url=url,
                            )
                        raise
                logger.error(f"SAP validation error uploading attachment: {error_msg}")
                raise SAPValidationError(error_msg)

            if response.status_code in (401, 403):
                logger.error("SAP authentication/authorization error during attachment upload")
                raise SAPConnectionError("SAP authentication failed")

            error_msg = self._extract_error_message(response)
            logger.error(f"SAP error uploading attachment: {error_msg}")
            raise SAPDataError(f"Failed to upload attachment: {error_msg}")

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error uploading attachment: {e}")
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout uploading attachment: {e}")
            raise SAPConnectionError("SAP Service Layer request timeout")
        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:
            logger.error(f"Unexpected error uploading attachment: {e}")
            raise SAPDataError(f"Unexpected error: {str(e)}")

    def get_document_attachment_entry(self, doc_entry: int) -> Optional[int]:
        """
        Get the existing AttachmentEntry from a GRPO document.

        Returns:
            The AttachmentEntry (AbsoluteEntry) if one exists, else None.
        """
        cookies = self._get_session_cookies()
        url = (
            f"{self.sl_config['base_url']}/b1s/v2"
            f"/PurchaseDeliveryNotes({doc_entry})"
            f"?$select=AttachmentEntry"
        )

        try:
            response = requests.get(
                url, cookies=cookies, timeout=30, verify=False
            )
            if response.status_code == 200:
                data = response.json()
                entry = data.get("AttachmentEntry")
                if entry and entry > 0:
                    return entry
        except Exception as e:
            logger.warning(
                f"Failed to get AttachmentEntry for DocEntry {doc_entry}: {e}"
            )
        return None

    def add_line_to_existing_attachment(
        self,
        absolute_entry: int,
        file_path: str,
        filename: str,
        *,
        allow_metadata_fallback: bool = False,
    ) -> dict:
        """
        Add a new file line to an existing Attachments2 entry using multipart
        upload so SAP receives and copies the actual file bytes.

        Args:
            absolute_entry: The existing Attachments2 AbsoluteEntry
            file_path: Path to the file on disk
            filename: Original filename

        Returns:
            dict: Updated Attachments2 response
        """
        cookies = self._get_session_cookies()

        patch_url = (
            f"{self.sl_config['base_url']}/b1s/v2"
            f"/Attachments2({absolute_entry})"
        )

        try:
            if FileUploaderClient.is_enabled():
                return self._add_line_from_file_uploader(
                    absolute_entry=absolute_entry,
                    file_path=file_path,
                    filename=filename,
                    cookies=cookies,
                    patch_url=patch_url,
                )

            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            with open(file_path, "rb") as f:
                files = {"files": (filename, f, content_type)}
                response = requests.patch(
                    patch_url,
                    files=files,
                    cookies=cookies,
                    timeout=60,
                    verify=False,
                )

            if response.status_code in (200, 204):
                logger.info(
                    f"Added line '{filename}' to Attachments2({absolute_entry})"
                )
                return {
                    "AbsoluteEntry": absolute_entry,
                    "FileName": filename,
                }

            if response.status_code == 400:
                error_msg = self._extract_error_message(response)
                if self._is_attachment_folder_error(error_msg, response.text):
                    logger.warning(
                        "Multipart attachment line upload failed with SAP folder "
                        "error; trying accessible source-path fallback: %s",
                        error_msg,
                    )
                    try:
                        return self._add_line_from_accessible_source_path(
                            absolute_entry=absolute_entry,
                            file_path=file_path,
                            filename=filename,
                            cookies=cookies,
                            patch_url=patch_url,
                            original_error=error_msg,
                        )
                    except SAPValidationError:
                        if allow_metadata_fallback:
                            logger.warning(
                                "Direct-copy attachment line fallback failed; "
                                "adding legacy metadata-only line for material GRPO."
                            )
                            return self._add_metadata_line_to_existing_attachment(
                                absolute_entry=absolute_entry,
                                filename=filename,
                                cookies=cookies,
                                patch_url=patch_url,
                            )
                        raise
                logger.error(f"SAP validation error adding attachment line: {error_msg}")
                raise SAPValidationError(error_msg)

            error_msg = self._extract_error_message(response)
            raise SAPDataError(f"Failed to add attachment line: {error_msg}")

        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:
            logger.error(f"Unexpected error adding attachment line: {e}")
            raise SAPDataError(f"Unexpected error: {str(e)}")

    def link_to_document(self, doc_entry: int, absolute_entry: int) -> dict:
        """
        Link an uploaded attachment to a GRPO document (PurchaseDeliveryNotes)
        by PATCHing the AttachmentEntry field.

        Args:
            doc_entry: The GRPO's DocEntry in SAP
            absolute_entry: The AbsoluteEntry from Attachments2 upload

        Returns:
            dict: Updated document response from SAP
        """
        cookies = self._get_session_cookies()
        url = (
            f"{self.sl_config['base_url']}/b1s/v2"
            f"/PurchaseDeliveryNotes({doc_entry})"
        )

        payload = {
            "AttachmentEntry": absolute_entry
        }

        headers = {
            "Content-Type": "application/json",
        }

        try:
            response = requests.patch(
                url,
                json=payload,
                cookies=cookies,
                headers=headers,
                timeout=30,
                verify=False
            )

            if response.status_code in (200, 204):
                logger.info(
                    f"Attachment {absolute_entry} linked to GRPO DocEntry {doc_entry}"
                )
                if response.status_code == 204:
                    return {"DocEntry": doc_entry, "AttachmentEntry": absolute_entry}
                return response.json()

            if response.status_code == 400:
                error_msg = self._extract_error_message(response)
                logger.error(f"SAP validation error linking attachment: {error_msg}")
                raise SAPValidationError(error_msg)

            if response.status_code in (401, 403):
                logger.error("SAP auth error linking attachment")
                raise SAPConnectionError("SAP authentication failed")

            error_msg = self._extract_error_message(response)
            logger.error(f"SAP error linking attachment: {error_msg}")
            raise SAPDataError(f"Failed to link attachment: {error_msg}")

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error linking attachment: {e}")
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout linking attachment: {e}")
            raise SAPConnectionError("SAP Service Layer request timeout")
        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:
            logger.error(f"Unexpected error linking attachment: {e}")
            raise SAPDataError(f"Unexpected error: {str(e)}")

    def _extract_error_message(self, response) -> str:
        """Extract error message from SAP response"""
        try:
            error_data = response.json()
            if "error" in error_data:
                message = error_data["error"].get("message")
                if isinstance(message, dict):
                    return message.get("value", str(error_data))
                if message:
                    return str(message)
                return str(error_data)
            return str(error_data)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
