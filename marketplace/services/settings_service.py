"""Per company + channel marketplace flow settings.

Currently holds the ``skip_packing`` toggle (see :class:`MarketplaceSettings`).
Reads are cheap get-or-create so a company that never opened Settings still
behaves with sane defaults (all toggles off).
"""
from ..models import MarketplaceSettings


def get_settings(company, channel):
    """Return (creating if needed) the settings row for a company + channel."""
    settings, _created = MarketplaceSettings.objects.get_or_create(
        company=company, channel=channel
    )
    return settings


def is_skip_packing(company, channel):
    """Whether the Packing step is skipped for this company + channel.

    ``channel`` may be ``None`` (e.g. an all-channel query) — in that case the
    safe default is False (packing required), since the toggle is per channel.
    """
    if not channel:
        return False
    return (
        MarketplaceSettings.objects.filter(company=company, channel=channel)
        .values_list("skip_packing", flat=True)
        .first()
        or False
    )


def set_skip_packing(company, channel, value, *, user=None):
    """Update the ``skip_packing`` toggle and return the settings row."""
    return _set_flag(company, channel, "skip_packing", value, user=user)


def is_defer_delivery_note(company, channel):
    """Whether the SAP Delivery Note is deferred to the bulk page (per channel).

    ``channel`` may be ``None`` — default False (post at confirm, as usual).
    """
    if not channel:
        return False
    return (
        MarketplaceSettings.objects.filter(company=company, channel=channel)
        .values_list("defer_delivery_note", flat=True)
        .first()
        or False
    )


def set_defer_delivery_note(company, channel, value, *, user=None):
    """Update the ``defer_delivery_note`` toggle and return the settings row."""
    return _set_flag(company, channel, "defer_delivery_note", value, user=user)


def _set_flag(company, channel, field, value, *, user=None):
    settings = get_settings(company, channel)
    setattr(settings, field, bool(value))
    settings.updated_by = user
    settings.save(update_fields=[field, "updated_by", "updated_at"])
    return settings
