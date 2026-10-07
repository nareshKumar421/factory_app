from django.apps import AppConfig


class ExpenseClaimsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "expense_claims"
    verbose_name = "Expense Claims"

    def ready(self):
        from . import signals  # noqa: F401
