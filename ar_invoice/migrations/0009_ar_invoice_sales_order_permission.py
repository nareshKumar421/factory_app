"""Gate billing against Sales Orders behind its own permission.

The new "AR Invoices from Sales Orders" group carries view + create + the new
permission, for whoever is to raise invoices from Sales Orders. It is NOT added
to the existing "AR Invoices" group: that one is the counter's, and handing it
the new permission would ungate the option for everyone who raises cash sales.

The permission is created explicitly (content type + codename) so this works on
a fresh ``migrate`` before the auth post_migrate signal has created it, the same
way ``0002_ar_invoice_group`` does.
"""
from django.db import migrations

SO_PERM = ("create_ar_invoice_from_sales_order", "Can raise A/R invoices from Sales Orders")
VIEW_PERM = ("view_ar_invoice_posting", "Can view A/R invoice postings")
CREATE_PERM = ("create_ar_invoice_posting", "Can create and post A/R invoices")

GROUP = "AR Invoices from Sales Orders"


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    posting_ct, _ = ContentType.objects.get_or_create(
        app_label="ar_invoice", model="arinvoiceposting"
    )
    perms = [
        Permission.objects.get_or_create(
            content_type=posting_ct, codename=codename, defaults={"name": name}
        )[0]
        for codename, name in (VIEW_PERM, CREATE_PERM, SO_PERM)
    ]
    group, _ = Group.objects.get_or_create(name=GROUP)
    group.permissions.add(*perms)


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    Group.objects.filter(name=GROUP).delete()
    Permission.objects.filter(
        content_type__app_label="ar_invoice", codename=SO_PERM[0]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ar_invoice", "0008_ar_invoice_warehouse_approval"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="arinvoiceposting",
            options={
                "default_permissions": (),
                "ordering": ["-created_at"],
                "permissions": [
                    ("view_ar_invoice_posting", "Can view A/R invoice postings"),
                    ("create_ar_invoice_posting", "Can create and post A/R invoices"),
                    SO_PERM,
                ],
            },
        ),
        migrations.RunPython(create_group, remove_group),
    ]
