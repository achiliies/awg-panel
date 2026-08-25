from django.apps import AppConfig


class ClientsConfig(AppConfig):
    # "clients", not "apps_clients": the label prefixes the table names and is
    # written into every migration, so it has to survive the package moving.
    name = "apps.clients"
    label = "clients"
    verbose_name = "VPN clients"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Let awg.store tell the index what each config write changed.

        Registered here rather than imported at the write site because awg is
        the layer underneath this one: apps.clients.index imports awg.store, so
        awg.store importing it back would be a cycle. Handing it a callable at
        startup keeps the dependency pointing one way and keeps awg importable
        by the CLI and the tests without Django loaded.

        No database work happens here. `ready()` runs before migrations have
        necessarily been applied - `manage.py migrate` calls it on a database
        that may not have these tables yet - so anything that touches them
        belongs behind the first request instead.
        """
        from awg import store

        from . import index

        store.on_config_write(index.absorb, index.begin, index.finish)
