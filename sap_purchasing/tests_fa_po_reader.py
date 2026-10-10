"""Open fixed-asset PO lines: the live SQL filters, the copy fallback, the DTOs.

``HanaPOReader._fetch`` is patched on the subclass, so no HANA is touched; the
copy fallback patches ``sap_mirror.purchase_orders.open_po_rows`` (the
function ``HanaPOReader._from_copy`` calls), so no database is touched either.

Run: ``manage.py test sap_purchasing.tests_fa_po_reader --settings=config.sqlite_test_settings``
"""

from datetime import datetime
from unittest import mock

from django.test import SimpleTestCase, override_settings

from sap_client.dtos import PODTO
from sap_client.exceptions import SAPConnectionError
from sap_purchasing import fa_po_reader
from sap_purchasing.fa_po_reader import HanaFixedAssetPOReader


def _line(item, whs, line_num=0, doc_num=221026008, doc_entry=14180):
    """One open-line row in ``OPEN_LINE_COLUMNS`` order (19 columns)."""
    return [doc_num, "VENDA001768", "ABD Engineering", item, "Tank 50 KL", 1.0, 0.0, 1.0,
            "Nos", 1270.0, doc_entry, line_num, "IGST@18", whs, "1204023", 2, "", datetime(2026, 10, 3),
            "CANOLA"]


def _reader():
    ctx = mock.Mock()
    ctx.hana = {"schema": "TEST_JIVO_OIL_HANADB"}
    ctx.company_code = "JIVO_OIL"
    with mock.patch("sap_client.hana.po_reader.HanaConnection") as conn_cls:
        conn_cls.return_value.schema = "TEST_JIVO_OIL_HANADB"
        return HanaFixedAssetPOReader(ctx)


@override_settings(FIXED_ASSET_WAREHOUSES="BH-FA,DL-FA,GP-FA")
class LiveQueryTests(SimpleTestCase):
    def test_sql_filters_item_lines_in_fa_warehouses(self):
        reader = _reader()
        with mock.patch.object(HanaFixedAssetPOReader, "_fetch",
                               return_value=[_line("CG0000073", "BH-FA")]) as fetch:
            pos = reader.get_open_fixed_asset_pos("VENDA001768")
        sql, params = fetch.call_args[0]
        self.assertIn("T0.\"DocType\" = 'I'", sql)
        self.assertIn('T1."ItemCode" IS NOT NULL', sql)
        self.assertIn('T1."WhsCode" IN (?, ?, ?)', sql)
        self.assertIn('"TEST_JIVO_OIL_HANADB"."OPOR"', sql)
        self.assertEqual(params, ["VENDA001768", "BH-FA", "DL-FA", "GP-FA"])

        self.assertEqual(len(pos), 1)
        self.assertIsInstance(pos[0], PODTO)
        self.assertEqual(pos[0].po_number, "221026008")
        self.assertEqual(pos[0].doc_entry, 14180)
        item = pos[0].items[0]
        self.assertEqual((item.po_item_code, item.warehouse_code, item.tax_code, item.account_code),
                         ("CG0000073", "BH-FA", "IGST@18", "1204023"))

    def test_explicit_warehouses(self):
        reader = _reader()
        with mock.patch.object(HanaFixedAssetPOReader, "_fetch", return_value=[]) as fetch:
            reader.get_open_fixed_asset_pos("V", ["DL-FA", " DL-FA ", ""])
        sql, params = fetch.call_args[0]
        self.assertIn('T1."WhsCode" IN (?)', sql)
        self.assertEqual(params, ["V", "DL-FA"])

    @override_settings(FIXED_ASSET_WAREHOUSES=["BH-FA", "DL-FA"])
    def test_list_setting(self):
        self.assertEqual(fa_po_reader.fixed_asset_warehouses(), ["BH-FA", "DL-FA"])

    @override_settings(FIXED_ASSET_WAREHOUSES=" , ")
    def test_no_warehouses_is_a_value_error(self):
        reader = _reader()
        with mock.patch.object(HanaFixedAssetPOReader, "_fetch") as fetch:
            with self.assertRaises(ValueError):
                reader.get_open_fixed_asset_pos("V")
        fetch.assert_not_called()


@override_settings(FIXED_ASSET_WAREHOUSES="BH-FA,DL-FA,GP-FA")
class CopyFallbackTests(SimpleTestCase):
    def test_copy_rows_are_filtered_by_warehouse_and_item(self):
        reader = _reader()
        copy_rows = [
            _line("CG0000073", "BH-FA", 0),
            _line("RM0000001", "BH-RM", 1),   # not a fixed-asset warehouse
            _line(None, "BH-FA", 2),          # service line: no item
            _line("  ", "DL-FA", 3),          # blank item
        ]
        with mock.patch.object(HanaFixedAssetPOReader, "_fetch",
                               side_effect=SAPConnectionError("HANA down")), \
                mock.patch("sap_mirror.purchase_orders.open_po_rows",
                           return_value=copy_rows) as open_po_rows:
            pos = reader.get_open_fixed_asset_pos("VENDA001768")
        open_po_rows.assert_called_once_with("JIVO_OIL", supplier_code="VENDA001768")
        self.assertEqual([i.line_num for i in pos[0].items], [0])

    def test_no_copy_re_raises(self):
        reader = _reader()
        with mock.patch.object(HanaFixedAssetPOReader, "_fetch",
                               side_effect=SAPConnectionError("HANA down")), \
                mock.patch("sap_mirror.purchase_orders.open_po_rows", return_value=None):
            with self.assertRaises(SAPConnectionError):
                reader.get_open_fixed_asset_pos("V")

    def test_filter_copy_rows_is_pure(self):
        rows = [_line("CG1", "GP-FA"), _line("CG2", "XX")]
        self.assertEqual(len(fa_po_reader.filter_copy_rows(rows, ["GP-FA"])), 1)


class ModuleFunctionTests(SimpleTestCase):
    def test_builds_the_company_context(self):
        with mock.patch("sap_purchasing.fa_po_reader.CompanyContext") as ctx_cls, \
                mock.patch.object(HanaFixedAssetPOReader, "get_open_fixed_asset_pos",
                                  return_value=[]) as get:
            self.assertEqual(fa_po_reader.get_open_fixed_asset_pos("JIVO_OIL", "V", ["BH-FA"]), [])
        ctx_cls.assert_called_once_with("JIVO_OIL")
        get.assert_called_once_with("V", ["BH-FA"])
