"""The panel's own settings, as one small key/value table.

A column per setting would mean a migration for every new toggle and a schema
that has to be kept in step with the frontend's TypeScript by hand. There are
fourteen settings, all of them read as text and coerced on use, so a key/value
table is both smaller and easier to extend. ``apps.panel.defaults`` is the
authoritative list of keys; a row exists only once its value differs from the
default, which is what makes an upgrade that adds a setting a no-op.

Removing one is the case that needs a migration, and only to tidy up: a row for
a key nothing recognises would otherwise be served by the settings endpoint
forever. See ``0002_drop_retention_settings``.
"""

from django.db import models


class Setting(models.Model):
    """One stored setting. Absent means "still the default"."""

    key = models.CharField(max_length=64, primary_key=True)
    value = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["key"]
        verbose_name = "setting"
        verbose_name_plural = "settings"

    def __str__(self) -> str:
        return self.key
