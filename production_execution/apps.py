from django.apps import AppConfig


class ProductionExecutionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'production_execution'
    verbose_name = 'Production Execution'

    def ready(self):
        import production_execution.signals  # noqa: F401
