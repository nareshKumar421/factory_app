"""A project's map is shown on its own, so an attachment has to know it is one.

Everything already uploaded becomes a DOCUMENT, which is what it was being
treated as. Nothing moves, and the stored order is unchanged: "maps first" is
annotated by the API, because Meta ordering would sort the stored strings and
"DOCUMENT" sorts above "MAP".
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('construction_projects', '0009_drafts_may_be_incomplete'),
    ]

    operations = [
        migrations.AddField(
            model_name='projectattachment',
            name='kind',
            field=models.CharField(choices=[('MAP', 'Site map'), ('DOCUMENT', 'Document')], db_index=True, default='DOCUMENT', max_length=10),
        ),
    ]
