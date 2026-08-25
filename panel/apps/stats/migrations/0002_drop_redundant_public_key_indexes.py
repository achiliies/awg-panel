"""Drop the single-column public_key indexes, which nothing reads.

Both tables already index public_key as the first column of a composite - the
(public_key, ts) index on the samples, the (public_key, hour_ts) unique
constraint on the rollups - and SQLite serves a lookup on a leading prefix from
those. The extra index was a second B-tree written on every insert for queries
that were never asked: on a 250-client server it cost 37% of the collector's
write time and 22% of the database's size.

Written as SeparateDatabaseAndState because the migration Django generates for a
db_index change on SQLite copies both tables into new ones and rebuilds every
index. That runs from ExecStartPre on the web unit, where the sample table can be
a gigabyte, and it would need twice that in free disk to change nothing but an
index. Dropping the index by name is instant and needs none.

The names are the ones 0001_initial created: Django derives them from the table
and column, so they are the same on every install. IF EXISTS covers the database
that somehow has a different one - it keeps an index nothing queries, which is
what it had before this migration anyway.
"""

from django.db import migrations, models

SAMPLE_INDEX = "stats_trafficsample_public_key_bb3f1a0d"
HOURLY_INDEX = "stats_traffichourly_public_key_e0e210ee"


class Migration(migrations.Migration):
    dependencies = [("stats", "0001_initial")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=f'DROP INDEX IF EXISTS "{SAMPLE_INDEX}";',
                    reverse_sql=(
                        f'CREATE INDEX IF NOT EXISTS "{SAMPLE_INDEX}" '
                        'ON "stats_trafficsample" ("public_key");'
                    ),
                ),
                migrations.RunSQL(
                    sql=f'DROP INDEX IF EXISTS "{HOURLY_INDEX}";',
                    reverse_sql=(
                        f'CREATE INDEX IF NOT EXISTS "{HOURLY_INDEX}" '
                        'ON "stats_traffichourly" ("public_key");'
                    ),
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="trafficsample",
                    name="public_key",
                    field=models.CharField(max_length=64),
                ),
                migrations.AlterField(
                    model_name="traffichourly",
                    name="public_key",
                    field=models.CharField(max_length=64),
                ),
            ],
        )
    ]
