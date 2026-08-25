import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Give the client list something with an index on it to read.

    Both tables arrive empty and stay that way until the first request, which
    finds the stamps in ClientIndexState at zero, decides the copy is stale
    because nothing on disk has a modification time of zero, and builds it. So
    there is no data migration here and nothing to backfill: the config files are
    the source, they are already on disk, and the first read after this lands
    pays one rebuild for the whole server.

    Dropping either table is safe at any point, which is worth knowing before the
    next migration touches them. Everything in both is recomputed from the server
    config, the client config files and traffic.db.
    """

    dependencies = [("clients", "0004_expiry_override")]

    operations = [
        migrations.CreateModel(
            name="ClientIndexState",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False),
                ),
                ("conf_mtime_ns", models.BigIntegerField(default=0)),
                ("conf_size", models.BigIntegerField(default=0)),
                ("conf_inode", models.BigIntegerField(default=0)),
                ("clients_mtime_ns", models.BigIntegerField(default=0)),
                ("traffic_mtime_ns", models.BigIntegerField(default=0)),
                ("traffic_size", models.BigIntegerField(default=0)),
                ("built_at", models.DateTimeField(auto_now=True)),
                ("subnet_cidr", models.CharField(blank=True, default="", max_length=64)),
                ("free_ips", models.IntegerField(default=0)),
                ("has_ipv6", models.BooleanField(default=False)),
            ],
            options={
                "verbose_name": "client index state",
                "verbose_name_plural": "client index state",
            },
        ),
        migrations.CreateModel(
            name="ClientIndex",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=64)),
                ("ip", models.CharField(blank=True, default="", max_length=45)),
                ("ip6", models.CharField(blank=True, default="", max_length=45)),
                ("allowed_ips", models.TextField(blank=True, default="")),
                ("enabled", models.BooleanField(default=True)),
                ("has_conf_file", models.BooleanField(default=False)),
                (
                    "created_key",
                    models.CharField(blank=True, db_index=True, default="", max_length=32),
                ),
                (
                    "name_key",
                    models.CharField(blank=True, db_index=True, default="", max_length=64),
                ),
                ("ip_order", models.BigIntegerField(db_index=True, default=-1)),
                ("position", models.IntegerField(default=0)),
                ("cum_rx", models.BigIntegerField(default=0)),
                ("cum_tx", models.BigIntegerField(default=0)),
                ("conf_mtime_ns", models.BigIntegerField(default=0)),
                ("conf_size", models.BigIntegerField(default=0)),
                (
                    "meta",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="index",
                        to="clients.clientmeta",
                    ),
                ),
            ],
            options={
                "verbose_name": "client index entry",
                "verbose_name_plural": "client index",
                "ordering": ["created_key", "position"],
            },
        ),
    ]
