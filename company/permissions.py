## Not Final working on this copy

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.exceptions import PermissionDenied
from .models import Company, UserCompany


#: The group companies a wall board is allowed to read across.
#:
#: Deliberately a fixed list rather than "every company on the deployment": the
#: boards add Oil and Mart together and Beverages runs its own copy of the same
#: screen, so those three are the whole of what a board ever asks for. A company
#: added to the deployment later must be named here on purpose before a board
#: read reaches it.
BOARD_CROSS_COMPANY_CODES = frozenset(
    {"JIVO_OIL", "JIVO_MART", "JIVO_BEVERAGES"}
)


def _required_company_code(request):
    """The `Company-Code` header, or a 403 saying it is missing."""
    company_code = request.headers.get("Company-Code")
    if not company_code:
        raise PermissionDenied("Company-Code header is missing.")
    return company_code


def _membership(user, company_code):
    """The user's own active row for that company, or None."""
    return UserCompany.objects.filter(
        user=user, is_active=True, company__code=company_code
    ).first()


class HasCompanyContext(BasePermission):

    def has_permission(self, request, view):

        # Extract company code from headers
        company_code = _required_company_code(request)

        # Validate presence and access
        user_company = _membership(request.user, company_code)
        if user_company is None:
            raise PermissionDenied("You do not have access to any companies.")

        # Attach for later use
        request.company = user_company

        return True


class HasBoardCompanyContext(HasCompanyContext):
    """Company context for the endpoints a control board reads across companies.

    WHY THIS EXISTS
    ---------------
    The Logistics Control board reports Oil and Mart added together — that is
    what the tile *means*, not a convenience — and it gets there by asking each
    of these endpoints once per company with the `Company-Code` header pinned.
    Under the plain `HasCompanyContext` every one of those Mart reads 403s for a
    login that is not a member of Mart, and the board quietly reported Oil only:
    the same wall, the same heading, roughly half the number. A figure that
    silently drops a company is worse than one that fails, because nothing on
    the screen says so.

    The business's decision, recorded here because it is a disclosure decision
    and not a technical one: holding a board's own view right is enough to see
    the board's whole figure. Company membership decides which company a person
    *works in* and therefore what they can act on; it no longer decides whether
    a roll-up they are already allowed to read is complete.

    WHAT IT DOES AND DOES NOT WIDEN
    -------------------------------
    It widens the COMPANY gate only. Every view it is used on keeps its own
    module permission class ahead of it, so this never buys access to a report
    the login could not already open for its own company — only the same report,
    for a group company the board adds in.

    Two hard limits keep that true:

      - Reads only. A non-member may GET; a write still needs real membership,
        so nothing here lets somebody edit another company's records. The one
        caveat worth naming: the two board-settings views `get_or_create` their
        row on GET, so a non-member read can bring an empty settings row into
        being. That row is keyed on the board's own (company, warehouse) and
        holds nulls, which is the state the screen already renders as "not
        configured" — no figure of anybody's changes.
      - Group companies only, named in `BOARD_CROSS_COMPANY_CODES`.

    A login with no active company at all is still refused, so this is a
    widening for staff, not an open door for any authenticated account.

    Use it ONLY on endpoints a board actually fans out across companies. Putting
    it on a module's whole view set would turn a per-board decision into a
    site-wide one — the same trap `admin_board.carousel` documents for the
    carousel right.
    """

    def has_permission(self, request, view):
        company_code = _required_company_code(request)

        user_company = _membership(request.user, company_code)
        if user_company is None:
            user_company = self._board_context(request, company_code)

        if user_company is None:
            raise PermissionDenied("You do not have access to any companies.")

        request.company = user_company
        return True

    @staticmethod
    def _board_context(request, company_code):
        """A read-only stand-in company context, or None if the rules say no.

        Returned UNSAVED on purpose: it is a request-scoped view of who is
        asking about what, never a grant. Callers read `.company` and
        `.company_id` off it — see the module-wide search behind that claim —
        and saving one would hand out a real membership by accident.
        """
        if request.method not in SAFE_METHODS:
            return None

        if (company_code or "").upper() not in BOARD_CROSS_COMPANY_CODES:
            return None

        # Staff of *some* company, never merely an authenticated account.
        if not UserCompany.objects.filter(user=request.user, is_active=True).exists():
            return None

        company = Company.objects.filter(code=company_code).first()
        if company is None:
            return None

        return UserCompany(user=request.user, company=company, is_active=True)


## Test This is the end of the code
