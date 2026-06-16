from django.apps import AppConfig


class GrpoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'grpo'
    verbose_name = 'GRPO Management'

    def ready(self):
        import grpo.signals  # noqa: F401
