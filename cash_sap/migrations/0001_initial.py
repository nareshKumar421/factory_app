# Written by hand on branch feat/fixed-asset-grpo (Django 6.0.1 field syntax).
#
# New tables only. Nothing here alters cash_book: it is depended on so the
# foreign keys to CashBranch, CashBunch and CashEntry resolve against its
# latest migration (0014_a_voucher_can_be_signed_on_paper).

import cash_sap.models
import django.core.serializers.json
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


STATUS_CHOICES = [
    ('PLANNED', 'Not sent'),
    ('QUEUED', 'Waiting for SAP'),
    ('IN_APPROVAL', 'In SAP approval'),
    ('POSTED', 'In SAP'),
    ('FAILED', 'Refused / stopped'),
    ('NOT_NEEDED', 'Not needed'),
]
MODE_CHOICES = [
    ('PO_GRPO', 'PO per bunch, GRPO per voucher'),
    ('AP_DRAFT_ONLY', 'A/P draft only'),
]
GROUPING_CHOICES = [
    ('NONE', 'One GRPO per voucher'),
    ('GATE_NO', 'One GRPO per bill date + G.no.'),
    ('BILL_NO', 'One GRPO per bill date + bill no.'),
]
VOUCHER_TREATMENT_CHOICES = [
    ('ITEM', 'Goods (GRPO)'),
    ('SERVICE', 'Service line'),
    ('SKIP', 'Not posted'),
]
HEAD_TREATMENT_CHOICES = [
    ('ITEM', 'Goods — GRPO against the imprest PO'),
    ('SERVICE', 'Service — A/P service line'),
    ('NEVER', 'Never posted'),
]
BUNCH_STATUS_CHOICES = [
    ('OPEN', 'Not frozen'),
    ('FROZEN', 'Frozen for SAP'),
    ('CHANGED', 'Changed in the cash book after freezing'),
]
AP_KIND_CHOICES = [
    ('ITEM', 'Item (based on the GRPOs)'),
    ('SERVICE', 'Service lines'),
]


def _base_fields():
    """gate_core.models.base.BaseModel's columns, as makemigrations writes them."""
    return [
        ('created_at', models.DateTimeField(auto_now_add=True)),
        ('updated_at', models.DateTimeField(auto_now=True)),
        ('is_active', models.BooleanField(default=True)),
        ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_created', to=settings.AUTH_USER_MODEL)),
        ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_updated', to=settings.AUTH_USER_MODEL)),
    ]


def _user_fk():
    return models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)


def _json(default=dict):
    return models.JSONField(blank=True, default=default, encoder=django.core.serializers.json.DjangoJSONEncoder)


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('cash_book', '0014_a_voucher_can_be_signed_on_paper'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CashBranchSapRouting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                *_base_fields(),
                ('enabled', models.BooleanField(default=False)),
                ('sap_company_code', models.CharField(blank=True, default='', max_length=30)),
                ('mode', models.CharField(choices=MODE_CHOICES, default='PO_GRPO', max_length=16)),
                ('vendor_code', models.CharField(blank=True, default='', max_length=30)),
                ('bpl_id', models.PositiveIntegerField(blank=True, null=True)),
                ('warehouse_code', models.CharField(blank=True, default='', max_length=20)),
                ('ocr_code', models.CharField(blank=True, default='', max_length=30)),
                ('ocr_code3', models.CharField(blank=True, default='', max_length=30)),
                ('ocr_code5', models.CharField(blank=True, default='', max_length=30)),
                ('item_tax_code', models.CharField(default='IGST@0', max_length=20)),
                ('service_tax_code', models.CharField(default='Exampt', max_length=20)),
                ('ap_drafts_enabled', models.BooleanField(default=False)),
                ('grpo_needs_attachment', models.BooleanField(default=True)),
                ('grpo_grouping', models.CharField(choices=GROUPING_CHOICES, default='NONE', max_length=10)),
                ('po_series_hint', models.CharField(blank=True, default='', max_length=40)),
                ('grpo_series_hint', models.CharField(blank=True, default='', max_length=40)),
                ('ap_series_hint', models.CharField(blank=True, default='', max_length=40)),
                ('cutover_date', models.DateField(blank=True, null=True)),
                ('branch', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='sap_routing', to='cash_book.cashbranch')),
            ],
            options={
                'default_permissions': (),
                'permissions': [('can_manage_cash_sap_settings', 'Can edit cash book SAP routing and G/L item map')],
            },
        ),
        migrations.CreateModel(
            name='GLItemMap',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                *_base_fields(),
                ('sap_company_code', models.CharField(max_length=30)),
                ('gl_account_code', models.CharField(max_length=32)),
                ('gl_account_name', models.CharField(blank=True, default='', max_length=255)),
                ('target_account_code', models.CharField(blank=True, default='', max_length=32)),
                ('treatment', models.CharField(choices=HEAD_TREATMENT_CHOICES, default='ITEM', max_length=8)),
                ('item_code', models.CharField(blank=True, default='', max_length=50)),
                ('item_name', models.CharField(blank=True, default='', max_length=200)),
                ('ocr_code3', models.CharField(blank=True, default='', max_length=30)),
                ('needs_review', models.BooleanField(default=False)),
            ],
            options={
                'default_permissions': (),
                'ordering': ['sap_company_code', 'gl_account_code'],
                'constraints': [models.UniqueConstraint(condition=models.Q(('is_active', True)), fields=('sap_company_code', 'gl_account_code'), name='uq_cash_sap_gl_map_active')],
            },
        ),
        migrations.CreateModel(
            name='VoucherSapInfo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bill_date', models.DateField(blank=True, null=True)),
                ('gate_entry_no', models.CharField(blank=True, default='', max_length=30)),
                ('supplier_bill_no', models.CharField(blank=True, default='', max_length=60)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('entry', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='sap_info', to='cash_book.cashentry')),
                ('updated_by', _user_fk()),
            ],
            options={
                'default_permissions': (),
            },
        ),
        migrations.CreateModel(
            name='BunchSapState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=BUNCH_STATUS_CHOICES, default='OPEN', max_length=16)),
                ('frozen_at', models.DateTimeField(blank=True, null=True)),
                ('member_snapshot', _json()),
                ('drift', _json()),
                ('checked_at', models.DateTimeField(blank=True, null=True)),
                ('changes_accepted_at', models.DateTimeField(blank=True, null=True)),
                ('changes_accepted_note', models.TextField(blank=True, default='')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('bunch', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='sap_state', to='cash_book.cashbunch')),
                ('frozen_by', _user_fk()),
                ('changes_accepted_by', _user_fk()),
            ],
            options={
                'default_permissions': (),
            },
        ),
        migrations.CreateModel(
            name='ImprestBatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                *_base_fields(),
                ('sap_company_code', models.CharField(max_length=30)),
                ('mode', models.CharField(choices=MODE_CHOICES, default='PO_GRPO', max_length=16)),
                ('vendor_code', models.CharField(blank=True, default='', max_length=30)),
                ('bpl_id', models.PositiveIntegerField(blank=True, null=True)),
                ('warehouse_code', models.CharField(blank=True, default='', max_length=20)),
                ('ap_drafts_enabled', models.BooleanField(default=False)),
                ('grpo_needs_attachment', models.BooleanField(default=True)),
                ('grpo_grouping', models.CharField(choices=GROUPING_CHOICES, default='NONE', max_length=10)),
                ('po_series_hint', models.CharField(blank=True, default='', max_length=40)),
                ('grpo_series_hint', models.CharField(blank=True, default='', max_length=40)),
                ('ap_series_hint', models.CharField(blank=True, default='', max_length=40)),
                ('expense_month', models.DateField(blank=True, null=True)),
                ('po_doc_date', models.DateField(blank=True, null=True)),
                ('po_due_date', models.DateField(blank=True, null=True)),
                ('po_round', models.PositiveSmallIntegerField(default=1)),
                ('po_status', models.CharField(choices=STATUS_CHOICES, default='PLANNED', max_length=16)),
                ('po_doc_entry', models.PositiveBigIntegerField(blank=True, null=True)),
                ('po_doc_num', models.PositiveBigIntegerField(blank=True, null=True)),
                ('po_draft_entry', models.PositiveBigIntegerField(blank=True, null=True)),
                ('po_doc_total', models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ('po_payload', _json()),
                ('po_error', models.TextField(blank=True, default='')),
                ('po_posted_at', models.DateTimeField(blank=True, null=True)),
                ('po_close_status', models.CharField(choices=STATUS_CHOICES, default='NOT_NEEDED', max_length=16)),
                ('po_close_error', models.TextField(blank=True, default='')),
                ('simulated', models.BooleanField(default=False)),
                ('requested_at', models.DateTimeField(blank=True, null=True)),
                ('last_sent_at', models.DateTimeField(blank=True, null=True)),
                ('completion_notified_at', models.DateTimeField(blank=True, null=True)),
                ('bunch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='sap_batches', to='cash_book.cashbunch')),
                ('requested_by', _user_fk()),
                ('last_sent_by', _user_fk()),
            ],
            options={
                'default_permissions': (),
                'permissions': [('can_post_cash_to_sap', 'Can post cash book bunches to SAP')],
                'constraints': [models.UniqueConstraint(fields=('bunch', 'sap_company_code'), name='uq_imprest_batch_bunch_company')],
            },
        ),
        migrations.CreateModel(
            name='ImprestGRPO',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                *_base_fields(),
                ('group_key', models.CharField(max_length=160)),
                ('round', models.PositiveSmallIntegerField(default=1)),
                ('sap_company_code', models.CharField(max_length=30)),
                ('vendor_code', models.CharField(blank=True, default='', max_length=30)),
                ('bill_date', models.DateField()),
                ('posting_date', models.DateField()),
                ('gate_entry_no', models.CharField(blank=True, default='', max_length=60)),
                ('supplier_bill_no', models.CharField(blank=True, default='', max_length=60)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('num_at_card', models.CharField(blank=True, default='', max_length=100)),
                ('status', models.CharField(choices=STATUS_CHOICES, default='PLANNED', max_length=16)),
                ('grpo_doc_entry', models.PositiveBigIntegerField(blank=True, null=True)),
                ('grpo_doc_num', models.PositiveBigIntegerField(blank=True, null=True)),
                ('grpo_draft_entry', models.PositiveBigIntegerField(blank=True, null=True)),
                ('grpo_doc_total', models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ('payload', _json()),
                ('error', models.TextField(blank=True, default='')),
                ('posted_at', models.DateTimeField(blank=True, null=True)),
                ('sap_attachment_entry', models.PositiveBigIntegerField(blank=True, null=True)),
                ('uploaded_files', _json(list)),
                ('line_map', _json()),
                ('simulated', models.BooleanField(default=False)),
                ('last_sent_at', models.DateTimeField(blank=True, null=True)),
                ('batch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='grpos', to='cash_sap.imprestbatch')),
                ('last_sent_by', _user_fk()),
            ],
            options={
                'default_permissions': (),
                'constraints': [
                    models.UniqueConstraint(fields=('batch', 'group_key'), name='uq_imprest_grpo_batch_key'),
                    models.UniqueConstraint(condition=models.Q(('num_at_card', ''), _negated=True), fields=('sap_company_code', 'vendor_code', 'num_at_card'), name='uq_imprest_grpo_num_at_card'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ImprestAPDraft',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                *_base_fields(),
                ('kind', models.CharField(choices=AP_KIND_CHOICES, max_length=8)),
                ('round', models.PositiveSmallIntegerField(default=1)),
                ('sap_company_code', models.CharField(max_length=30)),
                ('vendor_code', models.CharField(blank=True, default='', max_length=30)),
                ('status', models.CharField(choices=STATUS_CHOICES, default='PLANNED', max_length=16)),
                ('num_at_card', models.CharField(blank=True, default='', max_length=100)),
                ('doc_date', models.DateField(blank=True, null=True)),
                ('amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('payload', _json()),
                ('error', models.TextField(blank=True, default='')),
                ('sap_draft_entry', models.PositiveBigIntegerField(blank=True, null=True)),
                ('sap_doc_entry', models.PositiveBigIntegerField(blank=True, null=True)),
                ('sap_doc_num', models.PositiveBigIntegerField(blank=True, null=True)),
                ('posted_at', models.DateTimeField(blank=True, null=True)),
                ('simulated', models.BooleanField(default=False)),
                ('last_sent_at', models.DateTimeField(blank=True, null=True)),
                ('batch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ap_drafts', to='cash_sap.imprestbatch')),
                ('last_sent_by', _user_fk()),
            ],
            options={
                'default_permissions': (),
                'constraints': [
                    models.UniqueConstraint(fields=('batch', 'kind'), name='uq_imprest_ap_draft_batch_kind'),
                    models.UniqueConstraint(condition=models.Q(('num_at_card', ''), _negated=True), fields=('sap_company_code', 'vendor_code', 'num_at_card'), name='uq_imprest_ap_num_at_card'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ImprestVoucher',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                *_base_fields(),
                ('sap_company_code_override', models.CharField(blank=True, default='', max_length=30)),
                ('treatment_override', models.CharField(blank=True, choices=VOUCHER_TREATMENT_CHOICES, default='', max_length=8)),
                ('item_code_override', models.CharField(blank=True, default='', max_length=50)),
                ('ocr_code_override', models.CharField(blank=True, default='', max_length=30)),
                ('ocr_code3_override', models.CharField(blank=True, default='', max_length=30)),
                ('override_note', models.TextField(blank=True, default='')),
                ('overridden_at', models.DateTimeField(blank=True, null=True)),
                ('treatment', models.CharField(blank=True, choices=VOUCHER_TREATMENT_CHOICES, default='', max_length=8)),
                ('item_code', models.CharField(blank=True, default='', max_length=50)),
                ('item_name', models.CharField(blank=True, default='', max_length=200)),
                ('gl_account_code', models.CharField(blank=True, default='', max_length=32)),
                ('gl_account_name', models.CharField(blank=True, default='', max_length=255)),
                ('account_code', models.CharField(blank=True, default='', max_length=32)),
                ('warehouse_code', models.CharField(blank=True, default='', max_length=20)),
                ('tax_code', models.CharField(blank=True, default='', max_length=20)),
                ('ocr_code', models.CharField(blank=True, default='', max_length=30)),
                ('ocr_code2', models.CharField(blank=True, default='', max_length=30)),
                ('ocr_code3', models.CharField(blank=True, default='', max_length=30)),
                ('ocr_code5', models.CharField(blank=True, default='', max_length=30)),
                ('bill_date', models.DateField(blank=True, null=True)),
                ('gate_entry_no', models.CharField(blank=True, default='', max_length=30)),
                ('supplier_bill_no', models.CharField(blank=True, default='', max_length=60)),
                ('amount', models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ('po_line_num', models.PositiveIntegerField(blank=True, null=True)),
                ('skip_reason', models.CharField(blank=True, default='', max_length=255)),
                ('left_out_after_po', models.BooleanField(default=False)),
                ('left_out_at', models.DateTimeField(blank=True, null=True)),
                ('left_out_reason', models.TextField(blank=True, default='')),
                ('entry', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='sap_voucher', to='cash_book.cashentry')),
                ('bunch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='cash_book.cashbunch')),
                ('batch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='vouchers', to='cash_sap.imprestbatch')),
                ('grpo', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='vouchers', to='cash_sap.imprestgrpo')),
                ('overridden_by', _user_fk()),
                ('left_out_by', _user_fk()),
            ],
            options={
                'default_permissions': (),
            },
        ),
        migrations.CreateModel(
            name='ImprestVoucherBill',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                *_base_fields(),
                ('file', models.FileField(max_length=255, upload_to=cash_sap.models._bill_path)),
                ('original_filename', models.CharField(max_length=255)),
                ('size_bytes', models.PositiveIntegerField(default=0)),
                ('voucher', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bills', to='cash_sap.imprestvoucher')),
            ],
            options={
                'default_permissions': (),
                'ordering': ['id'],
            },
        ),
    ]
