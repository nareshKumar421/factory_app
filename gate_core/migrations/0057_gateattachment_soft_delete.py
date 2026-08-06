import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("gate_core", "0056_partial_delivery_item_wise"),
    ]

    operations = [
        migrations.AddField(
            model_name="gateattachment",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="gateattachment",
            name="uploaded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="gate_attachments_uploaded",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="gateattachment",
            name="removed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="gateattachment",
            name="removed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="gate_attachments_removed",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="gateattachment",
            name="remove_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AlterModelOptions(
            name="gateattachment",
            options={"ordering": ["uploaded_at", "id"]},
        ),
    ]
