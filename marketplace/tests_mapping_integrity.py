"""Mapping integrity: a line that cannot be resolved must stop, never vanish.

Six defects found on 17 Aug 2026 by auditing the mapping mechanism. Every one of
them failed silently -- the order kept moving and shipped the wrong thing, or
nothing at all. The rule they all now share: resolution either produces an item or
says which line it could not resolve.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from company.models import Company

from .models import (
    ComboComponent, ComboComponentOption, ComboComponentType, ComboDefinition,
    MarketplaceChannel, MarketplaceOrder, MarketplaceOrderLine, OrderImportBatch,
    SkuMapping, SkuMappingOption, SkuType,
)
from .services.resolve_service import MappingIndex, load_mappings, resolve_lines


class MappingIntegrityBase(TestCase):
    channel = MarketplaceChannel.FLIPKART

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = get_user_model().objects.create(
            email="mi@t.com", full_name="Ops", employee_code="MI1", is_active=True)
        self.batch = OrderImportBatch.objects.create(
            company=self.company, channel=self.channel, filename="mi.csv")
        self.order = MarketplaceOrder.objects.create(
            company=self.company, channel=self.channel, order_id="OD-MI",
            import_batch=self.batch)

    def _line(self, **kw):
        kw.setdefault("ordered_quantity", 1)
        return MarketplaceOrderLine.objects.create(order=self.order, **kw)

    def _resolve(self, line):
        return resolve_lines([line], "", load_mappings(self.company, self.channel))


class NothingShipsSilently(MappingIntegrityBase):
    """Defect 1 — resolving to nothing passed every gate."""

    def test_a_broken_option_stops_the_line_instead_of_emptying_it(self):
        """A COMBO option with no combo resolves to a blank code. The order used to
        come back with no lines AND no complaint, and went onto a delivery note
        carrying nothing."""
        m = SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-B",
            fsn="FSNB", sku_type=SkuType.RAW, fg_item_code="FG-GOOD")
        SkuMappingOption.objects.create(
            mapping=m, label="broken", sku_type=SkuType.COMBO, combo=None, is_default=True)

        r = self._resolve(self._line(marketplace_sku="SKU-B", fsn="FSNB", ordered_quantity=5))
        self.assertEqual(r["resolved_lines"], [])
        self.assertEqual(r["unmapped_skus"], ["FSNB"])

    def test_a_line_with_no_keys_is_reported_by_its_id(self):
        """Neither FSN nor SKU: there is nothing to name it by, so it used to be
        skipped entirely — the one case that reported nothing at all."""
        line = self._line(marketplace_sku="", fsn="", ordered_quantity=3)
        r = self._resolve(line)
        self.assertEqual(r["resolved_lines"], [])
        self.assertEqual(r["unmapped_skus"], [f"(order line {line.pk}: no FSN or SKU)"])

    def test_an_empty_combo_stops_the_line(self):
        combo = ComboDefinition.objects.create(
            company=self.company, channel=self.channel, code="CB-EMPTY", name="Empty")
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-E",
            sku_type=SkuType.COMBO, combo=combo)
        r = self._resolve(self._line(marketplace_sku="SKU-E"))
        self.assertEqual(r["resolved_lines"], [])
        self.assertEqual(r["unmapped_skus"], ["SKU-E"])

    def test_a_resolvable_line_is_not_reported(self):
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-OK",
            sku_type=SkuType.RAW, fg_item_code="FG-OK", fg_item_name="Fine")
        r = self._resolve(self._line(marketplace_sku="SKU-OK", ordered_quantity=2))
        self.assertEqual(r["unmapped_skus"], [])
        self.assertEqual(r["resolved_lines"][0]["item_code"], "FG-OK")
        self.assertEqual(r["resolved_lines"][0]["required_quantity"], Decimal("2"))


class KeysDoNotCollide(MappingIntegrityBase):
    """Defect 3 — FSN and SKU shared one index."""

    def test_an_fsn_cannot_hijack_another_mappings_sku(self):
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-A",
            fsn="COLLIDE", sku_type=SkuType.RAW, fg_item_code="FG-A", fg_item_name="A")
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="COLLIDE",
            fsn="", sku_type=SkuType.RAW, fg_item_code="FG-B", fg_item_name="B")

        r = self._resolve(self._line(marketplace_sku="COLLIDE"))
        self.assertEqual([x["item_code"] for x in r["resolved_lines"]], ["FG-B"])

    def test_an_exact_fsn_match_still_wins(self):
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-X",
            fsn="FSNX", sku_type=SkuType.RAW, fg_item_code="FG-BY-FSN")
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="FSNX",
            sku_type=SkuType.RAW, fg_item_code="FG-BY-SKU")
        # The line's FSN matches one mapping's fsn and another's marketplace_sku.
        r = self._resolve(self._line(fsn="FSNX", marketplace_sku=""))
        self.assertEqual([x["item_code"] for x in r["resolved_lines"]], ["FG-BY-FSN"])

    def test_a_cross_match_still_resolves_legacy_rows(self):
        """A sheet whose FSN column holds what the mapping stored as its SKU must
        keep resolving — the cross match is last, not gone."""
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="LEGACY",
            fsn="", sku_type=SkuType.RAW, fg_item_code="FG-LEGACY")
        r = self._resolve(self._line(fsn="LEGACY", marketplace_sku=""))
        self.assertEqual([x["item_code"] for x in r["resolved_lines"]], ["FG-LEGACY"])

    def test_case_variant_rows_do_not_decide_by_collation(self):
        """'sku-a' and 'SKU-A' are two legal rows. One shadows the other; the loser
        is logged rather than chosen by the database's collation."""
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="sku-a",
            sku_type=SkuType.RAW, fg_item_code="FG-LOWER")
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-A",
            sku_type=SkuType.RAW, fg_item_code="FG-UPPER")
        with self.assertLogs("marketplace.services.resolve_service", "WARNING") as logs:
            index = load_mappings(self.company, self.channel)
        self.assertIn("shadows", logs.output[0])
        # Deterministic: whichever row the index kept, it is the only one reachable.
        self.assertEqual(len(index.by_sku), 1)


class DeactivationIsHonoured(MappingIntegrityBase):
    """Defect 4 — only the mapping's is_active was ever checked."""

    def _combo_mapping(self, active):
        combo = ComboDefinition.objects.create(
            company=self.company, channel=self.channel, code="CB", name="Kit",
            is_active=active)
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG, item_code="FG-KIT",
            item_name="Kit item", quantity=Decimal("2"))
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-C",
            sku_type=SkuType.COMBO, combo=combo)

    def test_a_switched_off_combo_stops_shipping(self):
        self._combo_mapping(active=False)
        r = self._resolve(self._line(marketplace_sku="SKU-C"))
        self.assertEqual(r["resolved_lines"], [])
        self.assertEqual(r["unmapped_skus"], ["SKU-C"])

    def test_an_active_combo_still_ships(self):
        self._combo_mapping(active=True)
        r = self._resolve(self._line(marketplace_sku="SKU-C"))
        self.assertEqual(r["resolved_lines"][0]["item_code"], "FG-KIT")
        self.assertEqual(r["resolved_lines"][0]["required_quantity"], Decimal("2"))


class NamesDoNotLie(MappingIntegrityBase):
    """Defect 5 — the marketplace's product title stood in for the SAP name."""

    def test_a_missing_sap_name_stays_empty(self):
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-N",
            sku_type=SkuType.RAW, fg_item_code="FG-N", fg_item_name="")
        line = self._line(
            marketplace_sku="SKU-N",
            sku_name="Jivo Cold Pressed Groundnut Oil 5L Combo (Flipkart title)")
        r = self._resolve(line)
        # Blank, not the marketplace title: this value is frozen into the posted
        # snapshot and printed under "SAP Item Name".
        self.assertEqual(r["resolved_lines"][0]["item_name"], "")

    def test_the_sap_name_is_used_when_there_is_one(self):
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-M",
            sku_type=SkuType.RAW, fg_item_code="FG-M", fg_item_name="COLD PRESS 1 LTR 20 PCS")
        r = self._resolve(self._line(marketplace_sku="SKU-M", sku_name="Flipkart title"))
        self.assertEqual(r["resolved_lines"][0]["item_name"], "COLD PRESS 1 LTR 20 PCS")


class ExactlyOneDefault(MappingIntegrityBase):
    """Defect 6 — nothing but the serializer enforced a single default."""

    def test_a_second_mapping_option_default_is_rejected(self):
        m = SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-D",
            sku_type=SkuType.RAW, fg_item_code="FG-OWN")
        SkuMappingOption.objects.create(mapping=m, fg_item_code="FG-1", is_default=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SkuMappingOption.objects.create(mapping=m, fg_item_code="FG-2", is_default=True)

    def test_a_second_component_option_default_is_rejected(self):
        combo = ComboDefinition.objects.create(
            company=self.company, channel=self.channel, code="CB-D", name="Kit")
        comp = ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG, item_code="FG-S",
            quantity=Decimal("1"))
        ComboComponentOption.objects.create(component=comp, item_code="FG-1", is_default=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ComboComponentOption.objects.create(component=comp, item_code="FG-2", is_default=True)

    def test_non_defaults_are_unrestricted(self):
        m = SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-ND",
            sku_type=SkuType.RAW, fg_item_code="FG-OWN")
        SkuMappingOption.objects.create(mapping=m, fg_item_code="FG-1", is_default=True)
        SkuMappingOption.objects.create(mapping=m, fg_item_code="FG-2")
        SkuMappingOption.objects.create(mapping=m, fg_item_code="FG-3")
        self.assertEqual(m.options.count(), 3)


class PicksSurviveAnEdit(MappingIntegrityBase):
    """Defect 2 — saving options deleted and recreated them, nulling every pick."""

    def _mapping_with_options(self):
        m = SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-P",
            fsn="FSNP", sku_type=SkuType.RAW, fg_item_code="FG1", fg_item_name="One")
        default = SkuMappingOption.objects.create(
            mapping=m, label="default", fg_item_code="FG1", fg_item_name="One",
            is_default=True)
        alt = SkuMappingOption.objects.create(
            mapping=m, label="alt", fg_item_code="FG2", fg_item_name="Two")
        return m, default, alt

    def test_renaming_a_label_keeps_the_operators_pick(self):
        from .serializers import SkuMappingSerializer

        m, default, alt = self._mapping_with_options()
        line = self._line(marketplace_sku="SKU-P", fsn="FSNP", chosen_option=alt)

        ser = SkuMappingSerializer(instance=m, data={"options": [
            {"id": default.id, "label": "default RENAMED", "fg_item_code": "FG1",
             "fg_item_name": "One", "is_default": True},
            {"id": alt.id, "label": "alt", "fg_item_code": "FG2", "fg_item_name": "Two"},
        ]}, partial=True, context={})
        ser.is_valid(raise_exception=True)
        ser.save()

        line.refresh_from_db()
        self.assertEqual(line.chosen_option_id, alt.id)
        r = self._resolve(line)
        self.assertEqual([x["item_code"] for x in r["resolved_lines"]], ["FG2"])

    def test_an_option_the_payload_omits_is_still_deleted(self):
        from .serializers import SkuMappingSerializer

        m, default, alt = self._mapping_with_options()
        ser = SkuMappingSerializer(instance=m, data={"options": [
            {"id": default.id, "label": "default", "fg_item_code": "FG1",
             "fg_item_name": "One", "is_default": True},
        ]}, partial=True, context={})
        ser.is_valid(raise_exception=True)
        ser.save()
        self.assertEqual([o.id for o in m.options.all()], [default.id])

    def test_a_new_option_is_created(self):
        from .serializers import SkuMappingSerializer

        m, default, alt = self._mapping_with_options()
        ser = SkuMappingSerializer(instance=m, data={"options": [
            {"id": default.id, "label": "default", "fg_item_code": "FG1",
             "fg_item_name": "One", "is_default": True},
            {"id": alt.id, "label": "alt", "fg_item_code": "FG2", "fg_item_name": "Two"},
            {"label": "third", "fg_item_code": "FG3", "fg_item_name": "Three"},
        ]}, partial=True, context={})
        ser.is_valid(raise_exception=True)
        ser.save()
        self.assertEqual(m.options.count(), 3)
        self.assertEqual(m.options.filter(is_default=True).count(), 1)


class IndexHelpers(MappingIntegrityBase):

    def test_for_line_resolves_without_a_query(self):
        m = SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-Q",
            fsn="FSNQ", sku_type=SkuType.RAW, fg_item_code="FG-Q")
        line = self._line(marketplace_sku="SKU-Q", fsn="FSNQ")
        index = MappingIndex.for_line(line, m)
        with self.assertNumQueries(0):
            self.assertIs(index.lookup(line.fsn, line.marketplace_sku), m)

    def test_membership_still_answers_for_either_key(self):
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku="SKU-R",
            fsn="FSNR", sku_type=SkuType.RAW, fg_item_code="FG-R")
        index = load_mappings(self.company, self.channel)
        self.assertIn("FSNR", index)
        self.assertIn("sku-r", index)
        self.assertNotIn("NOPE", index)
