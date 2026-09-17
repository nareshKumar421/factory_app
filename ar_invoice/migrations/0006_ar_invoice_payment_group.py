"""Wire the 'record payment received' permission into auth groups.

Two grants, deliberately:

* A new "AR Invoice Payments" group carrying view + mark, for accounts staff who
  chase receipts but must not be able to raise invoices.
* The mark permission added to the existing "AR Invoices" group, because the
  counter takes the cash for the cash sale it just raised — without this the
  feature would ship dark until an admin wired a group by hand. Revoke it there
  if the two jobs are meant to stay apart.

Permissions are created explicitly (content type + codename) so this works on a
fresh ``migrate`` before the auth post_migrate signal has created them, the same
way ``0002_ar_invoice_group`` does.
"""
from django.db import migrations

MARK_PERM = ("mark_ar_invoice_payment", "Can record whether an A/R invoice has been paid")
VIEW_PERM = ("view_ar_invoice_posting", "Can view A/R invoice postings")

PAYMENTS_GROUP = "AR Invoice Payments"
INVOICES_GROUP = "AR Invoices"


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    payment_ct, _ = ContentType.objects.get_or_create(
        app_label="ar_invoice", model="arinvoicepayment"
    )
    posting_ct, _ = ContentType.objects.get_or_create(
        app_label="ar_invoice", model="arinvoiceposting"
    )

    mark_perm, _ = Permission.objects.get_or_create(
        content_type=payment_ct, codename=MARK_PERM[0], defaults={"name": MARK_PERM[1]}
    )
    view_perm, _ = Permission.objects.get_or_create(
        content_type=posting_ct, codename=VIEW_PERM[0], defaults={"name": VIEW_PERM[1]}
    )

    payments_group, _ = Group.objects.get_or_create(name=PAYMENTS_GROUP)
    payments_group.permissions.add(mark_perm, view_perm)

    invoices_group = Group.objects.filter(name=INVOICES_GROUP).first()
    if invoices_group is not None:
        invoices_group.permissions.add(mark_perm)


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    Group.objects.filter(name=PAYMENTS_GROUP).delete()
    Permission.objects.filter(
        content_type__app_label="ar_invoice", codename=MARK_PERM[0]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ar_invoice", "0005_ar_invoice_payment"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(create_group, remove_group)]
