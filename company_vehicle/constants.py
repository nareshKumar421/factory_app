"""Choices the fleet register offers, in one place.

The API sends these to the page through ``options/`` rather than the client
hardcoding them, so a category added here shows up in the form without a
frontend release.
"""

from django.db import models


class VehicleCategory(models.TextChoices):
    """What kind of vehicle it is. Drives nothing but the icon and filters."""

    TRUCK = "TRUCK", "Truck"
    TEMPO = "TEMPO", "Tempo / Pickup"
    CAR = "CAR", "Car"
    VAN = "VAN", "Van (Eeco / Omni)"
    TWO_WHEELER = "TWO_WHEELER", "Scooty / Bike"
    TRACTOR = "TRACTOR", "Tractor"
    FORKLIFT = "FORKLIFT", "Forklift"
    OTHER = "OTHER", "Other"


class FuelType(models.TextChoices):
    """What the vehicle runs on.

    ``PETROL_CNG`` is the dual-fuel case (an Eeco or a retrofitted car): the
    vehicle carries it, but every fuel entry still records the ONE fuel that
    was actually filled, so the two run as separate mileage streams.
    """

    DIESEL = "DIESEL", "Diesel"
    PETROL = "PETROL", "Petrol"
    CNG = "CNG", "CNG"
    PETROL_CNG = "PETROL_CNG", "Petrol + CNG"
    ELECTRIC = "ELECTRIC", "Electric"


#: The fuels a single filling can be. ``PETROL_CNG`` is a vehicle property,
#: never a fill, so it is deliberately absent.
FILLABLE_FUELS = [
    FuelType.DIESEL,
    FuelType.PETROL,
    FuelType.CNG,
    FuelType.ELECTRIC,
]

#: Which fuels a vehicle of each type may be filled with. Read by the fuel
#: form to decide whether to show the petrol/CNG toggle at all.
FUELS_ALLOWED_FOR = {
    FuelType.DIESEL: [FuelType.DIESEL],
    FuelType.PETROL: [FuelType.PETROL],
    FuelType.CNG: [FuelType.CNG],
    FuelType.PETROL_CNG: [FuelType.PETROL, FuelType.CNG],
    FuelType.ELECTRIC: [FuelType.ELECTRIC],
}

#: CNG is sold by weight and everything else by volume. The unit is never
#: stored on an entry -- it follows from the fuel, so the two cannot disagree.
FUEL_UNITS = {
    FuelType.DIESEL: "L",
    FuelType.PETROL: "L",
    FuelType.CNG: "Kg",
    FuelType.ELECTRIC: "kWh",
}


class VehicleStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    IN_WORKSHOP = "IN_WORKSHOP", "In workshop"
    STANDBY = "STANDBY", "Standby"
    SOLD = "SOLD", "Sold / Scrapped"


class ApprovalStatus(models.TextChoices):
    """Every bill starts pending. Only approved money reaches the reports."""

    PENDING = "PENDING", "Pending approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class PaymentMode(models.TextChoices):
    """How the bill was paid. A label on the entry and nothing more --
    the fleet register posts to no cash book and no accounts module."""

    CASH = "CASH", "Cash"
    CARD = "CARD", "Company card"
    UPI = "UPI", "UPI / Online"
    CREDIT = "CREDIT", "Credit at pump"


class ServiceKind(models.TextChoices):
    ROUTINE = "ROUTINE", "Routine service"
    REPAIR = "REPAIR", "Repair / Breakdown"
    TYRE = "TYRE", "Tyre"
    BATTERY = "BATTERY", "Battery"
    BODY = "BODY", "Body work"
    OTHER = "OTHER", "Other"


class DocumentKind(models.TextChoices):
    RC = "RC", "Registration (RC)"
    INSURANCE = "INSURANCE", "Insurance"
    PUC = "PUC", "Pollution (PUC)"
    FITNESS = "FITNESS", "Fitness certificate"
    PERMIT = "PERMIT", "Permit"
    ROAD_TAX = "ROAD_TAX", "Road tax"
    OTHER = "OTHER", "Other"


#: A document inside this many days of expiry is shown as a warning.
DOCUMENT_WARNING_DAYS = 30

#: A service due within this many km is shown as a warning.
SERVICE_DUE_WARNING_KM = 500
