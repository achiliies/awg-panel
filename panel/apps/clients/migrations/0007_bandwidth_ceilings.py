from django.db import migrations, models


class Migration(migrations.Migration):
    """How fast each client may go, in each direction.

    Zero for every existing row, which is the value that means "no ceiling", so
    a server that migrates into this behaves exactly as it did the day before:
    nothing is shaped, and awg.shaper attaches nothing to any interface until
    somebody sets a number.

    Two columns rather than one because the two directions are enforced in
    different places and one of them needs a WAN interface to enforce it on -
    see awg/shaper.py. A client can legitimately be capped downward and left
    alone upward, which is the usual case.
    """

    dependencies = [("clients", "0006_expires_at_index")]

    operations = [
        migrations.AddField(
            model_name="clientmeta",
            name="down_bps",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="clientmeta",
            name="up_bps",
            field=models.BigIntegerField(default=0),
        ),
    ]
