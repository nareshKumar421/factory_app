"""Who is allowed to change which electricity meter, and file readings on it.

Every caller goes through here rather than querying :class:`UserElectricityMeter`
directly, because two rules are easy to forget and both fail dangerously:

**No assignment means no access.** An unassigned user can neither edit a meter
nor book a reading against one. That is the stricter of the two possible
readings and it was chosen deliberately, so the restriction cannot be bypassed
by simply never configuring somebody. The cost is that this has to be configured
before it ships — see the ``report_electricity_meter_scope_gaps`` command, which
lists exactly who would be locked out, and the ``gaps`` endpoint behind the
admin page's warning banner.

**Superusers are exempt.** Without this the first deploy locks out the very
people who would configure it, including from the page that does the
configuring. It is a deadlock guard, not a convenience.

Errors are DRF ``PermissionDenied`` (403), matching
:mod:`warehouse.services.warehouse_scope`, and they always name the meters the
user *does* manage — "you can't do that" with no second half is what makes a
permission error feel like a bug.
"""

from typing import Iterable, Optional

from rest_framework.exceptions import PermissionDenied

from .models_manager import UserElectricityMeter

#: Rights that put somebody on the electricity register. Holding one of these
#: and managing no meter is precisely the lockout the gap report hunts for.
SCOPED_PERMISSION_CODENAMES = [
    "can_manage_electricity_meter",
    "can_add_daily_electricity",
    "can_edit_daily_electricity",
    "can_delete_daily_electricity",
    "can_manage_daily_electricity",
]


def is_unrestricted(user) -> bool:
    """True for users the meter rules do not apply to."""
    return bool(user and getattr(user, "is_superuser", False))


def managed_meter_ids(user) -> frozenset:
    """Ids of the meters this user manages.

    Empty for an unassigned user. Callers must not read an empty set as
    "unrestricted" — that is exactly backwards here — which is why the assert
    helpers below exist and should be preferred.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return frozenset()
    return frozenset(
        UserElectricityMeter.objects.filter(user=user, is_active=True).values_list(
            "meter_id", flat=True
        )
    )


def manages(user, meter) -> bool:
    """Convenience predicate — for hiding a button, not for guarding a write."""
    if is_unrestricted(user):
        return True
    meter_id = getattr(meter, "pk", meter)
    if meter_id is None:
        return False
    return int(meter_id) in managed_meter_ids(user)


def assert_manages(user, meters: Iterable, *, action: str) -> None:
    """Refuse unless the user manages EVERY meter named.

    All of them, not any: moving a reading from one meter to another names two,
    and letting the managed one authorise the pair would let a keeper book his
    own units onto somebody else's meter.
    """
    if is_unrestricted(user):
        return

    allowed = managed_meter_ids(user)
    if not allowed:
        raise PermissionDenied(
            f"Cannot {action}: you are not set as the manager of any electricity "
            "meter. An administrator assigns this on Admin → Electricity Meter "
            "Managers."
        )

    wanted = set()
    for meter in meters:
        meter_id = getattr(meter, "pk", meter)
        if meter_id is not None:
            wanted.add(int(meter_id))
    if not wanted:
        raise PermissionDenied(
            f"Cannot {action}: no meter is named on it, so it cannot be checked "
            "against the meters you manage."
        )

    missing = wanted - allowed
    if missing:
        raise PermissionDenied(
            f"Cannot {action}: you do not manage {_names(missing)}. "
            f"You manage {_names(allowed)}."
        )


def assert_can_edit_meter(user, meter) -> None:
    """Guard a change to the meter master itself."""
    assert_manages(user, [meter], action="change this meter")


def assert_can_record_for(user, meters: Iterable) -> None:
    """Guard a reading being filed, corrected or removed."""
    assert_manages(user, meters, action="record a reading on this meter")


def _names(meter_ids: Iterable[int]) -> str:
    """"Boiler, Terrace" — named, so the fix is obvious.

    Falls back to ids for a meter that has since been deleted, which is better
    than an error message that blows up rendering itself.
    """
    from .models import ElectricityMeter

    ids = sorted(int(i) for i in meter_ids)
    if not ids:
        return "no meter"
    found = dict(
        ElectricityMeter.objects.filter(pk__in=ids).values_list("pk", "name")
    )
    return ", ".join(found.get(i) or f"meter #{i}" for i in ids)


def users_missing_assignment():
    """Users who may work the electricity register but manage no meter.

    These are precisely the people the "no assignment means no access" rule
    would stop, so this is what the report command and the config page's warning
    banner are built on.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission
    from django.db.models import Q

    # A mistyped codename would make this report empty, which reads as "nobody
    # is affected" — the most dangerous possible wrong answer for a lockout
    # check. Fail loudly instead.
    known = set(
        Permission.objects.filter(
            content_type__app_label="maintenance",
            codename__in=SCOPED_PERMISSION_CODENAMES,
        ).values_list("codename", flat=True)
    )
    missing_perms = sorted(set(SCOPED_PERMISSION_CODENAMES) - known)
    if missing_perms:
        raise RuntimeError(
            "meter_scope: unknown permission codename(s) "
            f"{missing_perms} — run migrations, or fix the list."
        )

    User = get_user_model()
    holders = (
        User.objects.filter(is_active=True, is_superuser=False)
        .filter(
            Q(user_permissions__codename__in=SCOPED_PERMISSION_CODENAMES)
            | Q(groups__permissions__codename__in=SCOPED_PERMISSION_CODENAMES)
        )
        .distinct()
    )

    assigned_ids = set(
        UserElectricityMeter.objects.filter(is_active=True).values_list(
            "user_id", flat=True
        )
    )
    return [u for u in holders if u.id not in assigned_ids]


def unmanaged_meters(only_active: Optional[bool] = True):
    """Meters nobody manages — nobody can edit them or read them either.

    The mirror image of :func:`users_missing_assignment`, and the half that is
    easier to miss: a user with no assignment at least notices on their first
    403, while a meter with no manager simply stops being read.
    """
    from .models import ElectricityMeter

    qs = ElectricityMeter.objects.all()
    if only_active:
        qs = qs.filter(is_active=True)
    managed = set(
        UserElectricityMeter.objects.filter(is_active=True).values_list(
            "meter_id", flat=True
        )
    )
    return [m for m in qs.order_by("name") if m.pk not in managed]
