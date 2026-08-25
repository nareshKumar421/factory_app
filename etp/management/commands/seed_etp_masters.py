"""
Seed the ETP / STP masters from the paper registers so the module is usable on
day one.

Everything written here is ordinary master data the plant team can edit from the
Settings screen afterwards — the command only saves them the first hour of
typing. It is idempotent: re-running it adds what is missing and leaves edited
rows alone.

Usage::

    python manage.py seed_etp_masters
    python manage.py seed_etp_masters --skip-people   # masters only, no names

Notes on the source forms:

* The STP form records HYPO and POLY in grams while the ETP form records HYPO in
  litres. A chemical carries ONE default unit here and each entry stores its own
  unit, so the operator can switch the unit on the line when it differs.
* The monitoring limits seeded below are the CPCB inland-discharge norms
  (pH 6.5–8.5, TDS ≤ 2100 ppm, DO ≥ 2 ppm). Correct them against the plant's
  own consent conditions if those are tighter.
* The back-washing register covers the sand / carbon filters on the water side,
  so it is seeded under a WTP plant rather than under the ETP.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from company.models import Company
from etp.constants import (
    DEFAULT_PRINT_DOCUMENTS,
    CalibrationFrequency,
    ChemicalUom,
    MonitoringStage,
    OptionCategory,
    PlantType,
    SpecValidationType,
    StaffRole,
)
from etp.models import (
    BackwashEquipment,
    CalibrationInstrument,
    CalibrationPoint,
    EtpPrintDocument,
    MonitoringParameter,
    PlantChemical,
    PlantOption,
    PlantStaff,
    TreatmentPlant,
)

# code -> (name, type, location, sequence)
PLANTS = [
    ("ETP", "Effluent Treatment Plant", PlantType.ETP, "ETP Plant, Ganaur", 1),
    ("STP", "Sewage Treatment Plant", PlantType.STP, "STP Plant, Ganaur", 2),
    ("WTP", "Water Treatment Plant", PlantType.WTP, "Utility Block, Ganaur", 3),
]

#: Companies the Ganaur plants serve (skipped when a code does not exist).
PLANT_COMPANY_CODES = ["JIVO_OIL", "JIVO_BEVERAGES"]

# name, default uom, plant codes, sequence
CHEMICALS = [
    ("HYPO (Sodium Hypochlorite)", ChemicalUom.LTR, ["ETP", "STP"], 1),
    ("HCL (Hydrochloric Acid)", ChemicalUom.LTR, ["ETP"], 2),
    ("ALUM / PAC", ChemicalUom.KG, ["ETP", "STP"], 3),
    ("POLY (Polyelectrolyte)", ChemicalUom.GM, ["ETP", "STP"], 4),
    ("LIME / CAUSTIC", ChemicalUom.KG, ["ETP", "STP"], 5),
    ("DAP", ChemicalUom.KG, ["ETP"], 6),
    ("UREA", ChemicalUom.KG, ["ETP"], 7),
    ("JAGGERY", ChemicalUom.KG, ["ETP"], 8),
]

# stage, key, name, unit, min, max, spec text, validation
MONITORING_PARAMETERS = [
    (MonitoringStage.INFLUENT, "ph", "pH", "", "6.5", "8.5", "6.5-8.5", SpecValidationType.RANGE),
    (MonitoringStage.INFLUENT, "tds", "TDS", "ppm", None, "2100", "≤ 2100", SpecValidationType.MAX),
    (MonitoringStage.AERATION, "ph", "pH", "", "6.5", "8.5", "6.5-8.5", SpecValidationType.RANGE),
    (MonitoringStage.AERATION, "tds", "TDS", "ppm", None, "2100", "≤ 2100", SpecValidationType.MAX),
    (MonitoringStage.AERATION, "do", "DO", "ppm", "2.0", None, "≥ 2.0", SpecValidationType.MIN),
    (MonitoringStage.TREATED, "ph", "pH", "", "6.5", "8.5", "6.5-8.5", SpecValidationType.RANGE),
    (MonitoringStage.TREATED, "tds", "TDS", "ppm", None, "2100", "≤ 2100", SpecValidationType.MAX),
    (MonitoringStage.TREATED, "do", "DO", "ppm", "2.0", None, "≥ 2.0", SpecValidationType.MIN),
]

# category, labels (first one is the default)
OPTIONS = [
    (
        OptionCategory.SLUDGE_COLLECTION_MODE,
        ["Filter Press", "Manual", "Centrifuge / Decanter", "Sludge Pump"],
    ),
    (
        OptionCategory.SLUDGE_STORAGE_METHOD,
        ["Bag", "HDPE Bag", "Drum", "Covered Yard"],
    ),
    (
        OptionCategory.SLUDGE_DISPOSAL_MODE,
        ["Authorised Vendor", "Manure / Land Application", "Landfill"],
    ),
    (
        OptionCategory.CALIBRATION_ACTION,
        ["Nil", "Adjusted / Recalibrated", "Replaced", "Sent for External Calibration"],
    ),
]

# plant code, name, default minutes, sequence
BACKWASH_STEPS = [
    ("WTP", "Sand Filter Backwash", 10, 1),
    ("WTP", "Sand Filter Rinse", 5, 2),
    ("WTP", "Carbon Filter Backwash", 10, 3),
    ("WTP", "Carbon Filter Rinse", 5, 4),
]

# name, role
PEOPLE = [
    ("Anil", StaffRole.CHEMIST),
    ("Anurag", StaffRole.OPERATOR),
    ("Sumit", StaffRole.OPERATOR),
    ("Vishal", StaffRole.OPERATOR),
    ("Chotu", StaffRole.OPERATOR),
    ("Yogesh", StaffRole.SUPERVISOR),
]

#: The plant pH meter from the calibration record (CAL-FRM-08-03-00-01).
INSTRUMENT = {
    "equipment_name": "pH Meter",
    "equipment_id": "ETP-LE-01",
    "plant_code": "ETP",
    "location": "ETP Plant",
    "working_range": "0 - 14",
    "frequency": CalibrationFrequency.WEEKLY,
    "tolerance": Decimal("0.200"),
    "standard_make_model": "ADVIT PH14",
    "points": [Decimal("4.000"), Decimal("7.000"), Decimal("10.010")],
}


class Command(BaseCommand):
    help = "Seed ETP / STP plants, chemicals, monitoring parameters and dropdowns."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-people",
            action="store_true",
            help="Do not seed the operator / chemist names from the registers.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        plants = self._seed_plants()
        self._seed_chemicals(plants)
        self._seed_parameters(plants)
        self._seed_options()
        self._seed_backwash(plants)
        if not options["skip_people"]:
            self._seed_people()
        self._seed_instrument(plants)
        self._seed_print_documents()
        self.stdout.write(self.style.SUCCESS("ETP / STP masters seeded."))

    # -- individual masters -------------------------------------------------

    def _seed_plants(self):
        companies = list(
            Company.objects.filter(code__in=PLANT_COMPANY_CODES, is_active=True)
        )
        plants = {}
        for code, name, plant_type, location, sequence in PLANTS:
            plant, created = TreatmentPlant.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "plant_type": plant_type,
                    "location": location,
                    "sequence": sequence,
                },
            )
            if created and companies:
                plant.companies.set(companies)
            plants[code] = plant
            self._report("plant", plant.code, created)
        return plants

    def _seed_chemicals(self, plants):
        for name, uom, plant_codes, sequence in CHEMICALS:
            chemical, created = PlantChemical.objects.get_or_create(
                name=name,
                defaults={"default_uom": uom, "sequence": sequence},
            )
            if created:
                chemical.plants.set(
                    [plants[code] for code in plant_codes if code in plants]
                )
            self._report("chemical", name, created)

    def _seed_parameters(self, plants):
        for plant_code in ("ETP", "STP"):
            plant = plants.get(plant_code)
            if plant is None:
                continue
            for index, row in enumerate(MONITORING_PARAMETERS, start=1):
                stage, key, label, unit, low, high, spec, validation = row
                _, created = MonitoringParameter.objects.get_or_create(
                    plant=plant,
                    stage=stage,
                    parameter_key=key,
                    defaults={
                        "parameter_name": label,
                        "unit": unit,
                        "min_value": Decimal(low) if low else None,
                        "max_value": Decimal(high) if high else None,
                        "specification_text": spec,
                        "validation_type": validation,
                        "sequence": index,
                    },
                )
                self._report(
                    "parameter", f"{plant_code} {stage} {label}", created
                )

    def _seed_options(self):
        for category, labels in OPTIONS:
            for index, label in enumerate(labels, start=1):
                _, created = PlantOption.objects.get_or_create(
                    category=category,
                    label=label,
                    defaults={"sequence": index, "is_default": index == 1},
                )
                self._report("option", f"{category}: {label}", created)

    def _seed_backwash(self, plants):
        for plant_code, name, minutes, sequence in BACKWASH_STEPS:
            plant = plants.get(plant_code)
            if plant is None:
                continue
            _, created = BackwashEquipment.objects.get_or_create(
                plant=plant,
                name=name,
                defaults={
                    "default_duration_minutes": minutes,
                    "sequence": sequence,
                },
            )
            self._report("backwash step", name, created)

    def _seed_people(self):
        for index, (name, role) in enumerate(PEOPLE, start=1):
            _, created = PlantStaff.objects.get_or_create(
                name=name, role=role, defaults={"sequence": index}
            )
            self._report("person", f"{name} ({role})", created)

    def _seed_instrument(self, plants):
        instrument, created = CalibrationInstrument.objects.get_or_create(
            equipment_id=INSTRUMENT["equipment_id"],
            defaults={
                "equipment_name": INSTRUMENT["equipment_name"],
                "plant": plants.get(INSTRUMENT["plant_code"]),
                "location": INSTRUMENT["location"],
                "working_range": INSTRUMENT["working_range"],
                "frequency": INSTRUMENT["frequency"],
                "tolerance": INSTRUMENT["tolerance"],
                "standard_make_model": INSTRUMENT["standard_make_model"],
            },
        )
        self._report("instrument", instrument.equipment_id, created)
        for index, value in enumerate(INSTRUMENT["points"], start=1):
            _, point_created = CalibrationPoint.objects.get_or_create(
                instrument=instrument,
                actual_value=value,
                defaults={"sequence": index},
            )
            self._report("calibration point", str(value), point_created)

    def _seed_print_documents(self):
        """The document number each register prints, as one factory-wide row.

        Held in the database from here on: correcting a code or bumping a
        revision is a Settings edit, not a release.
        """
        today = timezone.localdate()
        for key, (form_name, code, revision) in DEFAULT_PRINT_DOCUMENTS.items():
            _, created = EtpPrintDocument.objects.get_or_create(
                document_key=key,
                company=None,
                defaults={
                    "form_name": form_name,
                    "document_code": code,
                    "revision": revision,
                    "issue_date": today,
                },
            )
            self._report("print document", f"{key} {code}", created)

    def _report(self, kind, label, created):
        if created:
            self.stdout.write(self.style.SUCCESS(f"+ {kind}: {label}"))
        else:
            self.stdout.write(f"= {kind}: {label} (already present)")
