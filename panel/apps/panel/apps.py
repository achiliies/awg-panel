from django.apps import AppConfig


class PanelConfig(AppConfig):
    # "panel", not "apps_panel": the label prefixes the table names and shows up
    # in every migration file, and it has to stay stable if the package ever
    # moves.
    name = "apps.panel"
    label = "panel"
    verbose_name = "Panel settings"
    default_auto_field = "django.db.models.BigAutoField"
