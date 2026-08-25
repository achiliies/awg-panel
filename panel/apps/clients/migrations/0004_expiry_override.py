from django.db import migrations, models


class Migration(migrations.Migration):
    """Let an admin keep a lapsed client on without moving its date.

    Until now a passed expiry was the same fact as a switched-off peer: the
    collector disabled the client, and switching it back on lasted only until the
    next enforcement pass. This column records the date the admin overruled, so
    the two can disagree on purpose.

    Null for every existing row, which is what "nobody has overruled anything"
    has always meant, so enforcement carries on exactly as before until somebody
    asks it not to.
    """

    dependencies = [("clients", "0003_created_at")]

    operations = [
        migrations.AddField(
            model_name="clientmeta",
            name="expiry_override_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
