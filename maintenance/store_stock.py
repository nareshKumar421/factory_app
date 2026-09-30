"""Take stock out of, or put it back into, Store / Spares.

For flows outside the Store page that move store material — a gate pass
taking switches out of the factory, a returnable pass bringing a tool back.
Every change writes a SpareMovement, so an item's history on the Store page
always adds up to what is on the shelf. Call these inside a transaction.
"""

from collections import defaultdict
from decimal import Decimal

from .constants import SpareMovementType
from .models import MaintenanceSpare, SpareMovement


#: The store's real stock has not been entered yet, so a shelf count cannot be
#: trusted to stop anything: stock that goes out comes off even when that takes
#: it below zero, and nothing waits on the store. Once the counts are in, set
#: this to False and every take-out -- Store give out, work-order issue, gate
#: pass save and gate out -- refuses what the shelf does not hold again.
ALLOW_NEGATIVE_STOCK = True


def refuses(spare, quantity):
    """Whether taking ``quantity`` of ``spare`` is refused for want of stock."""
    return not ALLOW_NEGATIVE_STOCK and quantity > spare.current_stock


def qty_text(value):
    """``3.000`` as "3", ``100.000`` as "100" (not "1E+2")."""
    return format(value.normalize(), "f")


class NotEnoughInStore(Exception):
    """The store has less of an item than is being taken out."""

    def __init__(self, spare):
        self.spare = spare
        self.in_store = qty_text(spare.current_stock)
        super().__init__(f"Only {self.in_store} {spare.uom} of {spare.name} in store.")


def take_out(lines, *, user, remarks):
    """Take ``(spare_id, quantity)`` lines out of the store — all of them, or none.

    Lines naming the same item are added together first. While stock may go
    below zero (ALLOW_NEGATIVE_STOCK) nothing is refused; otherwise two lines of
    3 against a stock of 4 raise NotEnoughInStore for that item, before writing
    anything.
    """
    wanted = defaultdict(Decimal)
    for spare_id, quantity in lines:
        wanted[spare_id] += quantity
    spares = {
        spare.id: spare
        for spare in MaintenanceSpare.objects.select_for_update().filter(pk__in=wanted)
    }
    for spare_id, quantity in wanted.items():
        if refuses(spares[spare_id], quantity):
            raise NotEnoughInStore(spares[spare_id])
    for spare_id, quantity in wanted.items():
        _move(spares[spare_id], -quantity, SpareMovementType.ISSUE, user=user, remarks=remarks)


def put_back(spare_id, quantity, *, user, remarks):
    """Put ``quantity`` of a store item back on the shelf."""
    spare = MaintenanceSpare.objects.select_for_update().get(pk=spare_id)
    _move(spare, quantity, SpareMovementType.RETURN, user=user, remarks=remarks)


def _move(spare, change, movement_type, *, user, remarks):
    spare.current_stock += change
    spare.updated_by = user
    spare.save(update_fields=["current_stock", "updated_by", "updated_at"])
    SpareMovement.objects.create(
        company_id=spare.company_id,
        spare=spare,
        movement_type=movement_type,
        quantity=abs(change),
        unit_cost=spare.unit_cost,
        remarks=remarks,
        performed_by=user,
        created_by=user,
        updated_by=user,
    )
