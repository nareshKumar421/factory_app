"""The "Fixed Asset GRPO" group, with this app's three rights.

* ``can_view_fixed_asset_grpo`` -- sees the Fixed Asset GRPO lists, entries,
  previews and history.
* ``can_post_fixed_asset_grpo`` -- posts (and simulates) the GRPO, closes an
  entry as done in SAP by hand, discards drafts and simulated posts, and may
  also link or change the PO.
* ``can_link_fixed_asset_po`` -- links, changes or removes the SAP PO on a
  completed fixed-asset gate entry (the "late link"), without posting.

The group is created holding all three and nobody in it: who gets it is an
open question for accounts (spec OPEN-3). Give a person only the link right
by adding it to them, or to another group, directly.

On a fresh database the post_migrate hook that creates model permissions has
not run yet, so they are created explicitly for this app first (the
``stock_audit/0002`` pattern). Reverse takes the three rights back out and
deletes the group only if that leaves it empty, so rights someone added by
hand are not thrown away with it.
"""
from django.db import migrations

GROUP_NAME = "Fixed Asset GRPO"
CODENAMES = [
    "can_view_fixed_asset_grpo",
    "can_post_fixed_asset_grpo",
    "can_link_fixed_asset_po",
]


def create_group(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    create_permissions(global_apps.get_app_config("fixed_asset_grpo"), verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.add(
        *Permission.objects.filter(
            content_type__app_label="fixed_asset_grpo", codename__in=CODENAMES
        )
    )


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group = Group.objects.filter(name=GROUP_NAME).first()
    if group is None:
        return
    group.permissions.remove(
        *Permission.objects.filter(
            content_type__app_label="fixed_asset_grpo", codename__in=CODENAMES
        )
    )
    if not group.permissions.exists() and not group.user_set.exists():
        group.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("fixed_asset_grpo", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(create_group, remove_group),
    ]
