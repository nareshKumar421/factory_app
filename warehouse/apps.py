from django.apps import AppConfig


class WarehouseConfig(AppConfig):
    name = 'warehouse'

    def ready(self):
        import warehouse.signals  # noqa: F401
