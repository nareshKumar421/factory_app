"""Give the BSTs that already shipped a loaded-at stamp.

`loaded_at` is written when a gated transfer is sealed, so without this every BST
booked before the field existed would read "—" on the dashboards forever. The
seal (`scan_approved_at`) is exactly the moment the new code stamps, so that is
what we copy.

Only `requires_gate` transfers are touched. An internal move has no loading
handover to record — the warehouse team lifts the pallets across and the
receiving warehouse's team takes them off, with no dispatch team and no gate in
between — so those stay null on purpose.
"""

from django.db import migrations

# A gated transfer that reached one of these is over: whatever loading it had is
# done, so a dispatched_at fallback is safe to read as the loading stamp. (For a
# gated transfer dispatched_at is the gate-out, which is the closest stamp there
# is when the warehouse never pressed approve.)
SETTLED = ("RECEIVED", "PARTIALLY_RECEIVED", "CLOSED")


def backfill(apps, schema_editor):
    BSTTransfer = apps.get_model("warehouse", "BSTTransfer")

    sealed = BSTTransfer.objects.filter(
        requires_gate=True, loaded_at__isnull=True, scan_approved_at__isnull=False,
    )
    for transfer in sealed.iterator():
        transfer.loaded_at = transfer.scan_approved_at
        transfer.loaded_by_id = transfer.scan_approved_by_id
        transfer.save(update_fields=["loaded_at", "loaded_by"])

    unsealed = BSTTransfer.objects.filter(
        requires_gate=True, loaded_at__isnull=True,
        dispatched_at__isnull=False, status__in=SETTLED,
    )
    for transfer in unsealed.iterator():
        transfer.loaded_at = transfer.dispatched_at
        transfer.loaded_by_id = transfer.dispatched_by_id
        transfer.save(update_fields=["loaded_at", "loaded_by"])


def unbackfill(apps, schema_editor):
    # The column goes away with 0025; nothing to undo that 0025 won't drop.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("warehouse", "0025_bst_loaded_at"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
