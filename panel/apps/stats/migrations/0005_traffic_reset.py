"""One row to carry a wipe from the panel process to the collector.

"Remove all traffic" empties three stores, and only two of them are reachable
from the request that asks for it. The third is traffic.db, which the collector
holds a mirror of and rewrites from memory every ten seconds - so a wipe the
collector was never told about would be undone by the next flush.

This table is the telling. It holds no traffic and never grows: one row, the
moment a wipe was asked for, and the moment the collector finished it. See
apps.stats.models.TrafficReset for why that is two columns rather than a stamp
the collector remembers, and for why a fold that happened twice would be worse
than one that happened late.

Nothing is created here. A panel that has never been asked to clear its traffic
has no row, which is the same answer as a wipe that has been fully applied.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("stats", "0004_client_daily"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrafficReset",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False),
                ),
                ("requested_at", models.DateTimeField()),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
    ]
