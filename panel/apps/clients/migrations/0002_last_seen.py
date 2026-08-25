from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("clients", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="clientmeta",
            name="last_handshake",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="clientmeta",
            name="last_endpoint",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
    ]
