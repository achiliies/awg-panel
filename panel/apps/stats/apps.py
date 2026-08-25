from django.apps import AppConfig


class StatsConfig(AppConfig):
    # "stats", not "apps_stats": the label prefixes the table names and is
    # written into every migration, so it has to survive the package moving.
    name = "apps.stats"
    label = "stats"
    verbose_name = "Traffic statistics"
    default_auto_field = "django.db.models.BigAutoField"
