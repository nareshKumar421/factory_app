# Utilities Mobile — Data Migration Guide

Step-by-step guide to migrate data from the PHP app's PostgreSQL `utilities` database into Sampooran's Django database.

---

## Overview

| Source (PHP) | Target (Django) | Strategy |
|---|---|---|
| `tbl_login` | `accounts.User` | Map by email/employee_code — DO NOT duplicate existing users |
| `tbl_questions` | `dynamic_forms.Form` + `FormField` | One-time migration |
| `tbl_responses` | `dynamic_forms.FormSubmission` + `FieldResponse` | Bulk migration with data validation |
| `tbl_permissions` | `dynamic_forms.FormPermission` | Map after user + form migration |
| `tbl_pageperm` | Django groups + permissions | Map pages to new permission system |
| `tbl_invoice_printing` | `docking.DockingEntry` + `DockingInvoice` | Selective migration |
| Material out records | `material_tracking.MaterialMovement` | Selective migration |

---

## Prerequisites

1. Both databases accessible from the Django server
2. All Django migrations applied (`makemigrations` + `migrate`)
3. `setup_utility_groups` management command run (permissions/groups created)
4. Existing Sampooran users mapped (same company users)

---

## Phase 1: User Mapping

### Source: `tbl_login`

```sql
-- PHP PostgreSQL (utilities DB)
SELECT login, password, name FROM tbl_login;
```

### Strategy

Do **NOT** create duplicate users. Map PHP users to existing Sampooran users:

```python
# management/commands/migrate_utility_users.py

class Command(BaseCommand):
    help = 'Map PHP utility users to existing Sampooran users.'

    def handle(self, *args, **options):
        # Connect to PHP database
        php_conn = psycopg2.connect(
            host='php-db-host', database='utilities',
            user='...', password='...'
        )
        cursor = php_conn.cursor()
        cursor.execute("SELECT login, name FROM tbl_login")

        mapped = 0
        not_found = []

        for login, name in cursor.fetchall():
            # Try matching by employee_code or email
            user = User.objects.filter(
                Q(employee_code__iexact=login) |
                Q(email__iexact=login) |
                Q(email__iexact=f"{login}@jivowellness.com")
            ).first()

            if user:
                # Store mapping for later use
                UserMapping.objects.update_or_create(
                    php_login=login,
                    defaults={'django_user': user, 'php_name': name},
                )
                mapped += 1
            else:
                not_found.append({'login': login, 'name': name})

        self.stdout.write(self.style.SUCCESS(f"Mapped: {mapped}"))
        if not_found:
            self.stdout.write(self.style.WARNING(
                f"Not found ({len(not_found)}): {not_found}"))
            self.stdout.write("Create these users manually or update mapping.")

        php_conn.close()
```

### Temporary Mapping Model

```python
# Add temporarily during migration, remove after cutover

class UserMapping(models.Model):
    """Temporary mapping between PHP login and Django user."""
    php_login = models.CharField(max_length=100, unique=True)
    php_name = models.CharField(max_length=200)
    django_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        db_table = 'migration_user_mapping'
```

---

## Phase 2: Form Definitions

### Source: `tbl_questions`

```sql
SELECT id, formid, question, type, "order"
FROM tbl_questions
ORDER BY formid, "order";
```

### Migration Command

```python
# management/commands/migrate_forms.py

class Command(BaseCommand):
    help = 'Migrate PHP forms (tbl_questions) to Django dynamic_forms.'

    def add_arguments(self, parser):
        parser.add_argument('--company-code', required=True,
                            help='Target company code (e.g., JIVO_OIL)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Preview without writing')

    def handle(self, *args, **options):
        company = Company.objects.get(code=options['company_code'])
        dry_run = options['dry_run']

        php_conn = psycopg2.connect(...)
        cursor = php_conn.cursor()

        # Get distinct forms
        cursor.execute("""
            SELECT DISTINCT formid,
                   MIN(question) as first_question
            FROM tbl_questions
            GROUP BY formid
            ORDER BY formid
        """)
        form_ids = cursor.fetchall()

        TYPE_MAP = {
            'text': FieldType.TEXT,
            'integer': FieldType.INTEGER,
            'image': FieldType.IMAGE,
            'textarea': FieldType.TEXTAREA,
            'select': FieldType.SELECT,
        }

        for formid, _ in form_ids:
            # Get form questions
            cursor.execute("""
                SELECT id, question, type, "order"
                FROM tbl_questions
                WHERE formid = %s
                ORDER BY "order"
            """, (formid,))
            questions = cursor.fetchall()

            form_title = f"Form {formid}"  # Update with real titles
            self.stdout.write(f"Form {formid}: {len(questions)} fields")

            if dry_run:
                for q_id, question, q_type, order in questions:
                    self.stdout.write(f"  [{order}] {question} ({q_type})")
                continue

            # Create Form
            form, created = Form.objects.get_or_create(
                company=company,
                title=form_title,
                defaults={
                    'description': f'Migrated from PHP form {formid}',
                    'is_published': True,
                    'requires_approval': True,
                },
            )

            # Create Fields
            for q_id, question, q_type, order in questions:
                field_type = TYPE_MAP.get(q_type, FieldType.TEXT)

                FormField.objects.get_or_create(
                    form=form,
                    order=order,
                    defaults={
                        'label': question,
                        'field_type': field_type,
                        'is_required': True,
                    },
                )

            # Store mapping
            FormMapping.objects.update_or_create(
                php_formid=formid,
                defaults={'django_form': form},
            )

            self.stdout.write(self.style.SUCCESS(
                f"  {'Created' if created else 'Updated'}: {form_title}"))

        php_conn.close()
```

### Temporary Mapping

```python
class FormMapping(models.Model):
    php_formid = models.IntegerField(unique=True)
    django_form = models.ForeignKey('dynamic_forms.Form', on_delete=models.CASCADE)

    class Meta:
        db_table = 'migration_form_mapping'
```

---

## Phase 3: Form Submissions (Responses)

### Source: `tbl_responses`

```sql
SELECT id, formid, questionid, answer, imagepath, login,
       timestamp, approved, closed, received_back,
       out_movement, gate_return_receiver
FROM tbl_responses
ORDER BY timestamp;
```

### Migration Command

```python
# management/commands/migrate_submissions.py

class Command(BaseCommand):
    help = 'Migrate PHP form responses to Django FormSubmission + FieldResponse.'

    def add_arguments(self, parser):
        parser.add_argument('--company-code', required=True)
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        company = Company.objects.get(code=options['company_code'])
        batch_size = options['batch_size']

        # Load mappings
        user_map = {m.php_login: m.django_user
                    for m in UserMapping.objects.select_related('django_user')}
        form_map = {m.php_formid: m.django_form
                    for m in FormMapping.objects.select_related('django_form')}

        php_conn = psycopg2.connect(...)
        cursor = php_conn.cursor()

        # Get unique submissions (grouped by formid + login + timestamp)
        cursor.execute("""
            SELECT formid, login, timestamp, approved, closed,
                   MIN(id) as first_response_id
            FROM tbl_responses
            GROUP BY formid, login, timestamp, approved, closed
            ORDER BY timestamp
        """)
        submissions = cursor.fetchall()

        STATUS_MAP = {
            None: SubmissionStatus.PENDING,
            '': SubmissionStatus.PENDING,
            'approved': SubmissionStatus.APPROVED,
            'rejected': SubmissionStatus.REJECTED,
        }

        migrated = 0
        skipped = 0

        for formid, login, timestamp, approved, closed, _ in submissions:
            django_form = form_map.get(formid)
            django_user = user_map.get(login)

            if not django_form or not django_user:
                skipped += 1
                continue

            if options['dry_run']:
                self.stdout.write(f"  Would migrate: form={formid} user={login} ts={timestamp}")
                continue

            # Determine status
            status = STATUS_MAP.get(approved, SubmissionStatus.PENDING)
            if closed:
                status = SubmissionStatus.CLOSED

            # Create submission
            submission = FormSubmission.objects.create(
                form=django_form,
                company=company,
                submitted_by=django_user,
                status=status,
                is_closed=bool(closed),
                created_by=django_user,
            )
            # Override auto timestamp
            FormSubmission.objects.filter(id=submission.id).update(
                created_at=timestamp
            )

            # Get individual field responses
            cursor2 = php_conn.cursor()
            cursor2.execute("""
                SELECT questionid, answer, imagepath
                FROM tbl_responses
                WHERE formid = %s AND login = %s AND timestamp = %s
            """, (formid, login, timestamp))

            for questionid, answer, imagepath in cursor2.fetchall():
                # Find matching Django field
                field = FormField.objects.filter(
                    form=django_form,
                    order=questionid,  # PHP questionid maps to field order
                ).first()

                if not field:
                    continue

                field_response = FieldResponse(
                    submission=submission,
                    field=field,
                    value=answer or '',
                    created_by=django_user,
                )

                # Handle image migration
                if imagepath:
                    source_path = f"/var/www/site2/mobile/uploads/{imagepath}"
                    if os.path.exists(source_path):
                        with open(source_path, 'rb') as f:
                            field_response.image.save(
                                os.path.basename(imagepath),
                                ContentFile(f.read()),
                                save=False,
                            )

                field_response.save()

            migrated += 1

            if migrated % batch_size == 0:
                self.stdout.write(f"  Progress: {migrated} migrated...")

        self.stdout.write(self.style.SUCCESS(
            f"Done. Migrated: {migrated}, Skipped: {skipped}"))
        php_conn.close()
```

---

## Phase 4: Form Permissions

### Source: `tbl_permissions`

```sql
SELECT formid, login, access FROM tbl_permissions;
```

### Migration

```python
# In migrate_submissions.py or separate command

cursor.execute("SELECT formid, login, access FROM tbl_permissions")
for formid, login, access in cursor.fetchall():
    django_form = form_map.get(formid)
    django_user = user_map.get(login)

    if django_form and django_user:
        FormPermission.objects.get_or_create(
            form=django_form,
            user=django_user,
            defaults={
                'can_submit': True,
                'can_approve': False,  # Set manually per business rules
            },
        )
```

### Source: `tbl_pageperm`

```sql
SELECT login, page, link FROM tbl_pageperm;
```

### Page → Group Mapping

| PHP Page | Django Group |
|---|---|
| `utility_approval.php` | `form_admin` |
| `reporting.php` | `report_viewer` |
| `dock_invoice.php` | `docking_operator` |
| `gate_invoice2.php` | `material_operator` |
| `set_out_movement.php` | `material_operator` |
| `pg_client.php` | Superuser only |
| `sql_client.php` | Superuser only |

```python
PAGE_GROUP_MAP = {
    'utility_approval': 'form_admin',
    'reporting': 'report_viewer',
    'dock_invoice': 'docking_operator',
    'gate_invoice': 'material_operator',
    'set_out_movement': 'material_operator',
}

for login, page, link in cursor.fetchall():
    django_user = user_map.get(login)
    group_name = PAGE_GROUP_MAP.get(page.replace('.php', ''))

    if django_user and group_name:
        group = Group.objects.get(name=group_name)
        django_user.groups.add(group)
```

---

## Phase 5: Image Files

### Copy uploaded images from PHP to Django media

```bash
# On server, copy PHP uploads to Django media directory
rsync -av /var/www/site2/mobile/uploads/ \
          /path/to/factory_app_v2/media/forms/migrated/

# Set permissions
chown -R www-data:www-data /path/to/factory_app_v2/media/forms/migrated/
chmod -R 755 /path/to/factory_app_v2/media/forms/migrated/
```

The migration command (Phase 3) copies individual images to the proper upload path (`forms/responses/YYYY/MM/DD/`) using Django's `ContentFile`.

---

## Phase 6: Validation

### Run validation checks after migration

```python
# management/commands/validate_migration.py

class Command(BaseCommand):
    help = 'Validate data integrity after migration.'

    def handle(self, *args, **options):
        php_conn = psycopg2.connect(...)
        cursor = php_conn.cursor()

        # 1. Count comparison
        cursor.execute("SELECT COUNT(DISTINCT (formid, login, timestamp)) FROM tbl_responses")
        php_count = cursor.fetchone()[0]
        django_count = FormSubmission.objects.count()

        self.stdout.write(f"Submissions: PHP={php_count}, Django={django_count}")
        if php_count != django_count:
            self.stdout.write(self.style.WARNING(
                f"  MISMATCH: {php_count - django_count} submissions missing"))

        # 2. Form count
        cursor.execute("SELECT COUNT(DISTINCT formid) FROM tbl_questions")
        php_forms = cursor.fetchone()[0]
        django_forms = Form.objects.count()
        self.stdout.write(f"Forms: PHP={php_forms}, Django={django_forms}")

        # 3. User mapping coverage
        cursor.execute("SELECT COUNT(DISTINCT login) FROM tbl_responses")
        php_users = cursor.fetchone()[0]
        mapped_users = UserMapping.objects.count()
        self.stdout.write(f"Users: PHP={php_users}, Mapped={mapped_users}")

        # 4. Image integrity
        missing_images = FieldResponse.objects.exclude(
            image=''
        ).exclude(image=None)
        broken = 0
        for fr in missing_images:
            if fr.image and not os.path.exists(fr.image.path):
                broken += 1
        self.stdout.write(f"Images with broken paths: {broken}")

        # 5. Status distribution
        self.stdout.write("\nStatus distribution:")
        for status, count in FormSubmission.objects.values_list('status').annotate(
            c=Count('id')
        ):
            self.stdout.write(f"  {status}: {count}")

        php_conn.close()
```

---

## Migration Execution Order

```
Step 1:  python manage.py migrate                      # Create all tables
Step 2:  python manage.py setup_utility_groups          # Create groups/permissions
Step 3:  python manage.py migrate_utility_users --company-code JIVO_OIL
Step 4:  python manage.py migrate_forms --company-code JIVO_OIL --dry-run
Step 5:  python manage.py migrate_forms --company-code JIVO_OIL
Step 6:  python manage.py migrate_submissions --company-code JIVO_OIL --dry-run
Step 7:  python manage.py migrate_submissions --company-code JIVO_OIL
Step 8:  python manage.py validate_migration
Step 9:  # Manual review of validation output
Step 10: # Remove temporary mapping models after cutover
```

---

## Rollback Plan

If migration fails or data is incorrect:

```sql
-- Delete migrated data (in order, respecting FK constraints)
DELETE FROM dynamic_forms_fieldresponse WHERE submission_id IN (
    SELECT id FROM dynamic_forms_formsubmission WHERE company_id = <company_id>
);
DELETE FROM dynamic_forms_formsubmission WHERE company_id = <company_id>;
DELETE FROM dynamic_forms_formpermission WHERE form_id IN (
    SELECT id FROM dynamic_forms_form WHERE company_id = <company_id>
);
DELETE FROM dynamic_forms_formfield WHERE form_id IN (
    SELECT id FROM dynamic_forms_form WHERE company_id = <company_id>
);
DELETE FROM dynamic_forms_form WHERE company_id = <company_id>;

-- Drop temporary mapping tables
DROP TABLE IF EXISTS migration_user_mapping;
DROP TABLE IF EXISTS migration_form_mapping;
```

---

## Post-Migration Cleanup

After successful cutover and validation:

1. Remove `UserMapping` and `FormMapping` models
2. Remove migration management commands
3. Drop temporary tables: `migration_user_mapping`, `migration_form_mapping`
4. Archive PHP `uploads/` directory
5. Decommission PHP endpoints
6. Update DNS/routing to point mobile app to Django APIs
