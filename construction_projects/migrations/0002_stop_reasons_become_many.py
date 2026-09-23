"""A day can be stopped for more than one reason at once.

`stopped_reason` was a single choice; it becomes a child table. The order of
operations matters: create the table, copy what is already recorded into it,
and only then drop the column. Django's autodetector wrote the RemoveField
first, which would have discarded every reason already logged.
"""

import django.db.models.deletion
from django.db import migrations, models


def carry_reasons_across(apps, schema_editor):
    DailyLog = apps.get_model("construction_projects", "DailyLog")
    StopReason = apps.get_model("construction_projects", "DailyLogStopReason")
    StopReason.objects.bulk_create(
        [
            StopReason(daily_log_id=log_id, reason=reason)
            for log_id, reason in DailyLog.objects.exclude(stopped_reason="")
            .exclude(stopped_reason=None)
            .values_list("id", "stopped_reason")
        ],
        ignore_conflicts=True,
    )


def put_reasons_back(apps, schema_editor):
    """One reason per log on the way down -- the column can only hold one, so a
    day stopped for two keeps the first alphabetically and the other is lost.
    That is inherent in reversing this, not an oversight."""
    DailyLog = apps.get_model("construction_projects", "DailyLog")
    StopReason = apps.get_model("construction_projects", "DailyLogStopReason")
    for log_id, reason in StopReason.objects.order_by(
        "daily_log_id", "reason"
    ).values_list("daily_log_id", "reason"):
        DailyLog.objects.filter(id=log_id, stopped_reason="").update(
            stopped_reason=reason
        )


class Migration(migrations.Migration):

    dependencies = [
        ("construction_projects", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DailyLogStopReason",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("RAIN", "Rain"),
                            ("NO_MATERIAL", "Material not available"),
                            ("NO_LABOUR", "Labour not available"),
                            ("NO_POWER", "No power"),
                            ("HOLIDAY", "Holiday"),
                            ("APPROVAL_PENDING", "Waiting on an approval"),
                            ("SAFETY", "Safety"),
                            ("OTHER", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "daily_log",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stop_reasons",
                        to="construction_projects.dailylog",
                    ),
                ),
            ],
            options={
                "verbose_name": "Daily Log Stop Reason",
                "verbose_name_plural": "Daily Log Stop Reasons",
                "ordering": ["reason"],
                "default_permissions": (),
                "unique_together": {("daily_log", "reason")},
            },
        ),
        migrations.RunPython(carry_reasons_across, put_reasons_back),
        migrations.RemoveField(
            model_name="dailylog",
            name="stopped_reason",
        ),
    ]
