import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """The first table this app has ever had.

    Sign-in lived entirely in django.contrib.auth and django.contrib.sessions
    until now, so there is nothing here to migrate from: an upgrade creates the
    table empty, and the sessions that are already open are recorded the first
    time each of them makes a request.
    """

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LoginSession",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("session_key", models.CharField(max_length=40, unique=True)),
                ("created_at", models.DateTimeField()),
                ("last_seen_at", models.DateTimeField()),
                ("ip", models.CharField(blank=True, default="", max_length=45)),
                ("user_agent", models.CharField(blank=True, default="", max_length=400)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="login_sessions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "login session",
                "verbose_name_plural": "login sessions",
                "ordering": ["-last_seen_at"],
            },
        ),
    ]
