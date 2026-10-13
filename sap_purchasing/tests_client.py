"""``PurchasingClient`` is a thin router: each method reaches the right module
function with its own company code.

Run: ``manage.py test sap_purchasing.tests_client --settings=config.sqlite_test_settings``
"""

from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from sap_purchasing.client import PurchasingClient


class PurchasingClientTests(SimpleTestCase):
    def setUp(self):
        self.client_ = PurchasingClient("JIVO_BEVERAGES")

    def test_writers(self):
        cases = [
            ("create_purchase_order_document", "create_purchase_order", ({"a": 1},)),
            ("create_purchase_delivery_note_document", "create_purchase_delivery_note", ({"a": 1},)),
            ("create_purchase_invoice_draft", "create_purchase_invoice_draft", ({"a": 1},)),
            ("close_purchase_order", "close_purchase_order", (5,)),
            ("save_purchasing_draft_to_document", "save_draft_to_document", ("22", 77)),
        ]
        for method, function, args in cases:
            with self.subTest(method=method), \
                    mock.patch(f"sap_purchasing.service_layer.{function}") as fn:
                getattr(self.client_, method)(*args)
                fn.assert_called_once_with("JIVO_BEVERAGES", *args)

    def test_readback(self):
        cases = [
            ("find_document_by_marker", "find_by_marker", ("20", "V", "[JI-CB G5]")),
            ("find_document_by_draft_key", "find_by_draft_key", ("22", 77)),
            ("find_draft_by_marker", "find_draft_by_marker", ("20", "V", "[JI-CB G5]")),
            ("document_lines", "document_lines", ("22", 1)),
            ("document_state", "document_state", ("22", 1)),
            ("num_at_card_taken", "num_at_card_taken", ("20", "V", "X")),
            ("item_flags", "item_flags", (["CG1"],)),
        ]
        with mock.patch("sap_purchasing.client.PurchasingReadback") as reader_cls:
            for method, reader_method, args in cases:
                with self.subTest(method=method):
                    getattr(self.client_, method)(*args)
                    reader_cls.assert_called_with("JIVO_BEVERAGES")
                    getattr(reader_cls.return_value, reader_method).assert_called_with(*args)

    def test_series_and_pos(self):
        with mock.patch("sap_purchasing.series.resolve_series_for_branch") as fn:
            self.client_.resolve_series_for_branch("18", date(2026, 9, 28), None, "IMP")
        fn.assert_called_once_with("JIVO_BEVERAGES", "18", date(2026, 9, 28),
                                   bpl_id=None, name_hint="IMP")
        with mock.patch("sap_purchasing.fa_po_reader.get_open_fixed_asset_pos") as fn:
            self.client_.get_open_fixed_asset_pos("V")
        fn.assert_called_once_with("JIVO_BEVERAGES", "V", None)

    def test_attachments_pass_through_to_sap_client(self):
        with mock.patch("sap_client.client.SAPClient") as sap_client_cls:
            self.client_.upload_attachment("/tmp/a.pdf", "a.pdf", allow_metadata_fallback=True)
            self.client_.add_line_to_existing_attachment(9, "/tmp/b.pdf", "b.pdf")
        sap_client_cls.assert_called_with("JIVO_BEVERAGES")
        sap_client_cls.return_value.upload_attachment.assert_called_once_with(
            "/tmp/a.pdf", "a.pdf", allow_metadata_fallback=True)
        sap_client_cls.return_value.add_line_to_existing_attachment.assert_called_once_with(
            9, "/tmp/b.pdf", "b.pdf", allow_metadata_fallback=False)
