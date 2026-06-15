from django.db import migrations


def backfill_box_scan_weights(apps, schema_editor):
    """Copy net/gross weight from the linked Box onto existing box scans."""
    SalesDispatchBoxScan = apps.get_model("gate_core", "SalesDispatchBoxScan")

    scans = SalesDispatchBoxScan.objects.filter(box__isnull=False).select_related("box")
    to_update = []
    for scan in scans.iterator():
        box = scan.box
        changed = False
        if scan.net_weight is None and box.n_weight is not None:
            scan.net_weight = box.n_weight
            changed = True
        if scan.gross_weight is None and box.g_weight is not None:
            scan.gross_weight = box.g_weight
            changed = True
        if changed:
            to_update.append(scan)

    if to_update:
        SalesDispatchBoxScan.objects.bulk_update(to_update, ["net_weight", "gross_weight"])


def reverse(apps, schema_editor):
    # Non-destructive: leave the backfilled values in place.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0030_salesdispatchboxscan_gross_weight_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_box_scan_weights, reverse),
    ]
