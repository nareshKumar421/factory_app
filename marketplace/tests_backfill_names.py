"""The masters can be saved nameless during a SAP outage; this repairs them."""
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from company.models import Company

from .models import (
    ComboComponent, ComboComponentType, ComboDefinition, MarketplaceChannel,
    SkuMapping, SkuType,
)

MASTER = {
    "FG0000005": "EXTRA LIGHT OLIVE 1 LTR 16 PCS",
    "FG0000028": "POMACE OLIVE 1 LTR 16 PCS",
}


class BackfillItemNamesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        combo = ComboDefinition.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            code="CB1", name="Kit")
        # Saved during the outage: a code, no name.
        self.blank = ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG,
            item_code="FG0000005", item_name="", quantity=Decimal("1"))
        # Saved with a name that disagrees with SAP.
        self.wrong = ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG,
            item_code="FG0000028", item_name="Pomace (typed by hand)",
            quantity=Decimal("1"))
        self.unknown = SkuMapping.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            marketplace_sku="SKU-X", sku_type=SkuType.RAW,
            fg_item_code="FG9999999", fg_item_name="")

    def _run(self, **kw):
        out = StringIO()
        with patch("marketplace.services.item_master.lookup_names", return_value=MASTER):
            call_command("mp_backfill_item_names", stdout=out, stderr=out, **kw)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        text = self._run()
        self.assertIn("Dry run", text)
        self.blank.refresh_from_db()
        self.assertEqual(self.blank.item_name, "")

    def test_blank_names_are_filled_from_sap(self):
        self._run(apply=True)
        self.blank.refresh_from_db()
        self.assertEqual(self.blank.item_name, MASTER["FG0000005"])

    def test_a_name_that_disagrees_with_sap_is_corrected(self):
        self._run(apply=True)
        self.wrong.refresh_from_db()
        self.assertEqual(self.wrong.item_name, MASTER["FG0000028"])

    def test_a_code_sap_does_not_know_is_reported_not_invented(self):
        text = self._run(apply=True)
        self.assertIn("FG9999999", text)
        self.unknown.refresh_from_db()
        self.assertEqual(self.unknown.fg_item_name, "")

    def test_it_refuses_when_the_master_cannot_be_read(self):
        """The outage that caused this must not be mistaken for 'no names exist'."""
        with patch("marketplace.services.item_master.lookup_names", return_value=None):
            with self.assertRaises(CommandError) as e:
                call_command("mp_backfill_item_names", stdout=StringIO())
        self.assertIn("HANA", str(e.exception))
