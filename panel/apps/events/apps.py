from django.apps import AppConfig


class EventsConfig(AppConfig):
    # "events", not "apps_events": the label prefixes the table names and is
    # written into every migration, so it has to survive the package moving.
    name = "apps.events"
    label = "events"
    verbose_name = "Event log"
    default_auto_field = "django.db.models.BigAutoField"
