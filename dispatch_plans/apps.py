from django.apps import AppConfig


class DispatchPlansConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dispatch_plans"
    verbose_name = "Dispatch Plans"

    def ready(self):
        import dispatch_plans.signals  # noqa: F401

