from django.apps import AppConfig


class NonMovingRmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "non_moving_rm"
    verbose_name = "Non-Moving Raw Material Dashboard"
