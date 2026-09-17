from django.apps import AppConfig


class ControlBoardsConfig(AppConfig):
    """The control boards' shared access layer, and the boards that lack an app.

    Owns no data: no ``models.py``, so the only migration is a data migration
    over ``auth_permission``. That is deliberate and follows ``admin_board`` and
    ``plant_board`` -- a board composes existing reports and should be
    deployable against the live database with no table to create.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "control_boards"
    verbose_name = "Control boards"
