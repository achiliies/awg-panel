from django.db import migrations, models


class Migration(migrations.Migration):
    """When a client's counters and history were last cleared.

    Null for every existing row, which is what "never cleared" already meant, so
    nothing about a server that migrates into this changes: the collector reads
    the column from the rows it was reading anyway and finds nothing to react
    to until somebody presses the button.

    A timestamp rather than a counter because it is also worth reading: the one
    thing a cleared history cannot tell an admin afterwards is when it was
    cleared, and the row that survives the clearing is where that has to live.
    """

    dependencies = [("clients", "0007_bandwidth_ceilings")]

    operations = [
        migrations.AddField(
            model_name="clientmeta",
            name="history_reset_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
