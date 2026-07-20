from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('blowing', '0006_resync_blowing_group'),
    ]
    operations = [
        migrations.RenameField(
            model_name='blowingmachine',
            old_name='cavities',
            new_name='heads',
        ),
    ]
