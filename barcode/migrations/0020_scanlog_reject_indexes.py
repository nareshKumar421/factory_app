from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    """Index ScanLog for scan failure-rate reporting.

    Built CONCURRENTLY: the table is written on every dock and BST scan, and a
    plain CREATE INDEX takes a lock that would stall a truck being scanned during
    the deploy. Concurrent builds cannot run inside a transaction, hence
    ``atomic = False``. If one fails midway Postgres leaves an INVALID index
    behind: drop it by name and re-run this migration.
    """

    atomic = False

    dependencies = [
        ('barcode', '0019_scanlog_reject_code_scanlog_reject_message_and_more'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='scanlog',
            index=models.Index(
                fields=['context_ref_type', 'context_ref_id'],
                name='barcode_sca_context_d938c8_idx',
            ),
        ),
        AddIndexConcurrently(
            model_name='scanlog',
            index=models.Index(
                fields=['scan_result', 'scanned_at'],
                name='barcode_sca_scan_re_a97482_idx',
            ),
        ),
        AddIndexConcurrently(
            model_name='scanlog',
            index=models.Index(
                fields=['reject_code'],
                name='barcode_sca_reject__71c0b4_idx',
            ),
        ),
    ]
