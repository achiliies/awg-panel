from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ClientMeta",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("public_key", models.CharField(db_index=True, max_length=64, unique=True)),
                ("name", models.CharField(blank=True, default="", max_length=64)),
                ("email", models.CharField(blank=True, default="", max_length=190)),
                ("note", models.TextField(blank=True, default="")),
                ("quota_bytes", models.BigIntegerField(default=0)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("offset_rx", models.BigIntegerField(default=0)),
                ("offset_tx", models.BigIntegerField(default=0)),
                ("disabled_reason", models.CharField(blank=True, default="", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "client metadata",
                "verbose_name_plural": "client metadata",
                "ordering": ["name", "public_key"],
            },
        ),
    ]
