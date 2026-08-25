"""Turn the old link-rate setting into the on/off switch that replaced it.

`shaperLinkMbps` held the server's uplink in megabits, and any value above zero
was what switched bandwidth limits on. The capacity itself turned out to decide
nothing - HTB holds a class to its own ceiling whatever sits above it - so the
setting is now `shaperOn`, and the rate is gone.

A row exists in this table only once its value differs from the default, so a
stored `shaperLinkMbps` is exactly the servers where somebody set a rate, which
is exactly the servers where shaping is meant to stay on. Anything that parses
as a number above zero becomes `shaperOn = 1`; a stored "0" was shaping switched
off and needs no row at all, since off is already the default.

Written out by name rather than derived from the current specs, for the reason
0002 gives: a migration that acted on "whatever the code no longer recognises"
would wipe a setting introduced after it was written.
"""

from django.db import migrations

GONE = "shaperLinkMbps"
REPLACEMENT = "shaperOn"


def carry_over(apps, schema_editor):
    setting = apps.get_model("panel", "Setting")
    row = setting.objects.filter(key=GONE).first()
    if row is not None:
        try:
            was_on = int(str(row.value).strip() or "0") > 0
        except ValueError:
            # Not a number this migration can read. Treating it as "on" keeps a
            # server that was shaping shaping, which is the safer of the two
            # wrong answers: the alternative silently stops enforcing limits
            # that are still listed against every client in the panel.
            was_on = True
        if was_on:
            setting.objects.update_or_create(key=REPLACEMENT, defaults={"value": "1"})
    setting.objects.filter(key=GONE).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("panel", "0002_drop_retention_settings"),
    ]

    operations = [
        # No reverse: the rate it came from is not recoverable from a boolean,
        # and a downgrade past this point finds no row and reads the old default
        # of 0, which is shaping off rather than shaping wrong.
        migrations.RunPython(carry_over, migrations.RunPython.noop),
    ]
