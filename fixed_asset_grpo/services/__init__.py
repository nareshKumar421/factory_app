"""Fixed Asset GRPO services.

* ``po_link``  -- the late PO link (receive / update / remove a PO receipt on
  an FA entry), its lock reasons, and discarding drafts a change would stale.
* ``entries``  -- scope (closure, cut-over), the Needs PO list, the entry
  detail, gate-photo fallback, "done in SAP by hand" close / reopen.
* ``grpo``     -- ``FixedAssetGRPOService(GRPOService)``: the engine scoped to
  FIXED_ASSET, the one-warehouse rule, simulate, and the discard hatches.
* ``state``    -- small shared reads (simulated?, on its way to SAP?).
"""

from .entries import (
    close_fa_entry,
    entry_in_flight,
    entry_state,
    fa_entries,
    fa_entry_detail,
    fa_scope,
    gate_attachment_files,
    is_before_cutover,
    needs_po_entries,
    needs_po_row,
    open_draft_of,
    reopen_fa_entry,
)
from .grpo import (
    SIMULATED_MESSAGE,
    FixedAssetGRPOService,
    discard_fa_draft,
    discard_simulated_grpo,
)
from .po_link import (
    can_link_po,
    discard_unsent_fa_drafts,
    entry_lock_reason,
    fa_po_receipt_lock_reason,
    get_open_fa_po,
    receive_fa_po,
    remove_fa_po_receipt,
    serialize_fa_po_receipt,
    update_fa_po_receipt,
)
from .state import (
    UNSENT_STATUSES,
    active_source_ids,
    closure_of,
    fa_detail_of,
    grpo_on_its_way,
    is_simulated,
    is_stranded,
    record_fa_post,
    reopen_stranded,
)
