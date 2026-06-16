from django.apps import AppConfig


class PersonGateinConfig(AppConfig):
    name = 'person_gatein'

    def ready(self):
        import person_gatein.signals  # noqa: F401
