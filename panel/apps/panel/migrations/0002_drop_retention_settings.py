"""Forget the two retention settings, which no longer control anything.

A row exists in this table only once its value differs from the default, so
these are present exactly on the servers where somebody tuned them. Left behind
they would be served by the settings endpoint forever - a value no code reads,
that the save endpoint now rejects as an unknown key, and that an admin reading
the API would reasonably take for a working control.

The keys are written out rather than derived from ``defaults.DEFAULTS``. A
migration runs against whatever the code says years from now, and one that
deletes "every key the current version does not recognise" would quietly wipe a
setting introduced after it.
"""

from django.db import migrations

GONE = ("sampleRetentionHours", "hourlyRetentionDays")


def forget(apps, schema_editor):
    apps.get_model("panel", "Setting").objects.filter(key__in=GONE).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("panel", "0001_initial"),
    ]

    operations = [
        # No reverse: restoring the rows would restore values nothing reads, and
        # a downgrade past this point puts the defaults back by itself.
        migrations.RunPython(forget, migrations.RunPython.noop),
    ]
