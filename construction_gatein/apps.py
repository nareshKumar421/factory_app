from django.apps import AppConfig


class ConstructionGateinConfig(AppConfig):
    name = 'construction_gatein'

    def ready(self):
        import construction_gatein.signals  # noqa: F401
