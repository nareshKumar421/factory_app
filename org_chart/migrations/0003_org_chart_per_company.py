"""One ownership chart per company.

The chart started life global, which was only ever right while Oil was the one
plant on it. Mart and Beverages are different plants with different people, so
every block and every heading now hangs off a company.

Whatever is already in the table is the Oil plant's chart — that is the chart
that was seeded and edited — so the backfill hands it to ``JIVO_OIL``. Mart and
Beverages start empty and get built on the page.
"""

import django.db.models.constraints
import django.db.models.deletion
from django.db import migrations, models

#: The company the pre-existing (global) chart actually described.
LEGACY_OWNER_CODE = "JIVO_OIL"


def _legacy_owner(apps):
    """The company the existing rows belong to, or None on an empty install."""
    Company = apps.get_model("company", "Company")
    return (
        Company.objects.filter(code=LEGACY_OWNER_CODE).first()
        or Company.objects.order_by("id").first()
    )


def attach_existing_chart_to_oil(apps, schema_editor):
    OrgDepartment = apps.get_model("org_chart", "OrgDepartment")
    OrgChartSettings = apps.get_model("org_chart", "OrgChartSettings")

    owner = _legacy_owner(apps)
    if owner is None:
        # A fresh install with no companies yet: there is nothing to hand over,
        # and anything orphaned here would block the NOT NULL below.
        OrgDepartment.objects.all().delete()
        OrgChartSettings.objects.all().delete()
        return

    OrgDepartment.objects.filter(company__isnull=True).update(company=owner)

    # The heading was a singleton; at most one row can become the owner's, and
    # any extra (there should be none) is dropped rather than left dangling.
    kept = OrgChartSettings.objects.filter(company__isnull=True).order_by("pk").first()
    if kept is not None:
        kept.company = owner
        kept.save(update_fields=["company"])
    OrgChartSettings.objects.filter(company__isnull=True).delete()


def unattach(apps, schema_editor):
    """Reverse: the chart goes back to being global, keeping Oil's rows only."""
    OrgDepartment = apps.get_model("org_chart", "OrgDepartment")
    OrgChartSettings = apps.get_model("org_chart", "OrgChartSettings")

    owner = _legacy_owner(apps)
    if owner is None:
        return
    OrgDepartment.objects.exclude(company=owner).delete()
    OrgChartSettings.objects.exclude(company=owner).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0003_alter_usercompany_role"),
        ("org_chart", "0002_orgchartsettings_and_more"),
    ]

    operations = [
        # The old constraint spans the whole table; it has to go before two
        # companies can each own a "Production".
        migrations.RemoveConstraint(
            model_name="orgdepartment",
            name="uniq_org_department_name",
        ),
        migrations.AddField(
            model_name="orgdepartment",
            name="company",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="org_departments",
                to="company.company",
            ),
        ),
        migrations.AddField(
            model_name="orgchartsettings",
            name="company",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="org_chart_settings",
                to="company.company",
            ),
        ),
        migrations.RunPython(attach_existing_chart_to_oil, unattach),
        migrations.AlterField(
            model_name="orgdepartment",
            name="company",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="org_departments",
                to="company.company",
            ),
        ),
        migrations.AlterField(
            model_name="orgchartsettings",
            name="company",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="org_chart_settings",
                to="company.company",
            ),
        ),
        migrations.AlterField(
            model_name="orgchartsettings",
            name="plant_name",
            field=models.CharField(
                help_text="Title at the top of the chart, e.g. 'Oil Plant'.",
                max_length=120,
            ),
        ),
        migrations.AlterModelOptions(
            name="orgchartsettings",
            options={
                "verbose_name": "org chart heading",
                "verbose_name_plural": "org chart headings",
            },
        ),
        migrations.AddConstraint(
            model_name="orgdepartment",
            constraint=models.UniqueConstraint(
                deferrable=django.db.models.constraints.Deferrable["DEFERRED"],
                fields=("company", "name"),
                name="uniq_org_department_name",
            ),
        ),
    ]
