"""The module's permission surface is exactly nine rows.

Django creates add/change/delete/view for every model unless a model says
otherwise. Six models would mean 24 rows nothing in this module checks, and a
`view_project` sitting beside `can_view_project` in the group editor is a
footgun: granting the wrong one looks right and does nothing. Every model sets
``default_permissions = ()``; this test is what stops a seventh model
reintroducing them.
"""

from django.contrib.auth.models import Permission
from django.test import TestCase

EXPECTED = {
    "can_approve_expense",
    "can_approve_project",
    "can_close_project",
    "can_create_project",
    "can_edit_project",
    "can_log_daily_work",
    "can_record_expense",
    "can_view_all_projects",
    "can_view_project",
}


class PermissionSurfaceTests(TestCase):
    def test_exactly_the_nine_custom_permissions_exist(self):
        actual = set(
            Permission.objects.filter(
                content_type__app_label="construction_projects"
            ).values_list("codename", flat=True)
        )
        self.assertEqual(
            actual,
            EXPECTED,
            "Unexpected permissions. A new model needs "
            "default_permissions = () in its Meta.",
        )

    def test_every_permission_class_names_a_real_permission(self):
        """A permission class naming a codename that does not exist gates
        nothing and fails open to nobody -- silently locking everyone out."""
        from construction_projects import permissions as perms_module

        declared = [
            value.permission
            for name, value in vars(perms_module).items()
            if isinstance(value, type)
            and issubclass(value, perms_module.DjangoPermission)
            and value is not perms_module.DjangoPermission
        ]
        self.assertTrue(declared, "no permission classes found")
        for dotted in declared:
            app_label, codename = dotted.split(".")
            self.assertEqual(app_label, "construction_projects")
            self.assertIn(codename, EXPECTED, f"{dotted} names no real permission")
