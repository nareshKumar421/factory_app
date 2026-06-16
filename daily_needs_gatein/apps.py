from django.apps import AppConfig


class DailyNeedsGateinConfig(AppConfig):
    name = 'daily_needs_gatein'

    def ready(self):
        import daily_needs_gatein.signals  # noqa: F401
