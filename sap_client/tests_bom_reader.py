"""The BOM reader behind bom_changes (OITT / ITT1), with a mocked cursor.

    python manage.py test sap_client.tests_bom_reader --settings=config.sqlite_test_settings

Nothing here reaches HANA.
"""

from datetime import date
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from hdbcli import dbapi

from .exceptions import SAPConnectionError, SAPDataError
from .hana.bom_reader import HanaBOMReader


def _context():
    context = MagicMock()
    context.hana = {"host": "h", "port": 1, "user": "u", "password": "p", "schema": "SCHEMA"}
    context.company_code = "JIVO_OIL"
    return context


class BOMReaderTests(SimpleTestCase):
    def setUp(self):
        self.reader = HanaBOMReader(_context())
        self.cursor = MagicMock()
        self.conn = MagicMock()
        self.conn.cursor.return_value = self.cursor
        patcher = patch.object(self.reader.connection, "connect", return_value=self.conn)
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)

    def _sql_and_params(self):
        return self.cursor.execute.call_args[0]

    def test_search_binds_the_text_over_every_tree(self):
        self.cursor.fetchall.return_value = [
            ("FG0000121", "CANOLA OIL 1 LTR", "P", 20, "BH-PF", 3, 1, date(2026, 9, 1)),
        ]
        rows = self.reader.search_trees("can'ola", limit=30)
        sql, params = self._sql_and_params()
        self.assertNotIn("can'ola", sql.lower())
        self.assertEqual(params, ("CAN'OLA", "%CAN'OLA%", "%CAN'OLA%", "%CAN'OLA%"))
        self.assertIn('"SCHEMA"."OITT"', sql)
        self.assertIn('"SCHEMA"."ITT1"', sql)
        self.assertIn("TOP 30", sql)
        self.assertEqual(
            rows,
            [{
                "tree_code": "FG0000121", "description": "CANOLA OIL 1 LTR", "tree_type": "P",
                "bom_type": "Production", "sap_tree_type": "iProductionTree", "quantity": 20.0,
                "warehouse": "BH-PF", "item_count": 3, "resource_count": 1, "updated_at": "2026-09-01",
            }],
        )

    def test_an_empty_search_lists_trees_and_the_limit_is_capped(self):
        self.cursor.fetchall.return_value = []
        self.reader.search_trees("", limit=10_000)
        sql, params = self._sql_and_params()
        self.assertEqual(params[0], "")
        self.assertIn("TOP 200", sql)

    def test_one_tree_with_items_resources_and_text(self):
        head = ("FG1", "Parent", "P", 4, "BH-PF", "OIL", "PRJ", -1, date(2026, 9, 2))
        self.cursor.fetchall.return_value = [
            head + (0, 0, 4, "RM1", "Oil", 2.5, "BH-PC", "B", 150.0, "INR", "note", "LTR"),
            head + (1, 1, 290, "RES1", "Filling", 4, "", "M", None, "", "", ""),
            head + (2, 2, -18, "", "", 0, "", None, None, "", "a text line", ""),
        ]
        tree = self.reader.get_tree("FG1")
        sql, params = self._sql_and_params()
        self.assertEqual(params, ("FG1",))
        self.assertIn('WHERE T."Code" = ?', sql)
        self.assertEqual(
            {k: tree[k] for k in ("tree_code", "bom_type", "quantity", "warehouse", "distribution_rule", "project")},
            {"tree_code": "FG1", "bom_type": "Production", "quantity": 4.0, "warehouse": "BH-PF",
             "distribution_rule": "OIL", "project": "PRJ"},
        )
        self.assertEqual([line["item_type"] for line in tree["lines"]], ["item", "resource", "text"])
        self.assertEqual(tree["lines"][0]["issue_method"], "Backflush")
        self.assertEqual(tree["lines"][0]["unit_cost"], 150.0)
        self.assertEqual((tree["item_count"], tree["resource_count"]), (1, 1))

    def test_a_tree_with_no_lines_comes_back_empty(self):
        head = ("FG2", "", "S", 1, "", "", "", -1, None)
        self.cursor.fetchall.return_value = [head + (None,) * 12]
        tree = self.reader.get_tree("FG2")
        self.assertEqual(tree["lines"], [])
        self.assertEqual(tree["bom_type"], "Sales")

    def test_no_tree_is_none(self):
        self.cursor.fetchall.return_value = []
        self.assertIsNone(self.reader.get_tree("NOPE"))

    def test_exists(self):
        self.cursor.fetchall.return_value = [(1,)]
        self.assertTrue(self.reader.tree_exists("FG1"))
        self.assertEqual(self._sql_and_params()[1], ("FG1",))
        self.cursor.fetchall.return_value = [(0,)]
        self.assertFalse(self.reader.tree_exists("FG1"))

    def test_could_not_connect_raises_rather_than_saying_no(self):
        self.connect.side_effect = dbapi.Error("down")
        with self.assertRaises(SAPConnectionError):
            self.reader.tree_exists("FG1")

    def test_a_failed_query_raises(self):
        self.cursor.execute.side_effect = dbapi.Error("invalid column")
        with self.assertRaises(SAPDataError):
            self.reader.get_tree("FG1")
        self.conn.close.assert_called_once()


@patch("sap_client.client.CompanyContext")
@patch("sap_client.hana.bom_reader.HanaBOMReader")
class SAPClientBOMTests(SimpleTestCase):
    def test_the_client_hands_each_read_to_the_reader(self, reader_class, context_class):
        from .client import SAPClient

        reader = reader_class.return_value
        reader.search_trees.return_value = ["row"]
        reader.get_tree.return_value = {"tree_code": "FG1"}
        reader.tree_exists.return_value = True
        client = SAPClient("JIVO_OIL")
        self.assertEqual(client.search_product_trees("fg", limit=5), ["row"])
        reader.search_trees.assert_called_once_with("fg", limit=5)
        self.assertEqual(client.get_product_tree("FG1"), {"tree_code": "FG1"})
        self.assertTrue(client.product_tree_exists("FG1"))
        reader_class.assert_called_with(context_class.return_value)
