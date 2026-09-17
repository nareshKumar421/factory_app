from django.apps import AppConfig


class IssuesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "issues"
    verbose_name = "Issue Tracker"

    def ready(self):
        # Registers the receiver that makes every new account a reporter.
        from . import signals  # noqa: F401
