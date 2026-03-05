from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_alter_memberreviewlog_action_memberduespayment"),
    ]

    operations = [
        migrations.AddField(
            model_name="basicduessetting",
            name="next_annual_dues_sats",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
