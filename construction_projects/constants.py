"""Every enum and tuning constant the module uses, in one place."""

from django.db import models


class ProjectStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    APPROVED = "APPROVED", "Approved"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    ON_HOLD = "ON_HOLD", "On hold"
    COMPLETED = "COMPLETED", "Completed"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class AttachmentKind(models.TextChoices):
    """What a project's paper is.

    Only the map is named. A site map is the one attachment people go looking
    for -- it answers "where on the campus, and what shape" -- and hunting it
    out of a list of quotations and sanction letters is the thing this
    distinction exists to stop. Everything else is a document until somebody
    has a reason to separate it too.
    """

    MAP = "MAP", "Site map"
    DOCUMENT = "DOCUMENT", "Document"


class RevisionStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


class DimensionUnit(models.TextChoices):
    """What the length/breadth/height are measured in.

    Recorded rather than assumed: "30 x 20 x 12" means nothing without it, and
    a site that works in feet while the estimate is priced per CUM is exactly
    where a quantity goes wrong by a factor of 35.
    """

    FEET = "FT", "Feet"
    METRE = "M", "Metres"


class ExpenseBatchStatus(models.TextChoices):
    """Where a project's running set of payments has got to.

    Expenses are not approved one at a time: they pile into the project's open
    batch, and one decision settles the lot. That matches how a site is actually
    reviewed -- "this week's spend is fine" -- rather than making somebody click
    through a hundred cement bills.

    The money has already left the box by the time a line is recorded, so this
    is a REVIEW and never a gate. A returned batch goes back to the site to be
    corrected and sent again; nothing is ever un-spent.
    """

    # Labels are what a site manager reads on the screen, so they say what has
    # happened rather than naming the state machine.
    OPEN = "OPEN", "Not sent yet"
    SUBMITTED = "SUBMITTED", "Waiting for approval"
    APPROVED = "APPROVED", "Approved"
    RETURNED = "RETURNED", "Sent back for changes"


class StopReason(models.TextChoices):
    """Why no work happened. A choice rather than free text so that "we lost 11
    days to rain and 4 waiting for material" is a query, not a reading
    exercise -- that sentence is the whole justification for a timeline
    extension."""

    RAIN = "RAIN", "Rain"
    NO_MATERIAL = "NO_MATERIAL", "Material not available"
    NO_LABOUR = "NO_LABOUR", "Labour not available"
    NO_POWER = "NO_POWER", "No power"
    HOLIDAY = "HOLIDAY", "Holiday"
    APPROVAL_PENDING = "APPROVAL_PENDING", "Waiting on an approval"
    SAFETY = "SAFETY", "Safety"
    OTHER = "OTHER", "Other"


class ExpenseCategory(models.TextChoices):
    MATERIAL = "MATERIAL", "Material"
    LABOUR = "LABOUR", "Labour"
    CONTRACTOR = "CONTRACTOR", "Contractor"
    EQUIPMENT_HIRE = "EQUIPMENT_HIRE", "Equipment hire"
    TRANSPORT = "TRANSPORT", "Transport"
    PROFESSIONAL_FEES = "PROFESSIONAL_FEES", "Professional fees"
    STATUTORY = "STATUTORY", "Statutory / fees"
    OTHER = "OTHER", "Other"


class PaymentMode(models.TextChoices):
    CASH = "CASH", "Cash"
    BANK = "BANK", "Bank transfer"
    CHEQUE = "CHEQUE", "Cheque"
    UPI = "UPI", "UPI"
    #: Material taken on credit is spent money even though it has not left the
    #: bank. Counting it only on payment makes a project look under budget right
    #: up until the day it doesn't.
    CREDIT = "CREDIT", "On credit (not yet paid)"


#: A project may be edited in place only at these statuses. Past approval the
#: budget and the end date move through a ProjectRevision instead.
EDITABLE_STATUSES = frozenset({ProjectStatus.DRAFT, ProjectStatus.REJECTED})

#: A project is live -- loggable, spendable against -- at these.
ACTIVE_STATUSES = frozenset(
    {ProjectStatus.APPROVED, ProjectStatus.IN_PROGRESS, ProjectStatus.ON_HOLD}
)

#: Nothing may be written against a project at these.
CLOSED_STATUSES = frozenset({ProjectStatus.COMPLETED, ProjectStatus.CANCELLED})

#: Batch states the site may still add to or edit. A submitted batch is with
#: the approver and an approved one is settled, so both are locked.
EDITABLE_BATCH_STATUSES = frozenset(
    {ExpenseBatchStatus.OPEN, ExpenseBatchStatus.RETURNED}
)

#: How many days back a daily log may be filed without ``can_edit_project``.
#: Sites get written up late; sites written up three weeks late are being
#: reconstructed from memory.
DEFAULT_BACKDATE_DAYS = 7
