"""Separate the HOD (Labour module) role from the gate person (Labour Gate).

Previously the `labour` group (the /labour department-allocation screen used by
HODs) recorded the split with `can_record_labour_in` — the very same permission
the gate person uses to record raw gate-in. That coupled two distinct roles.

This migration:
  1. Ensures the new `labour_gate.can_allocate_labour_department` permission
     exists (the post_migrate auto-creation runs only after this migration, so we
     create it explicitly here to use it in the same run).
  2. Re-points the `labour` (HOD) group at the allocation permission and drops
     `can_record_labour_in`, giving clean separation of concerns:
        labour      (HOD)         -> view + allocate + view/add contractor
        labour gate (gate person) -> view + record_in + record_out (+ contractor)
"""

from django.db import migrations

HOD_GROUP = "labour"

ALLOCATE = ("labour_gate", "can_allocate_labour_department", "Can allocate labour to department")

# Final permission set for the HOD group after the split.
HOD_PERMISSIONS = [
    ("labour_gate", "view_labourgateentry"),
    ("labour_gate", "can_allocate_labour_department"),
    ("person_gatein", "view_contractor"),
    ("person_gatein", "add_contractor"),
]


def _ensure_allocate_permission(apps):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    app_label, codename, name = ALLOCATE
    ct, _ = ContentType.objects.get_or_create(
        app_label=app_label, model="labourgateentry"
    )
    perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename=codename, defaults={"name": name}
    )
    return perm


def apply_split(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    _ensure_allocate_permission(apps)

    group, _ = Group.objects.get_or_create(name=HOD_GROUP)
    perms = []
    for app_label, codename in HOD_PERMISSIONS:
        perm = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if perm:
            perms.append(perm)
    group.permissions.set(perms)


def revert_split(apps, schema_editor):
    """Restore the pre-split HOD group (record_in instead of allocate)."""
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group = Group.objects.filter(name=HOD_GROUP).first()
    if not group:
        return
    old = [
        ("labour_gate", "view_labourgateentry"),
        ("labour_gate", "can_record_labour_in"),
        ("person_gatein", "view_contractor"),
        ("person_gatein", "add_contractor"),
    ]
    perms = []
    for app_label, codename in old:
        perm = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if perm:
            perms.append(perm)
    group.permissions.set(perms)


class Migration(migrations.Migration):

    dependencies = [
        ("labour_gate", "0006_alter_labourgateentry_options"),
        ("person_gatein", "0007_entrylog_actual_entry_time"),
    ]

    operations = [
        migrations.RunPython(apply_split, revert_split),
    ]
