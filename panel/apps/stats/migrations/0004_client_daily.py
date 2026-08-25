"""Bring back a per-client history, at one row per client per day.

Migration 0003 removed the per-client tables and said why: a row per client per
ten-second flush grows with the clock, and on a wide server that was tens of
millions of rows a day to serve one figure on one card. Nothing about that has
been reconsidered. What is added here is the same fact at a sample rate four
orders of magnitude lower - the row is keyed by the day, so a client that
transfers all afternoon is written a thousand times and remains one row.

Nothing is backfilled, and there is nothing that could be. The old tables are
gone, and traffic.db holds a running total with no record of when any of it
moved - which is the whole reason this table exists. So an upgraded server's
history starts at the day it upgrades, and the chart shows the days before that
as the zeroes they will always be.

Hung off ClientMeta with a cascade rather than keyed by public key, so a client's
history ends exactly where the client does and follows a key rotation without
anything having to carry it. apps.stats.models.ClientDaily is where that choice
is argued out.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        # The cascade points at ClientMeta, so this cannot be applied before the
        # migration that last touched it.
        ("clients", "0007_bandwidth_ceilings"),
        ("stats", "0003_daily_total"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClientDaily",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("day", models.DateField(db_index=True)),
                ("rx", models.BigIntegerField(default=0)),
                ("tx", models.BigIntegerField(default=0)),
                (
                    "meta",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="daily",
                        to="clients.clientmeta",
                    ),
                ),
            ],
            options={
                "ordering": ["-day"],
                "constraints": [
                    models.UniqueConstraint(fields=("meta", "day"), name="uniq_client_day")
                ],
            },
        ),
    ]
