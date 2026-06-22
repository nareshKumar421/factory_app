from django.db import migrations

PERMS = [
    (
        "can_request_docking_partial_scan",
        "Can request to dispatch a docking with a partial box scan",
    ),
    ("can_view_docking_partial_scan", "Can view docking partial scan requests"),
    ("can_approve_docking_partial_scan", "Can approve or reject docking partial scan requests"),
]


def add_partial_scan_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    # Ensure the content type + permissions exist (post_migrate may not have created
    # them yet when this runs in the same migrate command), then grant to the groups
    # that already review / raise scan-skip requests.
    ct, _ = ContentType.objects.get_or_create(
        app_label="docking_admin", model="dockingpartialscanrequest"
    )
    perms = {}
    for codename, name in PERMS:
        perm, _ = Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={"name": name}
        )
        perms[codename] = perm

    approver, _ = Group.objects.get_or_create(name="Docking Approver")
    approver.permissions.add(
        perms["can_view_docking_partial_scan"],
        perms["can_approve_docking_partial_scan"],
    )

    operator, _ = Group.objects.get_or_create(name="Docking Scan Operator")
    operator.permissions.add(perms["can_request_docking_partial_scan"])


def remove_partial_scan_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    try:
        ct = ContentType.objects.get(
            app_label="docking_admin", model="dockingpartialscanrequest"
        )
    except ContentType.DoesNotExist:
        return
    perms = Permission.objects.filter(
        content_type=ct, codename__in=[codename for codename, _ in PERMS]
    )
    for group_name in ("Docking Approver", "Docking Scan Operator"):
        group = Group.objects.filter(name=group_name).first()
        if group:
            group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ("docking_admin", "0003_dockingpartialscanrequest"),
    ]

    operations = [
        migrations.RunPython(add_partial_scan_permissions, remove_partial_scan_permissions),
    ]
