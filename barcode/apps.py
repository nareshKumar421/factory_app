from django.apps import AppConfig


class BarcodeConfig(AppConfig):
    name = 'barcode'

    def ready(self):
        import barcode.signals  # noqa: F401
