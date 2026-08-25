from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Setting",
            fields=[
                (
                    "key",
                    models.CharField(max_length=64, primary_key=True, serialize=False),
                ),
                ("value", models.TextField(blank=True, default="")),
            ],
            options={
                "verbose_name": "setting",
                "verbose_name_plural": "settings",
                "ordering": ["key"],
            },
        ),
    ]
