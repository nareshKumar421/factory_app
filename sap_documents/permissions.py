"""
Access control for SAP Documents.

One class per right. SAP Portal let any login open every document and download
every attachment of every company; here browsing needs ``can_view_sap_documents``
and a download needs ``can_download_sap_attachments`` as well — the download view
lists both classes, so the download right alone opens nothing.
"""

from rest_framework.permissions import BasePermission

VIEW_PERMISSION = "sap_documents.can_view_sap_documents"
DOWNLOAD_PERMISSION = "sap_documents.can_download_sap_attachments"


def _authenticated(request):
    user = request.user
    return bool(user and user.is_authenticated)


class CanViewSapDocuments(BasePermission):
    """Document lists and detail, payment drafts, attachment lists."""

    message = "You do not have permission to view SAP documents."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(VIEW_PERMISSION)


class CanDownloadSapAttachments(BasePermission):
    """The attachment files themselves. Used together with CanViewSapDocuments."""

    message = "You do not have permission to download SAP attachments."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(DOWNLOAD_PERMISSION)
