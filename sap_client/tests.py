import os
import tempfile
from unittest.mock import patch, MagicMock, call
from datetime import date, datetime, timezone

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient
from rest_framework import status

from company.models import Company, UserCompany, UserRole
from .dtos import PODTO, POItemDTO
from .serializers import GRPORequestSerializer, GRPOLineRequestSerializer, POSerializer
from .service_layer.attachment_writer import AttachmentWriter
from .service_layer.file_uploader_client import FileUploaderClient
from .service_layer.grpo_writer import GRPOWriter
from .exceptions import SAPConnectionError, SAPValidationError, SAPDataError

User = get_user_model()


class GRPOSerializerTests(TestCase):
    """Tests for GRPO serializers"""

    def test_grpo_line_serializer_valid(self):
        """Test valid GRPO line data"""
        data = {
            "ItemCode": "ITEM001",
            "Quantity": "100.00",
            "TaxCode": "T1",
            "UnitPrice": "50.00"
        }
        serializer = GRPOLineRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid())

    def test_grpo_line_serializer_missing_required(self):
        """Test GRPO line with missing required fields"""
        data = {"TaxCode": "T1"}
        serializer = GRPOLineRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("ItemCode", serializer.errors)
        self.assertIn("Quantity", serializer.errors)

    def test_grpo_request_serializer_valid(self):
        """Test valid GRPO request data"""
        data = {
            "CardCode": "V001",
            "DocumentLines": [
                {
                    "ItemCode": "ITEM001",
                    "Quantity": "100",
                    "TaxCode": "T1",
                    "UnitPrice": "50"
                }
            ]
        }
        serializer = GRPORequestSerializer(data=data)
        self.assertTrue(serializer.is_valid())

    def test_grpo_request_serializer_missing_card_code(self):
        """Test GRPO request without CardCode"""
        data = {
            "DocumentLines": [
                {"ItemCode": "ITEM001", "Quantity": "100"}
            ]
        }
        serializer = GRPORequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("CardCode", serializer.errors)

    def test_grpo_request_serializer_empty_lines(self):
        """Test GRPO request with empty DocumentLines"""
        data = {
            "CardCode": "V001",
            "DocumentLines": []
        }
        serializer = GRPORequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("DocumentLines", serializer.errors)

    def test_grpo_request_serializer_multiple_lines(self):
        """Test GRPO request with multiple lines"""
        data = {
            "CardCode": "V001",
            "DocumentLines": [
                {"ItemCode": "ITEM001", "Quantity": "100", "UnitPrice": "50"},
                {"ItemCode": "ITEM002", "Quantity": "50", "UnitPrice": "75"}
            ]
        }
        serializer = GRPORequestSerializer(data=data)
        self.assertTrue(serializer.is_valid())
        self.assertEqual(len(serializer.validated_data["DocumentLines"]), 2)


class POSerializerTests(TestCase):
    """Tests for PO serializers"""

    def test_po_serializer_includes_lookup_summary_fields(self):
        po = PODTO(
            po_number="4500001234",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            doc_entry=1001,
            branch_id=1,
            vendor_ref="INV-001",
            doc_date=date(2026, 5, 14),
            items=[
                POItemDTO(
                    po_item_code="ITEM001",
                    item_name="Groundnut Oil",
                    ordered_qty=100,
                    received_qty=25,
                    remaining_qty=75,
                    uom="KG",
                    rate=10,
                    line_num=0,
                )
            ],
        )

        data = POSerializer(po).data

        self.assertEqual(data["po_number"], "4500001234")
        self.assertEqual(data["supplier_code"], "SUP001")
        self.assertEqual(data["doc_entry"], 1001)
        self.assertEqual(data["vendor_ref"], "INV-001")
        self.assertEqual(data["doc_date"], "2026-05-14")
        self.assertEqual(len(data["items"]), 1)


class GRPOWriterTests(TestCase):
    """Tests for GRPOWriter class"""

    def setUp(self):
        self.mock_context = MagicMock()
        self.mock_context.service_layer = {
            "base_url": "https://test-server:50000",
            "company_db": "TEST_DB",
            "username": "test_user",
            "password": "test_pass"
        }
        self.writer = GRPOWriter(self.mock_context)

    @patch("sap_client.service_layer.grpo_writer.ServiceLayerSession")
    @patch("sap_client.service_layer.grpo_writer.requests.post")
    def test_create_grpo_success(self, mock_post, mock_session_class):
        """Test successful GRPO creation"""
        # Mock session login
        mock_session = MagicMock()
        mock_session.login.return_value = {"session_cookie": "abc123"}
        mock_session_class.return_value = mock_session

        # Mock SAP response
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "DocEntry": 123,
            "DocNum": 456,
            "CardCode": "V001",
            "CardName": "Test Vendor"
        }
        mock_post.return_value = mock_response

        payload = {
            "CardCode": "V001",
            "DocumentLines": [
                {"ItemCode": "ITEM001", "Quantity": 100}
            ]
        }

        result = self.writer.create(payload)

        self.assertEqual(result["DocEntry"], 123)
        self.assertEqual(result["DocNum"], 456)
        mock_post.assert_called_once()

    @patch("sap_client.service_layer.grpo_writer.ServiceLayerSession")
    @patch("sap_client.service_layer.grpo_writer.requests.post")
    def test_create_grpo_validation_error(self, mock_post, mock_session_class):
        """Test GRPO creation with SAP validation error"""
        mock_session = MagicMock()
        mock_session.login.return_value = {}
        mock_session_class.return_value = mock_session

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {
                "message": {"value": "Item 'INVALID' does not exist"}
            }
        }
        mock_post.return_value = mock_response

        payload = {"CardCode": "V001", "DocumentLines": [{"ItemCode": "INVALID", "Quantity": 1}]}

        with self.assertRaises(SAPValidationError) as context:
            self.writer.create(payload)

        self.assertIn("does not exist", str(context.exception))

    @patch("sap_client.service_layer.grpo_writer.ServiceLayerSession")
    def test_create_grpo_connection_error(self, mock_session_class):
        """Test GRPO creation with connection error"""
        import requests
        mock_session = MagicMock()
        mock_session.login.side_effect = requests.exceptions.ConnectionError("Connection refused")
        mock_session_class.return_value = mock_session

        payload = {"CardCode": "V001", "DocumentLines": [{"ItemCode": "ITEM001", "Quantity": 1}]}

        with self.assertRaises(SAPConnectionError):
            self.writer.create(payload)


class AttachmentWriterTests(TestCase):
    """Tests for SAP Attachments2 writer."""

    def setUp(self):
        self.mock_context = MagicMock()
        self.mock_context.service_layer = {
            "base_url": "https://test-server:50000",
            "company_db": "TEST_DB",
            "username": "test_user",
            "password": "test_pass",
        }
        self.mock_context.company_code = "JIVO_OIL"
        self.writer = AttachmentWriter(self.mock_context)

    def _temp_file(self, suffix=".pdf"):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(b"test-file")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        return tmp.name

    @patch("sap_client.service_layer.attachment_writer.ServiceLayerSession")
    @patch("sap_client.service_layer.attachment_writer.requests.post")
    def test_upload_raises_when_sap_attachment_folder_is_not_accessible(
        self, mock_post, mock_session_class
    ):
        mock_session = MagicMock()
        mock_session.login.return_value = {"B1SESSION": "abc123"}
        mock_session_class.return_value = mock_session

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {
                "code": "-43",
                "message": "Fail to get the LINUX mount point for AttachmentsFolderPath",
            }
        }
        mock_response.text = '{"error":{"code":"-43"}}'
        mock_post.return_value = mock_response

        with patch.object(
            self.writer,
            "_get_attachment_source_path",
            return_value=r"C:\missing\sap\attachments",
        ):
            with self.assertRaises(SAPValidationError) as context:
                self.writer.upload(self._temp_file(".jpeg"), "proof.jpeg")

        self.assertIn("not accessible from the backend host", str(context.exception))
        mock_post.assert_called_once()

    def test_upload_uses_file_uploader_and_validates_sap_metadata(self):
        self.mock_context.company_code = "JIVO_OIL"
        with (
            patch("sap_client.service_layer.attachment_writer.ServiceLayerSession") as mock_session_class,
            patch("sap_client.service_layer.attachment_writer.FileUploaderClient") as mock_uploader_class,
            patch("sap_client.service_layer.attachment_writer.requests.post") as mock_post,
            patch("sap_client.service_layer.attachment_writer.requests.get") as mock_get,
            patch.object(
                self.writer,
                "_get_attachment_source_path",
                return_value=r"C:\SAP Attachments\Jivo Oil\Attachments\\",
            ),
        ):
            mock_session = MagicMock()
            mock_session.login.return_value = {"B1SESSION": "abc123"}
            mock_session_class.return_value = mock_session

            mock_uploader_class.is_enabled.return_value = True
            mock_uploader = mock_uploader_class.return_value
            mock_uploader.upload.return_value = {
                "id": 321,
                "original_name": "proof.jpeg",
                "stored_name": "proof_v2.jpeg",
            }

            post_response = MagicMock()
            post_response.status_code = 201
            post_response.json.return_value = {"AbsoluteEntry": 987}
            mock_post.return_value = post_response

            get_response = MagicMock()
            get_response.status_code = 200
            get_response.json.return_value = {
                "Attachments2_Lines": [
                    {
                        "SourcePath": r"C:\SAP Attachments\Jivo Oil\Attachments",
                        "FileName": "proof_v2",
                        "FileExtension": "jpeg",
                    }
                ]
            }
            mock_get.return_value = get_response

            result = self.writer.upload(self._temp_file(".jpeg"), "proof.jpeg")

        self.assertEqual(result["AbsoluteEntry"], 987)
        self.assertEqual(result["UploaderFileId"], 321)
        self.assertEqual(result["StoredFileName"], "proof_v2.jpeg")
        payload = mock_post.call_args.kwargs["json"]
        line = payload["Attachments2_Lines"][0]
        self.assertEqual(line["SourcePath"], r"C:\SAP Attachments\Jivo Oil\Attachments")
        self.assertEqual(line["FileName"], "proof_v2")
        self.assertEqual(line["FileExtension"], "jpeg")
        self.assertEqual(line["CopyToTargetDoc"], "tYES")
        self.assertEqual(line["U_CHK"], "1")
        self.assertEqual(line["U_CHK2"], "OK")
        mock_get.assert_called_once()

    @patch("sap_client.service_layer.attachment_writer.ServiceLayerSession")
    @patch("sap_client.service_layer.attachment_writer.requests.post")
    def test_upload_can_create_metadata_entry_when_direct_copy_is_not_accessible(
        self, mock_post, mock_session_class
    ):
        mock_session = MagicMock()
        mock_session.login.return_value = {"B1SESSION": "abc123"}
        mock_session_class.return_value = mock_session

        failed_response = MagicMock()
        failed_response.status_code = 400
        failed_response.text = '{"error":{"code":"-43"}}'
        failed_response.json.return_value = {
            "error": {"code": "-43", "message": "Internal error (-43) occurred"}
        }

        metadata_response = MagicMock()
        metadata_response.status_code = 201
        metadata_response.json.return_value = {"AbsoluteEntry": 456}
        mock_post.side_effect = [failed_response, metadata_response]

        with patch.object(
            self.writer,
            "_get_attachment_source_path",
            return_value=r"C:\missing\sap\attachments",
        ):
            result = self.writer.upload(
                self._temp_file(".jpeg"),
                "proof.jpeg",
                allow_metadata_fallback=True,
            )

        self.assertEqual(result["AbsoluteEntry"], 456)
        self.assertEqual(mock_post.call_count, 2)
        metadata_payload = mock_post.call_args.kwargs["json"]
        line = metadata_payload["Attachments2_Lines"][0]
        self.assertEqual(line["SourcePath"], r"C:\missing\sap\attachments")
        self.assertEqual(line["FileName"], "proof")
        self.assertEqual(line["FileExtension"], "jpeg")
        self.assertEqual(line["U_CHK2"], "OK")
        self.assertEqual(line["U_CHK"], "1")

    @patch("sap_client.service_layer.attachment_writer.ServiceLayerSession")
    @patch("sap_client.service_layer.attachment_writer.requests.post")
    def test_upload_falls_back_to_accessible_source_path_on_sap_folder_error(
        self, mock_post, mock_session_class
    ):
        mock_session = MagicMock()
        mock_session.login.return_value = {"B1SESSION": "abc123"}
        mock_session_class.return_value = mock_session

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)

        failed_response = MagicMock()
        failed_response.status_code = 400
        failed_response.text = '{"error":{"code":"-43"}}'
        failed_response.json.return_value = {
            "error": {"code": "-43", "message": "Internal error (-43) occurred"}
        }

        success_response = MagicMock()
        success_response.status_code = 201
        success_response.json.return_value = {"AbsoluteEntry": 789}
        mock_post.side_effect = [failed_response, success_response]

        with patch.object(
            self.writer,
            "_get_attachment_source_path",
            return_value=folder.name,
        ):
            result = self.writer.upload(self._temp_file(".jpeg"), "proof.jpeg")

        self.assertEqual(result["AbsoluteEntry"], 789)
        self.assertEqual(mock_post.call_count, 2)
        fallback_payload = mock_post.call_args.kwargs["json"]
        line = fallback_payload["Attachments2_Lines"][0]
        self.assertEqual(line["SourcePath"], os.path.normpath(folder.name))
        self.assertEqual(line["FileExtension"], "jpeg")
        self.assertTrue(os.listdir(folder.name))

    @patch("sap_client.service_layer.attachment_writer.subprocess.run")
    @patch("sap_client.service_layer.attachment_writer.os.path.isdir")
    def test_direct_copy_path_uses_windows_share_credentials(
        self, mock_isdir, mock_run
    ):
        self.mock_context.company_code = "JIVO_OIL"
        mock_isdir.side_effect = [False, True]
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with self.settings(
            SAP_ATTACHMENT_DIRECT_COPY_CREDENTIALS={
                "JIVO_OIL": {"username": r"SERVER\user", "password": "secret"}
            }
        ):
            self.writer._ensure_direct_copy_path_access(
                r"\\103.89.45.247\SAPAttachments\Jivo Oil\Attachments"
            )

        mock_run.assert_called_once()
        args = mock_run.call_args.args[0]
        self.assertEqual(args[:5], [
            "net",
            "use",
            r"\\103.89.45.247\SAPAttachments",
            "secret",
            r"/user:SERVER\user",
        ])
        mock_isdir.assert_has_calls([
            call(r"\\103.89.45.247\SAPAttachments\Jivo Oil\Attachments"),
            call(r"\\103.89.45.247\SAPAttachments\Jivo Oil\Attachments"),
        ])

    @patch("sap_client.service_layer.attachment_writer.ServiceLayerSession")
    @patch("sap_client.service_layer.attachment_writer.requests.patch")
    def test_add_line_uses_multipart_upload(self, mock_patch, mock_session_class):
        mock_session = MagicMock()
        mock_session.login.return_value = {"B1SESSION": "abc123"}
        mock_session_class.return_value = mock_session

        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_patch.return_value = mock_response

        result = self.writer.add_line_to_existing_attachment(
            absolute_entry=123,
            file_path=self._temp_file(".pdf"),
            filename="proof.pdf",
        )

        self.assertEqual(result["AbsoluteEntry"], 123)
        kwargs = mock_patch.call_args.kwargs
        self.assertIn("files", kwargs)
        self.assertNotIn("json", kwargs)

    def test_add_line_uses_file_uploader_and_validates_sap_metadata(self):
        self.mock_context.company_code = "JIVO_OIL"
        with (
            patch("sap_client.service_layer.attachment_writer.ServiceLayerSession") as mock_session_class,
            patch("sap_client.service_layer.attachment_writer.FileUploaderClient") as mock_uploader_class,
            patch("sap_client.service_layer.attachment_writer.requests.get") as mock_get,
            patch("sap_client.service_layer.attachment_writer.requests.patch") as mock_patch,
        ):
            mock_session = MagicMock()
            mock_session.login.return_value = {"B1SESSION": "abc123"}
            mock_session_class.return_value = mock_session

            mock_uploader_class.is_enabled.return_value = True
            mock_uploader = mock_uploader_class.return_value
            mock_uploader.upload.return_value = {
                "id": 654,
                "original_name": "extra.pdf",
                "stored_name": "extra_v2.pdf",
            }

            existing_response = MagicMock()
            existing_response.status_code = 200
            existing_response.json.return_value = {
                "Attachments2_Lines": [
                    {
                        "SourcePath": r"C:\SAP Attachments\Jivo Oil\Attachments",
                        "FileName": "existing",
                        "FileExtension": "pdf",
                    }
                ]
            }
            verify_response = MagicMock()
            verify_response.status_code = 200
            verify_response.json.return_value = {
                "Attachments2_Lines": [
                    {
                        "SourcePath": r"C:\SAP Attachments\Jivo Oil\Attachments",
                        "FileName": "existing",
                        "FileExtension": "pdf",
                    },
                    {
                        "SourcePath": r"C:\SAP Attachments\Jivo Oil\Attachments",
                        "FileName": "extra_v2",
                        "FileExtension": "pdf",
                    },
                ]
            }
            mock_get.side_effect = [existing_response, verify_response]

            patch_response = MagicMock()
            patch_response.status_code = 204
            mock_patch.return_value = patch_response

            result = self.writer.add_line_to_existing_attachment(
                absolute_entry=123,
                file_path=self._temp_file(".pdf"),
                filename="extra.pdf",
            )

        self.assertEqual(result["AbsoluteEntry"], 123)
        self.assertEqual(result["UploaderFileId"], 654)
        payload = mock_patch.call_args.kwargs["json"]
        self.assertEqual(len(payload["Attachments2_Lines"]), 2)
        self.assertEqual(payload["Attachments2_Lines"][1]["FileName"], "extra_v2")
        self.assertEqual(payload["Attachments2_Lines"][1]["U_CHK"], "1")
        self.assertEqual(payload["Attachments2_Lines"][1]["U_CHK2"], "OK")
        self.assertEqual(mock_get.call_count, 2)

    @patch("sap_client.service_layer.attachment_writer.ServiceLayerSession")
    @patch("sap_client.service_layer.attachment_writer.requests.get")
    @patch("sap_client.service_layer.attachment_writer.requests.patch")
    def test_add_line_falls_back_to_accessible_source_path_on_sap_folder_error(
        self, mock_patch, mock_get, mock_session_class
    ):
        mock_session = MagicMock()
        mock_session.login.return_value = {"B1SESSION": "abc123"}
        mock_session_class.return_value = mock_session

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)

        failed_response = MagicMock()
        failed_response.status_code = 400
        failed_response.text = '{"error":{"code":"-43"}}'
        failed_response.json.return_value = {
            "error": {"code": "-43", "message": "Internal error (-43) occurred"}
        }
        success_response = MagicMock()
        success_response.status_code = 204
        mock_patch.side_effect = [failed_response, success_response]

        get_response = MagicMock()
        get_response.status_code = 200
        get_response.json.return_value = {
            "Attachments2_Lines": [
                {
                    "SourcePath": folder.name,
                    "FileName": "existing",
                    "FileExtension": "pdf",
                }
            ]
        }
        mock_get.return_value = get_response

        result = self.writer.add_line_to_existing_attachment(
            absolute_entry=123,
            file_path=self._temp_file(".pdf"),
            filename="proof.pdf",
        )

        self.assertEqual(result["AbsoluteEntry"], 123)
        self.assertEqual(mock_patch.call_count, 2)
        fallback_payload = mock_patch.call_args.kwargs["json"]
        self.assertEqual(len(fallback_payload["Attachments2_Lines"]), 2)
        self.assertTrue(os.listdir(folder.name))

    @patch("sap_client.service_layer.attachment_writer.ServiceLayerSession")
    @patch("sap_client.service_layer.attachment_writer.requests.get")
    @patch("sap_client.service_layer.attachment_writer.requests.patch")
    def test_add_line_can_create_metadata_line_when_direct_copy_is_not_accessible(
        self, mock_patch, mock_get, mock_session_class
    ):
        mock_session = MagicMock()
        mock_session.login.return_value = {"B1SESSION": "abc123"}
        mock_session_class.return_value = mock_session

        failed_response = MagicMock()
        failed_response.status_code = 400
        failed_response.text = '{"error":{"code":"-43"}}'
        failed_response.json.return_value = {
            "error": {"code": "-43", "message": "Internal error (-43) occurred"}
        }
        success_response = MagicMock()
        success_response.status_code = 204
        mock_patch.side_effect = [failed_response, success_response]

        get_response = MagicMock()
        get_response.status_code = 200
        get_response.json.return_value = {
            "Attachments2_Lines": [
                {
                    "SourcePath": r"C:\missing\sap\attachments",
                    "FileName": "existing",
                    "FileExtension": "pdf",
                }
            ]
        }
        mock_get.return_value = get_response

        result = self.writer.add_line_to_existing_attachment(
            absolute_entry=123,
            file_path=self._temp_file(".pdf"),
            filename="proof.pdf",
            allow_metadata_fallback=True,
        )

        self.assertEqual(result["AbsoluteEntry"], 123)
        self.assertEqual(mock_patch.call_count, 2)
        metadata_payload = mock_patch.call_args.kwargs["json"]
        self.assertEqual(len(metadata_payload["Attachments2_Lines"]), 2)
        self.assertEqual(metadata_payload["Attachments2_Lines"][1]["FileName"], "proof")

    # ----------------------- HANA-direct fallback ------------------------ #
    ATTACH_FOLDER = r"\\20.20.45.25\Attachments_Oil\JIVO_OIL\Attachments"

    def _fake_hana(self, fetch_values):
        """Return (patched HanaConnection instance, conn, cursor) for the writer."""
        cursor = MagicMock()
        cursor.fetchone.side_effect = list(fetch_values)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        hana = MagicMock()
        hana.connect.return_value = conn
        hana.schema = "JIVO_OIL_HANADB"
        return hana, conn, cursor

    def _folder_error_response(self, status_code):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = '{"error":{"code":"-5002"}}'
        resp.json.return_value = {
            "error": {
                "code": "-5002",
                "message": "Attachments folder not defined, or Attachments "
                           "folder has been changed or removed ",
            }
        }
        return resp

    def _verify_get_response(self, filename="proof_v2", ext="pdf"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "Attachments2_Lines": [
                {"SourcePath": self.ATTACH_FOLDER, "FileName": filename, "FileExtension": ext}
            ]
        }
        return resp

    @override_settings(SAP_ATTACHMENT_HANA_FALLBACK=True)
    def test_upload_falls_back_to_hana_on_folder_error(self):
        # sequence: MAX(AbsEntry)=167125, NEXTVAL=167126, MIN(USERID) superuser=1
        hana, conn, cursor = self._fake_hana([(167125,), (167126,), (1,)])
        with (
            patch("sap_client.service_layer.attachment_writer.ServiceLayerSession") as msc,
            patch("sap_client.service_layer.attachment_writer.FileUploaderClient") as muc,
            patch("sap_client.service_layer.attachment_writer.requests.post") as mpost,
            patch("sap_client.service_layer.attachment_writer.requests.get") as mget,
            patch("sap_client.service_layer.attachment_writer.HanaConnection", return_value=hana),
            patch.object(self.writer, "_get_attachment_source_path", return_value=self.ATTACH_FOLDER),
            patch.object(self.writer, "_attachment_approval_fields",
                         return_value={"U_CHK": "1", "U_CHK2": "OK"}),
        ):
            msc.return_value.login.return_value = {"B1SESSION": "abc"}
            muc.is_enabled.return_value = True
            uploader = muc.return_value
            uploader.upload.return_value = {"id": 11, "stored_name": "proof_v2.pdf"}
            mpost.return_value = self._folder_error_response(400)
            mget.return_value = self._verify_get_response()

            result = self.writer.upload(self._temp_file(".pdf"), "proof.pdf")

        self.assertEqual(result["AbsoluteEntry"], 167126)
        self.assertEqual(result["StoredFileName"], "proof_v2.pdf")
        uploader.delete.assert_not_called()          # keep the file: it IS the attachment
        conn.commit.assert_called_once()
        sqls = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertTrue(any('."OATC_S".NEXTVAL' in s for s in sqls))
        self.assertTrue(any('INSERT INTO "JIVO_OIL_HANADB"."OATC"' in s for s in sqls))
        atc1 = next(s for s in sqls if 'INSERT INTO "JIVO_OIL_HANADB"."ATC1"' in s)
        # mirrors the confirmed stored format
        self.assertIn('"Copied"', atc1)
        self.assertIn("CURRENT_TIMESTAMP", atc1)

    @override_settings(SAP_ATTACHMENT_HANA_FALLBACK=True)
    def test_add_line_falls_back_to_hana_on_folder_error(self):
        # sequence: OATC exists count=1, MAX(Line)=2, MIN(USERID) superuser=1
        hana, conn, cursor = self._fake_hana([(1,), (2,), (1,)])
        with (
            patch("sap_client.service_layer.attachment_writer.ServiceLayerSession") as msc,
            patch("sap_client.service_layer.attachment_writer.FileUploaderClient") as muc,
            patch("sap_client.service_layer.attachment_writer.requests.patch") as mpatch,
            patch("sap_client.service_layer.attachment_writer.requests.get") as mget,
            patch("sap_client.service_layer.attachment_writer.HanaConnection", return_value=hana),
            patch.object(self.writer, "_attachment_approval_fields",
                         return_value={"U_CHK": "1", "U_CHK2": "OK"}),
        ):
            msc.return_value.login.return_value = {"B1SESSION": "abc"}
            muc.is_enabled.return_value = True
            uploader = muc.return_value
            uploader.upload.return_value = {"id": 12, "stored_name": "proof_v2.pdf"}
            mget.return_value = self._verify_get_response()   # existing entry + verify
            mpatch.return_value = self._folder_error_response(400)

            result = self.writer.add_line_to_existing_attachment(
                absolute_entry=167126,
                file_path=self._temp_file(".pdf"),
                filename="proof.pdf",
            )

        self.assertEqual(result["AbsoluteEntry"], 167126)
        uploader.delete.assert_not_called()
        conn.commit.assert_called_once()
        sqls = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertTrue(any('INSERT INTO "JIVO_OIL_HANADB"."ATC1"' in s for s in sqls))

    def test_no_hana_fallback_when_flag_disabled(self):
        # flag defaults off -> folder error must NOT touch HANA; file cleaned up
        with (
            patch("sap_client.service_layer.attachment_writer.ServiceLayerSession") as msc,
            patch("sap_client.service_layer.attachment_writer.FileUploaderClient") as muc,
            patch("sap_client.service_layer.attachment_writer.requests.post") as mpost,
            patch("sap_client.service_layer.attachment_writer.HanaConnection") as mhana,
            patch.object(self.writer, "_get_attachment_source_path", return_value=self.ATTACH_FOLDER),
            patch.object(self.writer, "_attachment_approval_fields",
                         return_value={"U_CHK": "1", "U_CHK2": "OK"}),
        ):
            msc.return_value.login.return_value = {"B1SESSION": "abc"}
            muc.is_enabled.return_value = True
            uploader = muc.return_value
            uploader.upload.return_value = {"id": 13, "stored_name": "proof_v2.pdf"}
            mpost.return_value = self._folder_error_response(400)

            with self.assertRaises(SAPValidationError):
                self.writer.upload(self._temp_file(".pdf"), "proof.pdf")

            mhana.assert_not_called()
            uploader.delete.assert_called_once_with(13)


class GRPOAPITests(APITestCase):
    """Integration tests for GRPO API endpoint"""

    def setUp(self):
        self.client = APIClient()

        # Create test user (custom User model uses email as USERNAME_FIELD)
        self.user = User.objects.create_user(
            email="test@example.com",
            password="testpass123",
            full_name="Test User",
            employee_code="EMP001"
        )

        # Create company and role
        self.company = Company.objects.create(
            name="Test Company",
            code="JIVO_OIL",
            is_active=True
        )
        self.role = UserRole.objects.create(name="Admin")
        self.user_company = UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=self.role,
            is_default=True,
            is_active=True
        )

        # Authenticate
        self.client.force_authenticate(user=self.user)

    def test_grpo_api_missing_company_header(self):
        """Test GRPO API without Company-Code header"""
        payload = {
            "CardCode": "V001",
            "DocumentLines": [{"ItemCode": "ITEM001", "Quantity": "100"}]
        }
        response = self.client.post("/api/v1/po/grpo/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_grpo_api_invalid_payload(self):
        """Test GRPO API with invalid payload"""
        payload = {"DocumentLines": []}  # Missing CardCode, empty lines

        response = self.client.post(
            "/api/v1/po/grpo/",
            payload,
            format="json",
            HTTP_COMPANY_CODE="JIVO_OIL"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("sap_client.views.SAPClient")
    def test_grpo_api_success(self, mock_client_class):
        """Test successful GRPO creation via API"""
        mock_client = MagicMock()
        mock_client.create_grpo.return_value = {
            "DocEntry": 123,
            "DocNum": 456,
            "CardCode": "V001",
            "CardName": "Test Vendor",
            "DocDate": "2026-02-02",
            "DocTotal": 5000.00
        }
        mock_client_class.return_value = mock_client

        payload = {
            "CardCode": "V001",
            "DocumentLines": [
                {
                    "ItemCode": "ITEM001",
                    "Quantity": "100",
                    "TaxCode": "T1",
                    "UnitPrice": "50"
                }
            ]
        }

        response = self.client.post(
            "/api/v1/po/grpo/",
            payload,
            format="json",
            HTTP_COMPANY_CODE="JIVO_OIL"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["DocNum"], 456)

    @patch("sap_client.views.SAPClient")
    def test_grpo_api_sap_unavailable(self, mock_client_class):
        """Test GRPO API when SAP is unavailable"""
        mock_client = MagicMock()
        mock_client.create_grpo.side_effect = SAPConnectionError("Connection failed")
        mock_client_class.return_value = mock_client

        payload = {
            "CardCode": "V001",
            "DocumentLines": [{"ItemCode": "ITEM001", "Quantity": "100"}]
        }

        response = self.client.post(
            "/api/v1/po/grpo/",
            payload,
            format="json",
            HTTP_COMPANY_CODE="JIVO_OIL"
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch("sap_client.views.SAPClient")
    def test_grpo_api_sap_validation_error(self, mock_client_class):
        """Test GRPO API with SAP validation error"""
        mock_client = MagicMock()
        mock_client.create_grpo.side_effect = SAPValidationError("Invalid item code")
        mock_client_class.return_value = mock_client

        payload = {
            "CardCode": "V001",
            "DocumentLines": [{"ItemCode": "INVALID", "Quantity": "100"}]
        }

        response = self.client.post(
            "/api/v1/po/grpo/",
            payload,
            format="json",
            HTTP_COMPANY_CODE="JIVO_OIL"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid item code", response.data["detail"])

    def test_grpo_api_unauthenticated(self):
        """Test GRPO API without authentication"""
        self.client.logout()
        payload = {
            "CardCode": "V001",
            "DocumentLines": [{"ItemCode": "ITEM001", "Quantity": "100"}]
        }

        response = self.client.post(
            "/api/v1/po/grpo/",
            payload,
            format="json",
            HTTP_COMPANY_CODE="JIVO_OIL"
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("sap_client.views.SAPClient")
    def test_open_po_lookup_by_number_success(self, mock_client_class):
        """Test exact PO lookup endpoint"""
        mock_client = MagicMock()
        mock_client.get_open_po_by_number.return_value = PODTO(
            po_number="4500001234",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            doc_entry=1001,
            branch_id=1,
            vendor_ref="INV-001",
            doc_date=date(2026, 5, 14),
            items=[
                POItemDTO(
                    po_item_code="ITEM001",
                    item_name="Groundnut Oil",
                    ordered_qty=100,
                    received_qty=25,
                    remaining_qty=75,
                    uom="KG",
                    rate=10,
                    line_num=0,
                )
            ],
        )
        mock_client_class.return_value = mock_client

        response = self.client.get(
            "/api/v1/po/open-pos/4500001234/items/",
            HTTP_COMPANY_CODE="JIVO_OIL"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["po_number"], "4500001234")
        self.assertEqual(response.data["supplier_name"], "Test Supplier")
        self.assertEqual(str(response.data["items"][0]["remaining_qty"]), "75.000")
        mock_client.get_open_po_by_number.assert_called_once_with("4500001234")

    @patch("sap_client.views.SAPClient")
    def test_open_po_lookup_by_number_not_found(self, mock_client_class):
        """Test exact PO lookup endpoint when no open PO exists"""
        mock_client = MagicMock()
        mock_client.get_open_po_by_number.return_value = None
        mock_client_class.return_value = mock_client

        response = self.client.get(
            "/api/v1/po/open-pos/4500009999/items/",
            HTTP_COMPANY_CODE="JIVO_OIL"
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["detail"], "Open PO not found")


class POAdditionalExpenseReaderTests(TestCase):
    """Reader for the PO's freight lines (POR3), which the GRPO screen pre-fills.

    Freight is agreed at purchase time; the GRPO operator cannot know the expense
    code (it is SAP master data and differs per company), so it is read here.
    """

    def _reader(self, rows):
        from .hana.po_reader import HanaPOReader

        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch.object(HanaPOReader, "__init__", lambda self, context: None):
            reader = HanaPOReader(None)
        reader.connection = MagicMock()
        reader.connection.connect.return_value = conn
        reader.connection.schema = "JIVO_OIL_HANADB"
        return reader, cursor

    def test_maps_por3_rows_and_distribution_rule(self):
        rows = [
            (
                12345, 4, 2, "FREIGHT INWARD DRCT", 15200.0, "IGST@18",
                "Q", "", "O", "5100002", "00996791", 8000.0,
            ),
        ]
        reader, _ = self._reader(rows)

        expenses = reader.get_po_additional_expenses([12345])

        self.assertEqual(list(expenses), [12345])
        expense = expenses[12345][0]
        self.assertEqual(expense.expense_code, 2)
        self.assertEqual(expense.expense_name, "FREIGHT INWARD DRCT")
        self.assertEqual(expense.line_num, 4)
        self.assertEqual(expense.tax_code, "IGST@18")
        # 'Q' is the Service Layer's aedm_Quantity — sending the raw char, or
        # nothing at all, means the freight does not load onto item cost.
        self.assertEqual(expense.distribution_method, "aedm_Quantity")
        # POR3."DrawnTotal" is not maintained here, so the drawn figure is summed
        # off posted GRPOs; only the remainder should be offered for pre-fill.
        self.assertEqual(expense.posted_amount, 8000.0)
        self.assertEqual(expense.remaining_amount, 7200.0)

    def test_remaining_never_goes_negative(self):
        """An over-drawn PO line offers nothing rather than a negative charge."""
        rows = [
            (12345, 0, 2, "FREIGHT INWARD DRCT", 6500.0, "IGST@18",
             "Q", "", "C", "5100002", "", 11500.0),
        ]
        reader, _ = self._reader(rows)

        expense = reader.get_po_additional_expenses([12345])[12345][0]

        self.assertEqual(expense.remaining_amount, 0.0)

    def test_falls_back_to_code_when_oexd_row_is_missing(self):
        rows = [
            (12345, 0, 99, "", 100.0, "", "N", "", "O", "", "", 0.0),
        ]
        reader, _ = self._reader(rows)

        expense = reader.get_po_additional_expenses([12345])[12345][0]

        self.assertEqual(expense.expense_name, "Expense 99")
        self.assertEqual(expense.distribution_method, "aedm_None")

    def test_deduplicates_doc_entries_and_skips_blanks(self):
        reader, cursor = self._reader([])

        reader.get_po_additional_expenses([12345, 12345, None, 0, 999])

        params = cursor.execute.call_args[0][1]
        self.assertEqual(params, [999, 12345])

    def test_no_query_without_doc_entries(self):
        reader, cursor = self._reader([])

        self.assertEqual(reader.get_po_additional_expenses([]), {})
        self.assertEqual(reader.get_po_additional_expenses([None, 0]), {})
        cursor.execute.assert_not_called()

    def test_hana_failure_is_fail_soft(self):
        """The GRPO preview must still render when the lookup fails."""
        from hdbcli import dbapi
        from .hana.po_reader import HanaPOReader

        with patch.object(HanaPOReader, "__init__", lambda self, context: None):
            reader = HanaPOReader(None)
        reader.connection = MagicMock()
        reader.connection.connect.side_effect = dbapi.Error("HANA down")

        self.assertEqual(reader.get_po_additional_expenses([12345]), {})


@override_settings(
    SAP_FILE_UPLOADER_ENABLED=True,
    SAP_FILE_UPLOADER_BASE_URL="http://uploader.test:8013",
    SAP_FILE_UPLOADER_API_KEY="test-key",
    SAP_FILE_UPLOADER_FOLDER_IDS={"JIVO_OIL": 3},
    SAP_FILE_UPLOADER_TIMEOUT_SECONDS=30,
    SAP_FILE_UPLOADER_MAX_ATTEMPTS=3,
    SAP_FILE_UPLOADER_RETRY_BACKOFF_SECONDS=0,
    SAP_FILE_UPLOADER_TOTAL_BUDGET_SECONDS=65,
)
class FileUploaderClientRetryTests(SimpleTestCase):
    """
    The uploader writes to a Windows share that stalls intermittently: the
    request hangs or comes back as a bare 500 while uploads either side of it
    succeed, and a manual retry has always worked. These cover the client
    retrying that for the operator without leaving duplicate files behind.
    """

    def setUp(self):
        self.client_under_test = FileUploaderClient("JIVO_OIL")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp.write(b"pdf-bytes")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        self.file_path = tmp.name
        self.file_size = os.path.getsize(tmp.name)

    def _response(self, status_code, payload=None, text=""):
        response = MagicMock()
        response.status_code = status_code
        if payload is None:
            response.json.side_effect = ValueError("no json")
        else:
            response.json.return_value = payload
        response.text = text
        return response

    def _saved(self, file_id=901, stored_name="1311.pdf"):
        return self._response(
            200,
            {"data": {"files": [{"id": file_id, "stored_name": stored_name}]}},
        )

    def _listing(self, rows):
        return self._response(200, {"data": rows})

    def test_retries_a_500_and_returns_the_file_the_retry_saved(self):
        with (
            patch(
                "sap_client.service_layer.file_uploader_client.requests.post"
            ) as mock_post,
            patch(
                "sap_client.service_layer.file_uploader_client.requests.get"
            ) as mock_get,
        ):
            mock_post.side_effect = [
                self._response(500, text="Internal Server Error"),
                self._saved(),
            ]
            mock_get.return_value = self._listing([])

            result = self.client_under_test.upload(self.file_path, "1311.pdf")

        self.assertEqual(result["id"], 901)
        self.assertEqual(mock_post.call_count, 2)

    def test_retries_a_timeout_and_returns_the_file_the_retry_saved(self):
        with (
            patch(
                "sap_client.service_layer.file_uploader_client.requests.post"
            ) as mock_post,
            patch(
                "sap_client.service_layer.file_uploader_client.requests.get"
            ) as mock_get,
        ):
            mock_post.side_effect = [
                requests.exceptions.Timeout("read timed out"),
                self._saved(),
            ]
            mock_get.return_value = self._listing([])

            result = self.client_under_test.upload(self.file_path, "1311.pdf")

        self.assertEqual(result["id"], 901)
        self.assertEqual(mock_post.call_count, 2)

    def test_reuses_the_file_a_stalled_attempt_already_saved(self):
        """A hung write can still commit; re-uploading would leave a _v2 twin."""
        landed = {
            "id": 802,
            "folder_id": 3,
            "original_name": "1311.pdf",
            "stored_name": "1311.pdf",
            "size": self.file_size,
            "uploader": "factory_app_v2",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        with (
            patch(
                "sap_client.service_layer.file_uploader_client.requests.post"
            ) as mock_post,
            patch(
                "sap_client.service_layer.file_uploader_client.requests.get"
            ) as mock_get,
        ):
            mock_post.side_effect = requests.exceptions.Timeout("read timed out")
            mock_get.return_value = self._listing([landed])

            result = self.client_under_test.upload(self.file_path, "1311.pdf")

        self.assertEqual(result["id"], 802)
        self.assertEqual(mock_post.call_count, 1)

    def test_ignores_an_older_upload_of_the_same_name(self):
        stale = {
            "id": 299,
            "folder_id": 3,
            "original_name": "1311.pdf",
            "stored_name": "1311.pdf",
            "size": self.file_size,
            "uploader": "factory_app_v2",
            "uploaded_at": "2026-06-13T11:30:21.273418+00:00",
        }
        with (
            patch(
                "sap_client.service_layer.file_uploader_client.requests.post"
            ) as mock_post,
            patch(
                "sap_client.service_layer.file_uploader_client.requests.get"
            ) as mock_get,
        ):
            mock_post.side_effect = [
                requests.exceptions.Timeout("read timed out"),
                self._saved(file_id=903),
            ]
            mock_get.return_value = self._listing([stale])

            result = self.client_under_test.upload(self.file_path, "1311.pdf")

        self.assertEqual(result["id"], 903)
        self.assertEqual(mock_post.call_count, 2)

    def test_gives_up_after_max_attempts_with_the_sap_error(self):
        with (
            patch(
                "sap_client.service_layer.file_uploader_client.requests.post"
            ) as mock_post,
            patch(
                "sap_client.service_layer.file_uploader_client.requests.get"
            ) as mock_get,
        ):
            mock_post.return_value = self._response(
                500, text="Internal Server Error"
            )
            mock_get.return_value = self._listing([])

            with self.assertRaises(SAPDataError) as context:
                self.client_under_test.upload(self.file_path, "1311.pdf")

        self.assertIn("Internal Server Error", str(context.exception))
        self.assertEqual(mock_post.call_count, 3)

    def test_does_not_retry_a_rejected_upload(self):
        """A 400 is the uploader refusing the file; retrying only wastes time."""
        with (
            patch(
                "sap_client.service_layer.file_uploader_client.requests.post"
            ) as mock_post,
            patch(
                "sap_client.service_layer.file_uploader_client.requests.get"
            ) as mock_get,
        ):
            mock_post.return_value = self._response(
                400, {"error": {"message": "file type not allowed"}}
            )

            with self.assertRaises(SAPDataError) as context:
                self.client_under_test.upload(self.file_path, "1311.pdf")

        self.assertIn("file type not allowed", str(context.exception))
        self.assertEqual(mock_post.call_count, 1)
        mock_get.assert_not_called()

    def test_does_not_retry_an_authentication_failure(self):
        with patch(
            "sap_client.service_layer.file_uploader_client.requests.post"
        ) as mock_post:
            mock_post.return_value = self._response(401)

            with self.assertRaises(SAPConnectionError):
                self.client_under_test.upload(self.file_path, "1311.pdf")

        self.assertEqual(mock_post.call_count, 1)

    def test_attempt_timeout_never_overruns_the_retry_budget(self):
        """
        nginx cuts the browser request at 120s, so the retries plus the SAP
        Service Layer calls that follow them have to fit inside that.
        """
        with (
            patch(
                "sap_client.service_layer.file_uploader_client.requests.post"
            ) as mock_post,
            patch(
                "sap_client.service_layer.file_uploader_client.requests.get"
            ) as mock_get,
        ):
            mock_post.side_effect = [
                requests.exceptions.Timeout("read timed out"),
                self._saved(),
            ]
            mock_get.return_value = self._listing([])

            self.client_under_test.upload(self.file_path, "1311.pdf")

        timeouts = [c.kwargs["timeout"] for c in mock_post.call_args_list]
        self.assertEqual(len(timeouts), 2)
        self.assertLessEqual(sum(timeouts), 65)
        for value in timeouts:
            self.assertLessEqual(value, 30)
