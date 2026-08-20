"""Delete every SKU mapping and combo, so the masters can be rebuilt from scratch.

Deletion order is not a preference, it is enforced by the database: ``SkuMapping``
and ``SkuMappingOption`` reference ``ComboDefinition`` with ``on_delete=PROTECT``,
so the mappings must go first. Their options cascade with them; a combo's
components and component-options cascade with the combo.

What else this touches, none of it obvious from the Masters screen:

  * every dispatch not yet posted stops resolving, so packing, issue requests,
    confirm and the delivery-note cut all refuse with UNMAPPED_SKUS until the new
    mappings cover the same FSNs. That is the safe failure, but it IS a stop;
  * ``MarketplaceOrderLine.chosen_option`` is SET_NULL, so operators' variant picks
    are cleared;
  * ``component_choices`` holds plain combo-component ids with no foreign key.
    New components get new ids, so those picks silently fall back to a default;
  * a delivery note with no ``sap_posted_lines`` snapshot is re-derived from the
    mappings AT EXPORT TIME. Deleting them empties the per-order item columns of
    every such note, permanently -- re-creating mappings does not restore what the
    old ones said.

So a backup is required, not advised. Take one with::

    python manage.py dumpdata marketplace.SkuMapping marketplace.SkuMappingOption \\
        marketplace.ComboDefinition marketplace.ComboComponent \\
        marketplace.ComboComponentOption --indent 2 -o masters.json

and export every delivery note first, because that history cannot be rebuilt.

Dry run by default::

    python manage.py mp_delete_mappings                      # report only
    python manage.py mp_delete_mappings --backup masters.json --apply
"""
import json
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from marketplace.models import (
    ComboComponent,
    ComboComponentOption,
    ComboDefinition,
    MarketplaceDispatch,
    MarketplaceOrderLine,
    MarketplaceSapPostStatus,
    SkuMapping,
    SkuMappingOption,
)

MODELS = (SkuMapping, SkuMappingOption, ComboDefinition, ComboComponent, ComboComponentOption)


class Command(BaseCommand):
    help = "Delete all marketplace SKU mappings and combos (requires a backup)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually delete. Without it, nothing is written.")
        parser.add_argument("--backup", default="",
                            help="Path to the dumpdata JSON taken beforehand.")

    def handle(self, *args, **opts):
        counts = {m.__name__: m.objects.count() for m in MODELS}
        picks = MarketplaceOrderLine.objects.filter(chosen_option__isnull=False).count()
        slots = sum(1 for l in MarketplaceOrderLine.objects
                    .exclude(component_choices={}).only("component_choices")
                    if l.component_choices)
        open_d = MarketplaceDispatch.objects.exclude(
            sap_post_status=MarketplaceSapPostStatus.POSTED).count()
        posted = MarketplaceDispatch.objects.filter(
            sap_post_status=MarketplaceSapPostStatus.POSTED)
        unsnapshotted = sum(1 for d in posted.only("sap_posted_lines") if not d.sap_posted_lines)

        self.stdout.write("masters to delete")
        for name, n in counts.items():
            self.stdout.write(f"    {name:<22}{n:>6}")
        self.stdout.write("\nwhat this also does")
        self.stdout.write(f"    dispatches that stop resolving   {open_d:>6}")
        self.stdout.write(f"    variant picks cleared            {picks:>6}")
        self.stdout.write(f"    combo-slot picks left stale      {slots:>6}")
        self.stdout.write(self.style.WARNING(
            f"    posted notes that lose their per-order items   {unsnapshotted:>6}"))

        if not opts["apply"]:
            self.stdout.write(self.style.SUCCESS(
                "\nDry run — nothing deleted. Re-run with --backup <file> --apply."))
            return

        self._check_backup(opts["backup"], counts)

        with transaction.atomic():
            # Mappings before combos: PROTECT refuses the other order.
            SkuMappingOption.objects.all().delete()
            SkuMapping.objects.all().delete()
            ComboDefinition.objects.all().delete()

        left = {m.__name__: m.objects.count() for m in MODELS}
        for name, n in left.items():
            style = self.style.SUCCESS if n == 0 else self.style.ERROR
            self.stdout.write(style(f"    {name:<22}{n:>6} remaining"))
        self.stdout.write(self.style.SUCCESS("\nDeleted. Rebuild the masters before cutting anything."))

    def _check_backup(self, path, counts):
        """Refuse to run without a backup that actually holds these rows."""
        if not path:
            raise CommandError("--backup is required with --apply. Take a dumpdata first.")
        if not os.path.exists(path):
            raise CommandError(f"Backup {path!r} does not exist.")
        try:
            with open(path) as fh:
                rows = json.load(fh)
        except Exception as e:
            raise CommandError(f"Backup {path!r} is not readable JSON: {e}")

        found = {}
        for row in rows:
            found[row.get("model", "")] = found.get(row.get("model", ""), 0) + 1
        for model in MODELS:
            key = f"marketplace.{model.__name__.lower()}"
            if found.get(key, 0) < counts[model.__name__]:
                raise CommandError(
                    f"Backup holds {found.get(key, 0)} {key} rows but the database has "
                    f"{counts[model.__name__]}. Take a fresh dump before deleting.")
        self.stdout.write(f"\nbackup verified: {len(rows)} rows in {path}")
