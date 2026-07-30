"""Replace a wrong/invalid finished-good item code everywhere in the masters.

Repoints every reference to ``--old`` → ``--new`` across RAW SKU mappings, ship-as
variants, and combo components (looking up the new item's name from existing
components). Use it to fix a mapping that points at a placeholder / typo / retired
code so its orders resolve and post a delivery note.

Idempotent. Dry-run by default; pass ``--apply`` to write.

    python manage.py mp_repoint_item_code --old "New_Extra Light 5L-Local" --new FG0000009
    python manage.py mp_repoint_item_code --old "New_Extra Light 5L-Local" --new FG0000009 --apply
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from company.models import Company
from marketplace.models import (
    ComboComponent, MarketplaceChannel, SkuMapping, SkuMappingOption, SkuType,
)


class Command(BaseCommand):
    help = "Replace a finished-good item code everywhere in the marketplace masters."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=getattr(settings, "MARKETPLACE_COMPANY_CODE", "JIVO_MART"))
        parser.add_argument("--channel", default="FLIPKART")
        parser.add_argument("--old", required=True, help="the item code to replace")
        parser.add_argument("--new", required=True, help="the correct item code")
        parser.add_argument("--apply", action="store_true", help="Write changes (otherwise dry run).")

    def handle(self, *args, **opts):
        try:
            company = Company.objects.get(code=opts["company"])
        except Company.DoesNotExist:
            raise CommandError(f"No company with code {opts['company']!r}")
        channel, old, new = opts["channel"], opts["old"].strip(), opts["new"].strip()
        if not old or not new:
            raise CommandError("--old and --new are required and non-empty.")

        # Name for the new code (best-effort, from existing combo components).
        new_name = ""
        ex = ComboComponent.objects.filter(
            combo__company=company, combo__channel=channel, item_code=new,
        ).exclude(item_name="").first()
        if ex:
            new_name = ex.item_name

        raw = SkuMapping.objects.filter(
            company=company, channel=channel, sku_type=SkuType.RAW, fg_item_code=old)
        opts_qs = SkuMappingOption.objects.filter(
            mapping__company=company, mapping__channel=channel, fg_item_code=old)
        comps = ComboComponent.objects.filter(
            combo__company=company, combo__channel=channel, item_code=old)

        self.stdout.write(f"Repoint {old!r} -> {new!r} ({new_name or 'name unknown'}):")
        self.stdout.write(f"  RAW mappings   : {raw.count()}  {[m.marketplace_sku for m in raw][:8]}")
        self.stdout.write(f"  ship-as options: {opts_qs.count()}")
        self.stdout.write(f"  combo components: {comps.count()}  {[x.combo.code for x in comps][:8]}")

        if opts["apply"]:
            with transaction.atomic():
                raw.update(fg_item_code=new)
                opts_qs.update(fg_item_code=new)
                for x in comps:
                    x.item_code = new
                    if new_name:
                        x.item_name = new_name
                    x.save(update_fields=["item_code", "item_name"])
            self.stdout.write(self.style.SUCCESS(f"\nWROTE: repointed {old!r} -> {new!r}."))
        else:
            self.stdout.write("\nDRY RUN — re-run with --apply to commit.")
