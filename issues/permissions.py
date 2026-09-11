"""
DRF permission classes for the issue tracker.

The split that matters is between *filing* and *triaging*. Anyone who uses the
software should be able to report a problem in it, so ``can_create_issues`` is
meant to be handed out widely; deciding what an issue is labelled, who owns it
and whether it is closed is ``can_triage_issues`` and belongs to whoever owns
the backlog.

On top of the Django permissions there is one object-level rule, applied in
:func:`can_edit_issue` and :func:`can_edit_comment`: **an author may always edit
and close their own issue, and edit or delete their own comment**, triage right
or not. Someone who filed a duplicate by mistake should be able to withdraw it
without waiting for a maintainer.
"""

from rest_framework.permissions import BasePermission

VIEW_PERMISSION = "issues.can_view_issues"
CREATE_PERMISSION = "issues.can_create_issues"
TRIAGE_PERMISSION = "issues.can_triage_issues"
SETTINGS_PERMISSION = "issues.can_manage_issue_settings"

#: Holding any of these reveals the module. Triage and settings imply reading --
#: nobody should be able to close an issue they cannot open.
ANY_ACCESS = (VIEW_PERMISSION, CREATE_PERMISSION, TRIAGE_PERMISSION, SETTINGS_PERMISSION)

#: HTTP verbs that write.
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _has_any(user, permissions):
    return bool(
        user
        and user.is_authenticated
        and any(user.has_perm(permission) for permission in permissions)
    )


class CanViewIssues(BasePermission):
    """Read the tracker."""

    def has_permission(self, request, view):
        return _has_any(request.user, ANY_ACCESS)


class CanCreateIssues(BasePermission):
    """Read the tracker; write needs the right to file (or to triage)."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return _has_any(request.user, (CREATE_PERMISSION, TRIAGE_PERMISSION))
        return _has_any(request.user, ANY_ACCESS)


class CanTriageIssues(BasePermission):
    """Triage-only endpoints (bulk state changes, pinning, label assignment)."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return _has_any(request.user, (TRIAGE_PERMISSION,))
        return _has_any(request.user, ANY_ACCESS)


class CanManageIssueSettings(BasePermission):
    """The label master: anyone on the module reads it (every form needs the
    list to render), only the settings right writes it."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return _has_any(request.user, (SETTINGS_PERMISSION,))
        return _has_any(request.user, ANY_ACCESS)


def can_triage(user):
    return _has_any(user, (TRIAGE_PERMISSION,))


def can_edit_issue(user, issue):
    """Triagers may edit anything; an author may always edit their own issue."""
    if can_triage(user):
        return True
    return bool(user and issue.author_id and user.pk == issue.author_id)


def can_edit_comment(user, comment):
    """Triagers may edit any comment; an author may always edit their own."""
    if can_triage(user):
        return True
    return bool(user and comment.author_id and user.pk == comment.author_id)


def permission_flags(user):
    """The rights the client needs to decide what to render.

    Sent with every list and detail response so the UI can hide the Close button
    rather than letting someone click into a 403.
    """
    return {
        "can_view": _has_any(user, ANY_ACCESS),
        "can_create": _has_any(user, (CREATE_PERMISSION, TRIAGE_PERMISSION)),
        "can_triage": can_triage(user),
        "can_manage_settings": _has_any(user, (SETTINGS_PERMISSION,)),
    }
