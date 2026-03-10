import logging
import mimetypes
import os
from typing import Optional
import requests

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .auth import ServiceLayerSession

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

    def _get_attachment_source_path(self) -> str:
        """Get the SAP attachment source path from existing attachment entries."""
        cookies = self._get_session_cookies()
        url = (
            f"{self.sl_config['base_url']}/b1s/v2/Attachments2"
            f"?$top=1&$select=Attachments2_Lines"
        )
        try:
            response = requests.get(
                url, cookies=cookies, timeout=30, verify=False
            )
            if response.status_code == 200:
                data = response.json()
                entries = data.get("value", [])
                if entries:
                    lines = entries[0].get("Attachments2_Lines", [])
                    if lines:
                        return lines[0].get("SourcePath", "")
        except Exception as e:
            logger.warning(f"Failed to get SAP attachment source path: {e}")
        return ""

    def upload(self, file_path: str, filename: str) -> dict:
        """
        Upload a file to SAP Attachments2 endpoint.
        First tries multipart file upload; if that fails with -43 (path error),
        falls back to creating a JSON metadata entry using the SAP attachment path.

        Args:
            file_path: Absolute path to the file on disk (from FileField.path)
            filename: Original filename to use in the upload

        Returns:
            dict: SAP response containing AbsoluteEntry
        """
        cookies = self._get_session_cookies()
        url = f"{self.sl_config['base_url']}/b1s/v2/Attachments2"

        try:
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

            # If file upload fails with -43 (internal/path error),
            # fall back to JSON metadata approach
            if response.status_code == 400:
                error_text = response.text
                if '"-43"' in error_text:
                    logger.warning(
                        "Multipart upload failed with -43, "
                        "falling back to JSON metadata entry"
                    )
                    return self._create_attachment_entry(filename, cookies, url)

                error_msg = self._extract_error_message(response)
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

    def _create_attachment_entry(
        self, filename: str, cookies: dict, url: str
    ) -> dict:
        """
        Create an attachment entry in SAP using JSON metadata.
        Used as fallback when multipart file upload fails due to
        SAP server path configuration issues.
        """
        name_without_ext = os.path.splitext(filename)[0]
        file_ext = os.path.splitext(filename)[1].lstrip(".")

        source_path = self._get_attachment_source_path()

        payload = {
            "Attachments2_Lines": [{
                "SourcePath": source_path,
                "FileName": name_without_ext,
                "FileExtension": file_ext,
                "Override": "tYES",
            }]
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

            if response.status_code == 201:
                result = response.json()
                logger.info(
                    f"Attachment entry created in SAP (JSON). "
                    f"AbsoluteEntry: {result.get('AbsoluteEntry')}"
                )
                return result

            error_msg = self._extract_error_message(response)
            logger.error(f"SAP error creating attachment entry: {error_msg}")
            raise SAPDataError(f"Failed to create attachment entry: {error_msg}")

        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating attachment entry: {e}")
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
        self, absolute_entry: int, file_path: str, filename: str
    ) -> dict:
        """
        Add a new file line to an existing Attachments2 entry.
        This avoids PATCHing the GRPO document (which triggers SAP approval).

        Args:
            absolute_entry: The existing Attachments2 AbsoluteEntry
            file_path: Path to the file on disk
            filename: Original filename

        Returns:
            dict: Updated Attachments2 response
        """
        cookies = self._get_session_cookies()

        # First, get existing lines so we can append
        get_url = (
            f"{self.sl_config['base_url']}/b1s/v2"
            f"/Attachments2({absolute_entry})"
        )

        try:
            response = requests.get(
                get_url, cookies=cookies, timeout=30, verify=False
            )
            if response.status_code != 200:
                error_msg = self._extract_error_message(response)
                raise SAPDataError(
                    f"Failed to get existing attachment entry: {error_msg}"
                )

            existing_data = response.json()
            existing_lines = existing_data.get("Attachments2_Lines", [])

        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:
            logger.error(f"Error fetching attachment entry {absolute_entry}: {e}")
            raise SAPDataError(f"Unexpected error: {str(e)}")

        # Build new line
        name_without_ext = os.path.splitext(filename)[0]
        file_ext = os.path.splitext(filename)[1].lstrip(".")
        source_path = ""
        if existing_lines:
            source_path = existing_lines[0].get("SourcePath", "")

        new_line = {
            "SourcePath": source_path,
            "FileName": name_without_ext,
            "FileExtension": file_ext,
            "Override": "tYES",
        }
        existing_lines.append(new_line)

        # PATCH the Attachments2 entry with updated lines
        patch_url = (
            f"{self.sl_config['base_url']}/b1s/v2"
            f"/Attachments2({absolute_entry})"
        )
        payload = {"Attachments2_Lines": existing_lines}
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.patch(
                patch_url,
                json=payload,
                cookies=cookies,
                headers=headers,
                timeout=30,
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
                return error_data["error"].get("message", {}).get("value", str(error_data))
            return str(error_data)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
