from django.apps import AppConfig


class ProductionOrdersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "production_orders"
    verbose_name = "Production Orders"
