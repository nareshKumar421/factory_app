"""Pin every existing dispatch to the sheet it was worked under.

``MarketplaceDispatch.import_batch`` is new, so historic rows have none. For all
but one shape the order never moved sheets and ``order.import_batch`` is exactly
right. The exception is a RE-MANIFESTED order (Flipkart re-lists it under a new
Tracking ID): it is pulled onto the newer sheet and gains a second dispatch, so
its already-shipped dispatch would be mis-attributed to that newer sheet — which
is what made a 14-order sheet show 15 gate rows.

For those, the older dispatch is pinned to the order's ORIGINAL sheet: the latest
batch of the same company/channel imported at or before the ORDER's ``created_at``
(an order row is written during its first sheet's ingest, so the two timestamps
are milliseconds apart).

Deliberately keyed on the order's creation, not the dispatch's: a dispatch is
often created hours after its sheet, by which time a LATER sheet may already have
been imported — keying on the dispatch would pick that unrelated sheet. The
approximation only degrades for an order re-manifested twice (3+ dispatches), where
the middle parcel would also land on the original sheet; no such order exists.
"""
from django.db import migrations
from django.db.models import Count, OuterRef, Subquery


def backfill(apps, schema_editor):
    MarketplaceDispatch = apps.get_model("marketplace", "MarketplaceDispatch")
    MarketplaceOrder = apps.get_model("marketplace", "MarketplaceOrder")
    OrderImportBatch = apps.get_model("marketplace", "OrderImportBatch")

    # Straightforward case: the dispatch inherits its order's sheet.
    MarketplaceDispatch.objects.filter(import_batch__isnull=True).update(
        import_batch_id=Subquery(
            MarketplaceOrder.objects.filter(pk=OuterRef("order_id")).values("import_batch_id")[:1]
        )
    )

    # Re-manifested orders: every dispatch EXCEPT the newest predates the move onto
    # the order's current sheet, so re-derive its sheet from when it was created.
    multi = (
        MarketplaceDispatch.objects.values("order_id")
        .annotate(n=Count("id")).filter(n__gt=1).values_list("order_id", flat=True)
    )
    for order_id in list(multi):
        ds = list(
            MarketplaceDispatch.objects.filter(order_id=order_id)
            .select_related("order").order_by("created_at", "id")
        )
        for d in ds[:-1]:  # all but the newest, which keeps the order's current sheet
            batch = (
                OrderImportBatch.objects.filter(
                    company_id=d.company_id, channel=d.channel,
                    created_at__lte=d.order.created_at,
                )
                .order_by("-created_at", "-id")
                .first()
            )
            if batch is not None:
                MarketplaceDispatch.objects.filter(pk=d.pk).update(import_batch_id=batch.id)


def unbackfill(apps, schema_editor):
    apps.get_model("marketplace", "MarketplaceDispatch").objects.update(import_batch=None)


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0030_marketplacedispatch_import_batch"),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
