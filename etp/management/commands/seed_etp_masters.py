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

* The STP sheet records HYPO, POLY and LIME / CAUSTIC in GRAMS while the ETP
  sheet records HYPO in litres and LIME / CAUSTIC in kg. A chemical carries ONE
  default unit here and each entry stores its own, so the operator switches the
  unit on the line for whichever sheet disagrees with the default. Note the STP
  sheet then TOTALS its grams column as litres / kg at the foot of the month,
  which ``EtpSummaryAPI`` does not do -- it reports each unit as recorded.
* The monitoring limits are the plant's HSPCB consent conditions, applied at the
  discharge point. See ``MONITORING_PARAMETERS`` for which stage carries which
  check and why.
* The back-washing register covers the sand / carbon filters on the water side,
  so it is seeded under a WTP plant rather than under the ETP.
* Everything here was re-checked in Sept 2026 against photographs of the filled
  registers. The one thing still missing is each plant's ``capacity_kld``
  and ``consent_number``, which appear on no form -- QA has to supply those.
"""

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max

from company.models import Company
from etp.constants import (
    DEFAULT_PRINT_DOCUMENTS,
    CalibrationFrequency,
    ChemicalUom,
    MonitoringStage,
    OptionCategory,
    PlantType,
    PrintDocumentKey,
    SpecValidationType,
    StaffRole,
)
from etp.models import (
    BackwashEquipment,
    CalibrationInstrument,
    CalibrationPoint,
    DailyPlantLog,
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
#
# The columns each sheet actually prints (Apr 2026 ETP sheet, Mar 2026 STP sheet):
#
#   ETP: HYPO | HCL | POLY | LIME/CAUSTIC | DAP | UREA
#   STP: HYPO | ALUM/PAC | POLY | LIME/CAUSTIC
#
# ALUM / PAC is struck through on the ETP sheet, so it is dosed on the STP only.
# JAGGERY has no column on either sheet -- it shows up only as a stock-receipt
# note in REMARKS ("33kg Jeggery Recived 28/04/2026"), so it is not a master row.
#
# The sequence below prints both sheets in their paper column order.
CHEMICALS = [
    ("HYPO (Sodium Hypochlorite)", ChemicalUom.LTR, ["ETP", "STP"], 1),
    ("HCL (Hydrochloric Acid)", ChemicalUom.LTR, ["ETP"], 2),
    ("ALUM / PAC", ChemicalUom.KG, ["STP"], 3),
    ("POLY (Polyelectrolyte)", ChemicalUom.GM, ["ETP", "STP"], 4),
    ("LIME / CAUSTIC", ChemicalUom.KG, ["ETP", "STP"], 5),
    ("DAP", ChemicalUom.KG, ["ETP"], 6),
    ("UREA", ChemicalUom.KG, ["ETP"], 7),
]

#: Short names a person may already have typed into Settings -> the canonical
#: name in CHEMICALS above. Used by ``--fix-existing`` to ADOPT an existing row
#: (rename it, correct its unit) instead of leaving it beside a seeded twin: the
#: register would otherwise print two columns for the same chemical, because
#: ``PlantChemical.name`` is unique and the seeder matches on it.
CHEMICAL_ALIASES = {
    "HCL": "HCL (Hydrochloric Acid)",
    "HYPO": "HYPO (Sodium Hypochlorite)",
    "SODIUM HYPOCHLORITE": "HYPO (Sodium Hypochlorite)",
    "POLY": "POLY (Polyelectrolyte)",
    "POLYELECTROLYTE": "POLY (Polyelectrolyte)",
    "ALUM": "ALUM / PAC",
    "PAC": "ALUM / PAC",
    "ALUM/PAC": "ALUM / PAC",
    "LIME": "LIME / CAUSTIC",
    "CAUSTIC": "LIME / CAUSTIC",
    "LIME/CAUSTIC": "LIME / CAUSTIC",
}

# stage, key, name, unit, min, max, spec text, validation
#
# The parameter SET is confirmed against the filled ETP On Line Monitoring Record
# (04/07/26): influent pH + TDS, aeration pH + TDS + DO, treated pH + TDS + DO,
# all in ppm, sampled every two hours.
#
# The sheet itself prints no specification column, so the LIMITS come from the
# plant's HSPCB consent as printed on the ETP Log Sheet (QA-FRM-08-03-00-17):
# pH 6.5-8.5, TDS < 1800 mg/l, DO > 1 mg/l. Those are discharge conditions, so
# they are applied at the TREATED stage -- the point of discharge.
#
# Influent is incoming effluent, so nothing there is "out of spec"; aeration TDS
# is likewise watched rather than limited. Both are recorded with no numeric
# check. Aeration DO keeps a >= 2.0 ppm floor: that is the operational target the
# plant runs the blowers to, and every logged reading sits at 2.0-2.6.
MONITORING_PARAMETERS = [
    (MonitoringStage.INFLUENT, "ph", "pH", "", None, None, "Record only", SpecValidationType.NONE),
    (MonitoringStage.INFLUENT, "tds", "TDS", "ppm", None, None, "Record only", SpecValidationType.NONE),
    (MonitoringStage.AERATION, "ph", "pH", "", "6.5", "8.5", "6.5-8.5", SpecValidationType.RANGE),
    (MonitoringStage.AERATION, "tds", "TDS", "ppm", None, None, "Record only", SpecValidationType.NONE),
    (MonitoringStage.AERATION, "do", "DO", "ppm", "2.0", None, "≥ 2.0", SpecValidationType.MIN),
    (MonitoringStage.TREATED, "ph", "pH", "", "6.5", "8.5", "6.5-8.5", SpecValidationType.RANGE),
    (MonitoringStage.TREATED, "tds", "TDS", "ppm", None, "1800", "< 1800", SpecValidationType.MAX),
    (MonitoringStage.TREATED, "do", "DO", "ppm", "1.0", None, "> 1.0", SpecValidationType.MIN),
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
# Yogesh appears twice on purpose: he countersigns "Supervisor Signature" on the
# sludge register but signs the "Chemist Sign" column on the ETP daily record, and
# the signature dropdowns filter by role.
PEOPLE = [
    ("Anil", StaffRole.CHEMIST),
    ("Anurag", StaffRole.OPERATOR),
    ("Sumit", StaffRole.OPERATOR),
    ("Vishal", StaffRole.OPERATOR),
    ("Chotu", StaffRole.OPERATOR),
    ("Yogesh", StaffRole.SUPERVISOR),
    ("Yogesh", StaffRole.CHEMIST),
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
        parser.add_argument(
            "--fix-existing",
            action="store_true",
            help=(
                "Also correct rows an earlier run already seeded: rewrite the "
                "print-document codes / revisions to the verified values, move "
                "ALUM / PAC to the STP only and retire JAGGERY. Skips anything a "
                "person has since edited by hand."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        plants = self._seed_plants()
        if options["fix_existing"]:
            # Must run BEFORE _seed_chemicals, so the adopted row is the one
            # get_or_create then matches on rather than a freshly created twin.
            self._adopt_hand_typed_chemicals()
        self._seed_chemicals(plants)
        self._seed_parameters(plants)
        self._seed_options()
        self._seed_backwash(plants)
        if not options["skip_people"]:
            self._seed_people()
        self._seed_instrument(plants)
        self._seed_print_documents()
        if options["fix_existing"]:
            self._fix_existing(plants)
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
        revision is a Settings edit, not a release. The revision and issue date
        come off the photographed originals -- see ``DEFAULT_PRINT_DOCUMENTS``.
        """
        for key, (form_name, code, revision, issued) in DEFAULT_PRINT_DOCUMENTS.items():
            _, created = EtpPrintDocument.objects.get_or_create(
                document_key=key,
                company=None,
                defaults={
                    "form_name": form_name,
                    "document_code": code,
                    "revision": revision,
                    "issue_date": date.fromisoformat(issued) if issued else None,
                },
            )
            self._report("print document", f"{key} {code or '(code blank)'}", created)

    # -- corrections to an already-seeded database ---------------------------

    def _adopt_hand_typed_chemicals(self):
        """Rename a short hand-typed chemical onto its canonical name.

        A factory that started entering masters through the Settings screen
        before this command was ever run has rows like ``HCL`` where the seeder
        wants ``HCL (Hydrochloric Acid)``. Renaming first means the row is
        ADOPTED -- its consumption history, plants and sequence stay attached --
        instead of sitting next to a seeded duplicate.

        The default unit is corrected at the same time: it is only a prefill (each
        line stores its own unit), and the hand-typed ones disagree with the
        sheets, e.g. HCL typed as gm where the ETP sheet records litres.
        """
        canonical_uoms = {name: uom for name, uom, _plants, _seq in CHEMICALS}
        for chemical in PlantChemical.objects.all():
            target = CHEMICAL_ALIASES.get(chemical.name.strip().upper())
            if target is None or chemical.name == target:
                continue
            if PlantChemical.objects.filter(name=target).exists():
                self.stdout.write(
                    self.style.WARNING(
                        f"! chemical: '{chemical.name}' and '{target}' both exist"
                        " -- merge them by hand, nothing renamed"
                    )
                )
                continue
            was_name, was_uom = chemical.name, chemical.default_uom
            chemical.name = target
            chemical.default_uom = canonical_uoms.get(target, chemical.default_uom)
            chemical.save(update_fields=["name", "default_uom"])
            note = f"* chemical: '{was_name}' adopted as '{target}'"
            if was_uom != chemical.default_uom:
                note += f" (default unit {was_uom} -> {chemical.default_uom})"
            self.stdout.write(self.style.WARNING(note))

        unknown = [
            c.name
            for c in PlantChemical.objects.filter(is_active=True)
            if c.name not in canonical_uoms
        ]
        if unknown:
            self.stdout.write(
                self.style.WARNING(
                    "! chemical: not on either consumption sheet, left untouched -- "
                    + ", ".join(sorted(unknown))
                    + ". Confirm with QA whether the plant really doses these."
                )
            )

    def _fix_plant_details(self, plants):
        """Fill in a plant row somebody created by hand with only a code.

        ``_seed_plants`` matches on ``code``, so a hand-made row keeps whatever
        thin values it was typed with. Only genuinely empty fields are filled --
        a name or location a person chose is never overwritten.
        """
        wanted = {code: (name, location) for code, name, _t, location, _s in PLANTS}
        for code, plant in plants.items():
            name, location = wanted.get(code, (None, None))
            changed = []
            if name and plant.name.strip() in ("", code):
                plant.name = name
                changed.append("name")
            if location and not plant.location.strip():
                plant.location = location
                changed.append("location")
            if changed:
                plant.save(update_fields=changed)
                self.stdout.write(
                    self.style.WARNING(
                        f"* plant {code}: filled {', '.join(changed)} -> '{plant.name}'"
                    )
                )
            self._warn_on_capacity(plant)

    def _warn_on_capacity(self, plant):
        """Flag a design capacity that looks like litres typed into a KLD field."""
        if not plant.capacity_kld:
            return
        peak = (
            DailyPlantLog.objects.filter(plant=plant).aggregate(
                peak=Max("inlet_total")
            )["peak"]
            or Decimal("0")
        )
        # A design capacity is normally the same order as the daily flow. Three
        # orders of magnitude above the busiest day on record is a unit slip.
        if peak and plant.capacity_kld < peak * 1000:
            return
        if not peak and plant.capacity_kld < Decimal("1000"):
            return
        measured = f"busiest logged day is {peak} KL" if peak else "no daily logs yet"
        self.stdout.write(
            self.style.WARNING(
                f"! plant {plant.code}: capacity_kld = {plant.capacity_kld} looks like"
                f" litres/day typed into a kilolitres/day field ({measured})."
                " Correct it in Settings."
            )
        )

    def _fix_existing(self, plants):
        """Bring a database seeded by the first release onto the verified values.

        The seeders above are add-only, so a factory that already ran this command
        keeps whatever the first release guessed. These are the three places the
        guess turned out to be wrong once the filled registers were read.

        Rows a person has edited by hand are left alone: a print document only
        moves if it still holds the exact value the old release seeded.
        """
        stale_codes = {
            PrintDocumentKey.ETP_DAILY_RECORD: "QA-FRM-14-00-08-01",
            PrintDocumentKey.ETP_MONITORING_RECORD: "QA-FRM-14-00-08-02",
            PrintDocumentKey.ETP_CHEMICAL_CONSUMPTION: "QA-FRM-14-00-08-05",
            PrintDocumentKey.STP_CHEMICAL_CONSUMPTION: "QA-FRM-14-00-08-04",
            PrintDocumentKey.ETP_SLUDGE_GENERATION: "QA-FRM-14-00-08-06",
            PrintDocumentKey.ETP_BACKWASH_RECORD: "QA-FRM-14-09-00-03",
            PrintDocumentKey.ETP_CALIBRATION_RECORD: "CAL-FRM-08-03-00-01",
        }
        for key, (form_name, code, revision, issued) in DEFAULT_PRINT_DOCUMENTS.items():
            document = EtpPrintDocument.objects.filter(
                document_key=key, company=None
            ).first()
            if document is None:
                continue
            if document.document_code != stale_codes[key]:
                self.stdout.write(
                    f"~ print document: {key} left alone (hand-edited to "
                    f"'{document.document_code}')"
                )
                continue
            document.form_name = form_name
            document.document_code = code
            document.revision = revision
            document.issue_date = date.fromisoformat(issued) if issued else None
            document.save(
                update_fields=["form_name", "document_code", "revision", "issue_date"]
            )
            self.stdout.write(
                self.style.WARNING(
                    f"* print document: {key} -> '{code or '(blank)'}' rev {revision}"
                    f" / {issued}"
                )
            )

        self._fix_plant_details(plants)

        stp = plants.get("STP")
        alum = PlantChemical.objects.filter(name="ALUM / PAC").first()
        if alum is not None and stp is not None:
            alum.plants.set([stp])
            self.stdout.write(
                self.style.WARNING("* chemical: ALUM / PAC -> STP only")
            )

        jaggery = PlantChemical.objects.filter(name="JAGGERY", is_active=True).first()
        if jaggery is not None:
            # Deactivated, not deleted: consumption lines PROTECT the chemical, and
            # anything already logged against it stays readable.
            jaggery.is_active = False
            jaggery.save(update_fields=["is_active"])
            self.stdout.write(
                self.style.WARNING(
                    "* chemical: JAGGERY retired (no column on either sheet)"
                )
            )

    def _report(self, kind, label, created):
        if created:
            self.stdout.write(self.style.SUCCESS(f"+ {kind}: {label}"))
        else:
            self.stdout.write(f"= {kind}: {label} (already present)")
