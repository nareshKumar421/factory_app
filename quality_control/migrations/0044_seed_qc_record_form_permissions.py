"""Grant the QC record-form permissions and seed the NMW water form.

Two jobs, both idempotent:

1. Put ``can_view_qc_records`` / ``can_fill_qc_records`` /
   ``can_approve_qc_records`` on the existing QC groups, so the Documents
   page is reachable without an administrator assigning them by hand.
2. Seed the "NMW DAILY WATER MONITORING RECORD" form -- the paper sheet QA
   fills for borewell and treated water -- for every active company, so
   there is a working form to fill on day one.

The seed is skipped for any company that already has a form with this code,
so re-running never duplicates or overwrites an edited form.
"""

from django.db import migrations

VIEW = "can_view_qc_records"
FILL = "can_fill_qc_records"
APPROVE = "can_approve_qc_records"

MANAGER_GROUPS = ["qc_manager", "QC Manager", "factory head"]
FILLER_GROUPS = ["qc_chemist", "qc_store"]

# Provisional code -- the printed sheet in hand shows a revision but no
# document code. Edit it on the form once the controlled code is confirmed.
NMW_CODE = "NMW-DAILY-WATER"

AGREEABLE = ["Agreeable", "Not Agreeable"]
ODOUR = ["No off Odour", "Off Odour"]
APPEARANCE = ["Clear", "Not Clear"]

EVERY_2H = "Every Startup / Every 2 Hours"
CLEAR_SPEC = "Clear without any suspended particulates and extraneous matter."

# (sr_no, name, frequency, specification, unit, value_type, min, max, choices)
COMMON = [
    ("1", "Taste", EVERY_2H, "Agreeable", "", "CHOICE", None, None, AGREEABLE),
    ("2", "Odour", EVERY_2H, "No off Odour", "", "CHOICE", None, None, ODOUR),
    ("3", "Appearance", EVERY_2H, CLEAR_SPEC, "", "CHOICE", None, None, APPEARANCE),
    ("4", "pH", EVERY_2H, "6.5 - 8.5", "", "NUMBER", "6.5", "8.5", []),
    ("5", "TDS", EVERY_2H, "150 - 750 ppm", "ppm", "NUMBER", "150", "750", []),
    ("6", "Turbidity", EVERY_2H, "Max 2.0 NTU", "NTU", "NUMBER", None, "2.0", []),
]

BOREWELL_EXTRA = [
    ("7", "Alkalinity", EVERY_2H, "75 - 400 mg/l", "mg/l", "NUMBER", "75", "400", []),
    ("8", "Total Hardness", EVERY_2H, "To be tested", "mg/l", "NUMBER", None, None, []),
    ("9", "Calcium Hardness", EVERY_2H, "To be tested", "mg/l", "NUMBER", None, None, []),
    ("10", "Magnesium Hardness", EVERY_2H, "To be tested", "mg/l", "NUMBER", None, None, []),
    ("11", "Calcium", EVERY_2H, "Max 100 mg/l", "mg/l", "NUMBER", None, "100", []),
    ("12", "Magnesium", EVERY_2H, "Max 50 mg/l", "mg/l", "NUMBER", None, "50", []),
    ("13", "Chloride", EVERY_2H, "Max 200 mg/l", "mg/l", "NUMBER", None, "200", []),
]

SECTIONS = [
    ("Borewell Water", COMMON + BOREWELL_EXTRA),
    ("Treated Water", COMMON),
]


def _grant(apps, db):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    app_config = global_apps.get_app_config("quality_control")
    if app_config.models_module is not None:
        create_permissions(app_config, verbosity=0, using=db)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    def perm(codename):
        return (
            Permission.objects.using(db)
            .filter(codename=codename, content_type__app_label="quality_control")
            .first()
        )

    view, fill, approve = perm(VIEW), perm(FILL), perm(APPROVE)
    if not all([view, fill, approve]):
        return

    for name in MANAGER_GROUPS:
        group = Group.objects.using(db).filter(name=name).first()
        if group:
            group.permissions.add(view, fill, approve)

    for name in FILLER_GROUPS:
        group = Group.objects.using(db).filter(name=name).first()
        if group:
            group.permissions.add(view, fill)


def _seed(apps, db):
    from datetime import date

    Company = apps.get_model("company", "Company")
    RecordTemplate = apps.get_model("quality_control", "RecordTemplate")
    RecordTemplateSection = apps.get_model("quality_control", "RecordTemplateSection")
    RecordTemplateParameter = apps.get_model(
        "quality_control", "RecordTemplateParameter"
    )

    for company in Company.objects.using(db).filter(is_active=True):
        if (
            RecordTemplate.objects.using(db)
            .filter(company=company, document_code=NMW_CODE)
            .exists()
        ):
            continue

        template = RecordTemplate.objects.using(db).create(
            company=company,
            document_code=NMW_CODE,
            title="NMW DAILY WATER MONITORING RECORD",
            organisation="JIVO WELLNESS PVT.LTD.",
            revision_number="01",
            revision_date=date(2026, 5, 21),
            classification="Business Confidential",
            description=(
                "Daily borewell and treated water monitoring, recorded at "
                "startup and every 2 hours."
            ),
        )

        for s_index, (section_title, parameters) in enumerate(SECTIONS):
            section = RecordTemplateSection.objects.using(db).create(
                template=template, sequence=s_index, title=section_title
            )
            RecordTemplateParameter.objects.using(db).bulk_create(
                [
                    RecordTemplateParameter(
                        section=section,
                        sequence=p_index,
                        sr_no=sr_no,
                        name=name,
                        frequency=frequency,
                        specification=specification,
                        unit=unit,
                        value_type=value_type,
                        min_value=min_value,
                        max_value=max_value,
                        allowed_values=choices,
                    )
                    for p_index, (
                        sr_no,
                        name,
                        frequency,
                        specification,
                        unit,
                        value_type,
                        min_value,
                        max_value,
                        choices,
                    ) in enumerate(parameters)
                ]
            )


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    _grant(apps, db)
    _seed(apps, db)


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    RecordTemplate = apps.get_model("quality_control", "RecordTemplate")

    perms = list(
        Permission.objects.using(db).filter(
            codename__in=[VIEW, FILL, APPROVE],
            content_type__app_label="quality_control",
        )
    )
    if perms:
        for group in Group.objects.using(db).filter(
            name__in=MANAGER_GROUPS + FILLER_GROUPS
        ):
            group.permissions.remove(*perms)

    # Only remove a seeded form nobody has filled yet.
    RecordTemplate.objects.using(db).filter(
        document_code=NMW_CODE, records__isnull=True
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0043_recordtemplate_qcrecord_recordtemplatesection_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("company", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
