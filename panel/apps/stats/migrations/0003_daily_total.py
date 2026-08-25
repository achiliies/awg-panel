"""Replace the per-client sample and rollup tables with one row a day.

The two old tables are dropped rather than migrated into the new one. Their
contents could be summed per day and carried across, but the result would be a
history whose early days came from a rollup that had already been pruned at
whatever retention the server happened to be set to - a figure that looks
authoritative and is quietly partial. The only reader of any of this is the
dashboard's "today" card, which is correct again after one collector cycle.

Dropping them does not shrink the database file. SQLite keeps freed pages for
reuse rather than returning them, so an existing install stays at its high-water
mark and simply stops growing; `VACUUM` with the services stopped is what
reclaims the disk, and docs/PANEL.md says how.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("stats", "0002_drop_redundant_public_key_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="DailyTotal",
            fields=[
                ("day", models.DateField(primary_key=True, serialize=False)),
                ("rx", models.BigIntegerField(default=0)),
                ("tx", models.BigIntegerField(default=0)),
            ],
            options={
                "ordering": ["-day"],
            },
        ),
        migrations.DeleteModel(name="TrafficSample"),
        migrations.DeleteModel(name="TrafficHourly"),
    ]
