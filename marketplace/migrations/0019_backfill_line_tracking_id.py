"""Backfill per-line tracking_id from each line's stored raw CSV row.

Multi-item orders were previously collapsed to a single order-level tracking ID;
each line's own tracking ID survives in ``raw_row['tracking']``, so restore it.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    MarketplaceOrderLine = apps.get_model("marketplace", "MarketplaceOrderLine")
    to_update = []
    qs = MarketplaceOrderLine.objects.filter(tracking_id="").only("id", "raw_row")
    for line in qs.iterator(chunk_size=1000):
        track = (line.raw_row or {}).get("tracking") if isinstance(line.raw_row, dict) else ""
        if track:
            line.tracking_id = str(track)[:120]
            to_update.append(line)
        if len(to_update) >= 1000:
            MarketplaceOrderLine.objects.bulk_update(to_update, ["tracking_id"])
            to_update = []
    if to_update:
        MarketplaceOrderLine.objects.bulk_update(to_update, ["tracking_id"])


class Migration(migrations.Migration):
    dependencies = [("marketplace", "0018_marketplaceorderline_tracking_id")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
