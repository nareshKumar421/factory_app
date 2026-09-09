"""Hand the A/R Return already posted to the invoice it was posted for.

The returns booked before the split posted one combined document and recorded it
on the header. A single-invoice return *is* that invoice's return, so its document
moves down onto the invoice ref where every reader now looks for it.

A return that carried several invoices is left alone: one document cannot be
attributed to two bills, and claiming it for either would be a lie. Those keep
showing the header's document, which is what the detail serializer falls back to.
Their status is already POSTED, so nothing offers to post them again.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    GoodsReturn = apps.get_model("goods_return", "GoodsReturn")

    returns = GoodsReturn.objects.filter(
        sap_gr_doc_entry__isnull=False
    ).prefetch_related("invoice_refs")

    for gr in returns:
        refs = [ref for ref in gr.invoice_refs.all() if ref.is_active]
        if len(refs) != 1:
            continue
        ref = refs[0]
        if ref.sap_gr_doc_entry is not None:
            continue
        ref.sap_gr_doc_entry = gr.sap_gr_doc_entry
        ref.sap_gr_doc_num = gr.sap_gr_doc_num
        ref.sap_return_warehouse = gr.sap_return_warehouse
        ref.posted_at = gr.received_at
        ref.save(
            update_fields=[
                "sap_gr_doc_entry",
                "sap_gr_doc_num",
                "sap_return_warehouse",
                "posted_at",
            ]
        )


def unbackfill(apps, schema_editor):
    """Reversible: the header still holds every document this copied down."""
    GoodsReturnInvoiceRef = apps.get_model("goods_return", "GoodsReturnInvoiceRef")
    GoodsReturnInvoiceRef.objects.update(
        sap_gr_doc_entry=None,
        sap_gr_doc_num="",
        sap_return_warehouse="",
        posted_at=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("goods_return", "0010_invoice_level_sap_returns"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
