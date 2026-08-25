import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """The table behind API tokens, which nothing before this release had.

    Nothing to migrate from and nothing to backfill: an upgrade creates it
    empty, and a panel where nobody issues a token keeps it that way. Sessions
    and the password are untouched, so an upgrade signs nobody out.
    """

    dependencies = [
        ("accounts", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ApiToken",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("name", models.CharField(max_length=64)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("hint", models.CharField(blank=True, default="", max_length=8)),
                ("created_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("lifetime_sec", models.PositiveIntegerField(blank=True, null=True)),
                ("renew_on_use", models.BooleanField(default=False)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_ip", models.CharField(blank=True, default="", max_length=45)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="api_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "API token",
                "verbose_name_plural": "API tokens",
                "ordering": ["-created_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "name"), name="unique_token_name_per_user"
                    )
                ],
            },
        ),
    ]
