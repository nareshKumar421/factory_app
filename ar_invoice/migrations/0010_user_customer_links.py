"""Link users to the SAP customers they are, for the Ledger tab.

A user sees the ledgers of the customers linked to them (``UserCustomer``);
the new ``view_all_customer_ledgers`` right opens every customer's. The
"Customer Ledgers — all customers" group carries view + that right, for
accounts and billing staff. It is NOT added to the existing "AR Invoices"
group: that is the counter's, and the counter is who the links restrict.

The permission is created explicitly, as in ``0009``, so the group gets it on
a fresh ``migrate`` before the auth post_migrate signal has run.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

ALL_LEDGERS_PERM = ("view_all_customer_ledgers", "Can view every customer's ledger")
VIEW_PERM = ("view_ar_invoice_posting", "Can view A/R invoice postings")

GROUP = "Customer Ledgers — all customers"


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
        for codename, name in (VIEW_PERM, ALL_LEDGERS_PERM)
    ]
    group, _ = Group.objects.get_or_create(name=GROUP)
    group.permissions.add(*perms)


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    Group.objects.filter(name=GROUP).delete()
    Permission.objects.filter(
        content_type__app_label="ar_invoice", codename=ALL_LEDGERS_PERM[0]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ar_invoice', '0009_ar_invoice_sales_order_permission'),
        ('company', '0003_alter_usercompany_role'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='arinvoiceposting',
            options={'default_permissions': (), 'ordering': ['-created_at'], 'permissions': [('view_ar_invoice_posting', 'Can view A/R invoice postings'), ('create_ar_invoice_posting', 'Can create and post A/R invoices'), ('create_ar_invoice_from_sales_order', 'Can raise A/R invoices from Sales Orders'), ('view_all_customer_ledgers', "Can view every customer's ledger")]},
        ),
        migrations.CreateModel(
            name='UserCustomer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('customer_code', models.CharField(max_length=50)),
                ('customer_name', models.CharField(blank=True, default='', max_length=200)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='user_customer_links', to='company.company')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sap_customer_links', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'customer ledger link',
                'verbose_name_plural': 'customer ledger links',
                'db_table': 'ar_invoice_user_customer',
                'ordering': ['company', 'customer_name', 'customer_code'],
                'indexes': [models.Index(fields=['user', 'company'], name='ar_invoice__user_id_033747_idx'), models.Index(fields=['company', 'customer_code'], name='ar_invoice__company_a00045_idx')],
                'constraints': [models.UniqueConstraint(fields=('user', 'company', 'customer_code'), name='uniq_user_company_customer')],
            },
        ),
        migrations.RunPython(create_group, remove_group),
    ]
