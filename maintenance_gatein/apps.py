from django.apps import AppConfig


class MaintenanceGateinConfig(AppConfig):
    name = 'maintenance_gatein'

    def ready(self):
        import maintenance_gatein.signals  # noqa: F401
