from django.apps import AppConfig


class ProductionDispatchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "production_dispatch"
    verbose_name = "Production & Dispatch"
