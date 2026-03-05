from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_basicduessetting"),
    ]

    operations = [
        migrations.AddField(
            model_name="member",
            name="application_note",
            field=models.TextField(blank=True, default=""),
        ),
    ]
