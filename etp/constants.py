"""
Choice tables for the ETP / STP plant registers.

Everything here is a *fixed* vocabulary (the shape of the paper form). Anything
the plant team is expected to maintain themselves -- the plants, the chemical
columns, the monitoring parameters, the people who sign, the small dropdowns on
the sludge and calibration registers -- lives in a master TABLE instead (see
``etp.models``), so it can be edited from the Settings page without a code
change.
"""

from django.db import models


class PlantType(models.TextChoices):
    """What kind of treatment plant a register belongs to.

    ETP and STP are the two plants the Ganaur forms cover today; the rest are
    here so the same registers can be opened for the water-side plants (the
    back-washing register is really a WTP/RO record) without a migration.
    """

    ETP = "ETP", "ETP — Effluent Treatment Plant"
    STP = "STP", "STP — Sewage Treatment Plant"
    WTP = "WTP", "WTP — Water Treatment Plant"
    RO = "RO", "RO Plant"
    ZLD = "ZLD", "ZLD — Zero Liquid Discharge"
    OTHER = "OTHER", "Other"


class ChemicalUom(models.TextChoices):
    KG = "KG", "kg"
    GM = "GM", "gm"
    LTR = "LTR", "litre"
    ML = "ML", "ml"
    NOS = "NOS", "nos"


class MonitoringStage(models.TextChoices):
    """Where in the plant a monitored sample is drawn.

    The ETP on-line monitoring form groups its columns exactly this way:
    influent water / aeration water / treated effluent water.
    """

    INFLUENT = "INFLUENT", "Influent water"
    PRIMARY = "PRIMARY", "Primary / equalisation"
    AERATION = "AERATION", "Aeration water"
    SECONDARY = "SECONDARY", "Secondary clarifier"
    TREATED = "TREATED", "Treated effluent water"
    OTHER = "OTHER", "Other"


class SpecValidationType(models.TextChoices):
    """How a monitoring parameter's limits are checked."""

    RANGE = "RANGE", "Range (min–max)"
    MIN = "MIN", "Minimum only"
    MAX = "MAX", "Maximum only"
    NONE = "NONE", "No numeric check"


class CalibrationFrequency(models.TextChoices):
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    FORTNIGHTLY = "FORTNIGHTLY", "Fortnightly"
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"
    HALF_YEARLY = "HALF_YEARLY", "Half yearly"
    YEARLY = "YEARLY", "Yearly"


#: Days added to a calibration date to get the next due date.
CALIBRATION_FREQUENCY_DAYS = {
    CalibrationFrequency.DAILY: 1,
    CalibrationFrequency.WEEKLY: 7,
    CalibrationFrequency.FORTNIGHTLY: 14,
    CalibrationFrequency.MONTHLY: 30,
    CalibrationFrequency.QUARTERLY: 91,
    CalibrationFrequency.HALF_YEARLY: 182,
    CalibrationFrequency.YEARLY: 365,
}


class StaffRole(models.TextChoices):
    """Why a person appears in a signature dropdown.

    Plant operators and chemists sign the paper registers by hand and most of
    them have no login, so the signature fields point at this small master list
    rather than at application users. Who *typed* the entry is still recorded
    separately (``created_by``).
    """

    OPERATOR = "OPERATOR", "Operator"
    CHEMIST = "CHEMIST", "Chemist"
    SUPERVISOR = "SUPERVISOR", "Supervisor"
    QAM = "QAM", "QA Manager"
    OTHER = "OTHER", "Other"


class OptionCategory(models.TextChoices):
    """Dropdowns the plant team maintains as a plain list of words.

    Each category is one select on one register. Adding a value is a Settings
    edit; adding a whole new category needs a line here plus the field that
    uses it.
    """

    SLUDGE_COLLECTION_MODE = "SLUDGE_COLLECTION_MODE", "Sludge — mode of collection"
    SLUDGE_STORAGE_METHOD = "SLUDGE_STORAGE_METHOD", "Sludge — method of storage"
    SLUDGE_DISPOSAL_MODE = "SLUDGE_DISPOSAL_MODE", "Sludge — mode of disposal"
    CALIBRATION_ACTION = "CALIBRATION_ACTION", "Calibration — corrective action"

class RegisterKey(models.TextChoices):
    """Which register a change-log row belongs to.

    A stable key rather than the model name, so the trail keeps reading the same
    way if a model is ever split or renamed.
    """

    DAILY_LOG = "DAILY_LOG", "Daily plant log"
    MONITORING = "MONITORING", "On-line monitoring"
    CHEMICAL = "CHEMICAL", "Chemical consumption"
    SLUDGE = "SLUDGE", "Sludge generation"
    BACKWASH = "BACKWASH", "Daily back washing"
    CALIBRATION = "CALIBRATION", "Calibration"


class ChangeAction(models.TextChoices):
    CREATED = "CREATED", "Recorded"
    UPDATED = "UPDATED", "Edited"
    DELETED = "DELETED", "Deleted"
    VERIFIED = "VERIFIED", "Verified"


class PrintDocumentKey(models.TextChoices):
    """One key per printable ETP / STP form.

    The keys match the front-end's ``CONTROLLED_DOCUMENTS`` entries, which stay
    as the fallback for a form nobody has configured in the database yet.
    """

    ETP_DAILY_RECORD = "ETP_DAILY_RECORD", "Effluent Treatment Plant Record"
    ETP_MONITORING_RECORD = "ETP_MONITORING_RECORD", "ETP On Line Monitoring Record"
    ETP_CHEMICAL_CONSUMPTION = (
        "ETP_CHEMICAL_CONSUMPTION",
        "Chemical Consumption Record — ETP",
    )
    STP_CHEMICAL_CONSUMPTION = (
        "STP_CHEMICAL_CONSUMPTION",
        "Chemical Consumption Record — STP",
    )
    ETP_SLUDGE_GENERATION = "ETP_SLUDGE_GENERATION", "Sludge Generation Record"
    ETP_BACKWASH_RECORD = "ETP_BACKWASH_RECORD", "Daily Back Washing Record"
    ETP_CALIBRATION_RECORD = "ETP_CALIBRATION_RECORD", "Calibration Record"


#: What the paper forms carry today:
#: ``{key: (printed name, code, revision, issue date)}``.
#:
#: Seeded into the database by ``seed_etp_masters`` so the register prints a real
#: number from day one; edit the rows, not this table, once it is seeded.
#:
#: Every value below is read off a photographed original of the filled register
#: (Aug 2026 set), except where noted:
#:
#: * ``ETP_DAILY_RECORD`` — the code is illegible on both photos of the sheet and
#:   is deliberately left BLANK. It previously carried ``QA-FRM-14-00-08-01``,
#:   which is really the Shelf Life Study Record's number; printing that would put
#:   another form's controlled number on this register. QA fills it from Settings.
#: * ``ETP_MONITORING_RECORD`` — the footer confirms the ``QA-FRM-14-00-08-``
#:   group but the last segment is lost in the curl of the page, so ``-02`` is
#:   still a guess inside the right group. The revision and date ARE confirmed.
#: * ``ETP_CHEMICAL_CONSUMPTION`` — the ETP sheet is the "A" variant of the STP
#:   sheet's number (the QA-FRM house style for a paired form), read off a blurred
#:   footer. Worth one confirmation from QA.
DEFAULT_PRINT_DOCUMENTS = {
    PrintDocumentKey.ETP_DAILY_RECORD: (
        "EFFLUENT TREATMENT PLANT RECORD",
        "",
        "01",
        "2024-06-01",
    ),
    PrintDocumentKey.ETP_MONITORING_RECORD: (
        "ETP ON LINE MONITORING RECORD",
        "QA-FRM-14-00-08-02",
        "00",
        "2023-10-05",
    ),
    PrintDocumentKey.ETP_CHEMICAL_CONSUMPTION: (
        "CHEMICAL CONSUMPTION RECORD FOR ETP PLANT",
        "QA-FRM-14-00-08-04 A",
        "01",
        "2024-07-01",
    ),
    PrintDocumentKey.STP_CHEMICAL_CONSUMPTION: (
        "CHEMICAL CONSUMPTION RECORD FOR STP PLANT",
        "QA-FRM-14-00-08-04",
        "01",
        "2024-07-01",
    ),
    PrintDocumentKey.ETP_SLUDGE_GENERATION: (
        "SLUDGE GENERATION RECORD",
        "QA-FRM-14-00-08-06",
        "00",
        "2025-01-01",
    ),
    PrintDocumentKey.ETP_BACKWASH_RECORD: (
        "DAILY BACK WASHING RECORD",
        "QA-FRM-14-09-00-03",
        "00",
        "2023-10-05",
    ),
    PrintDocumentKey.ETP_CALIBRATION_RECORD: (
        "CALIBRATION RECORD",
        "CAL-FRM-08-03-00-01",
        "01",
        "2023-09-06",
    ),
}
