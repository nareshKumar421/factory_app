"""The support desk's phone number becomes a row instead of a constant.

Seeded with the number the frontend was shipping hardcoded, so the screens
read the same thing the day this migration lands and every change after that
is an admin edit rather than a deploy.
"""


import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


#: What the frontend had compiled in before this migration existed.
INITIAL_PHONE = "+91 9218179324"


def seed_support_contact(apps, schema_editor):
    SupportContact = apps.get_model("issues", "SupportContact")
    SupportContact.objects.update_or_create(pk=1, defaults={"phone": INITIAL_PHONE})


class Migration(migrations.Migration):

    dependencies = [
        ('issues', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SupportContact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('phone', models.CharField(blank=True, default='', help_text="Shown to users exactly as typed, e.g. '+91 9218179324'. Leave blank to hide the support number everywhere.", max_length=32)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Support Contact',
                'verbose_name_plural': 'Support Contact',
            },
        ),
        migrations.RunPython(seed_support_contact, migrations.RunPython.noop),
    ]
