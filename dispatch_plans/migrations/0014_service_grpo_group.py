from django.db import migrations


# Dedicated role for the Dispatch > Service GRPO submodule. Holds only
# can_post_bilty_service_grpo, which is enough to fully work in the submodule:
# every Service GRPO check (frontend nav/routes and the backend permission
# classes CanViewBiltyServiceGRPOQueue / CanPreviewBiltyServiceGRPO /
# CanPostBiltyServiceGRPO / CanViewBiltyServiceGRPOHistory /
# CanViewBiltyServiceGRPODetail) is an OR that accepts this permission. So a
# member can view the pending queue, preview, post, and see history/detail,
# without any dispatch-plans, AP-invoice, or docking access.
# Membership is assigned operationally per environment.
GROUP_NAME = "Service GRPO"
PERMISSION_CODENAME = "can_post_bilty_service_grpo"


def create_service_grpo_group(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    content_type = ContentType.objects.filter(
        app_label="dispatch_plans",
        model="transporterapinvoiceposting",
    ).first()
    if content_type is None:
        return

    permission = Permission.objects.filter(
        content_type=content_type,
        codename=PERMISSION_CODENAME,
    ).first()
    if permission is None:
        return

    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.set([permission])


def delete_service_grpo_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dispatch_plans", "0013_dispatchplan_customer_code_and_more"),
    ]

    operations = [
        migrations.RunPython(
            create_service_grpo_group,
            delete_service_grpo_group,
        ),
    ]
