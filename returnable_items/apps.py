from django.apps import AppConfig


class ReturnableItemsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "returnable_items"
    verbose_name = "Returnable Items"

    def ready(self):
        from django.db.models.signals import post_migrate

        from .signals import ensure_returnable_groups

        post_migrate.connect(
            ensure_returnable_groups,
            sender=self,
            dispatch_uid="returnable_items.ensure_returnable_groups",
        )
