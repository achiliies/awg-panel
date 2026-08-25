from django.db import migrations, models


class Migration(migrations.Migration):
    """Make created_at the moment the client was added, not the moment the row was.

    The existing values are kept: auto_now_add stamped them when the panel first
    wrote a row for the client, which for a client added in the panel is the
    same instant to the second and for one added over SSH is the best guess
    anything has. The collector replaces the guesses with the config's
    "# Created" comment on its next enforcement pass.
    """

    dependencies = [("clients", "0002_last_seen")]

    operations = [
        migrations.AlterField(
            model_name="clientmeta",
            name="created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
