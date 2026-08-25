from django.db import migrations, models


class Migration(migrations.Migration):
    """Index the expiry date, because two things now ask for one rather than for a client.

    The collector wants the earliest date still to come, so that it knows when it
    next has to look at expiry at all instead of asking every client every
    minute; the list filters and the sweep want the ones already gone. Both are a
    walk to one end of an ordered column and both were a scan of every row.

    Data is untouched. On SQLite this is a table rebuild, which on any client
    count a single server holds is a great deal faster than the migration that
    created the table.
    """

    dependencies = [("clients", "0005_client_index")]

    operations = [
        migrations.AlterField(
            model_name="clientmeta",
            name="expires_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
