"""The customer moves onto the invoice it was billed to.

A return is one truckload, and a truck coming back off a market run carries the
bills of several distributors. The customer therefore belongs to the bill, not
to the return -- which it already did in SAP, since every invoice posts its own
A/R Return under its own ``CardCode``.

Existing refs are backfilled from their header: every return booked until now
was forced to a single customer, so the header's is theirs by construction.
"""

from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def backfill_customer(apps, schema_editor):
    GoodsReturn = apps.get_model("goods_return", "GoodsReturn")
    GoodsReturnInvoiceRef = apps.get_model("goods_return", "GoodsReturnInvoiceRef")
    header = GoodsReturn.objects.filter(pk=OuterRef("goods_return_id"))
    GoodsReturnInvoiceRef.objects.filter(customer_code="").update(
        customer_code=Subquery(header.values("customer_code")[:1]),
        customer_name=Subquery(header.values("customer_name")[:1]),
    )


def unbackfill(apps, schema_editor):
    # Nothing to undo: the columns go with the reverse of the AddFields.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("goods_return", "0014_alter_goodsreturnitem_condition"),
    ]

    operations = [
        migrations.AddField(
            model_name="goodsreturninvoiceref",
            name="customer_code",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="goodsreturninvoiceref",
            name="customer_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.RunPython(backfill_customer, unbackfill),
    ]
