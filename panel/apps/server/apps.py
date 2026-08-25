from django.apps import AppConfig


class ServerAppConfig(AppConfig):
    # No models and no migrations directory: the server's state is awg0.conf.
    name = "apps.server"
    label = "server"
    verbose_name = "Server configuration"
    default_auto_field = "django.db.models.BigAutoField"
