"""
board_builder/admin.py

Registered so an administrator can find a board somebody deleted by mistake,
re-own one whose author has left, and read a layout without the editor.

Deliberately read-mostly on the geometry: the column/row pair is validated
against the catalogue in the serializer, and the admin does not run that
check. Editing a placement here can produce a board that overlaps itself --
which the service detects and reports as ``meta.misplaced`` rather than
drawing wrongly, but it is still not the place to arrange a board.
"""

from django.contrib import admin

from .models import BoardPlacement, CustomBoard


class BoardPlacementInline(admin.TabularInline):
    model = BoardPlacement
    extra = 0
    fields = ("card_key", "column", "row", "title", "accent", "options")


@admin.register(CustomBoard)
class CustomBoardAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "company",
        "owner",
        "mode",
        "visibility",
        "in_carousel",
        "is_active",
        "updated_at",
    )
    list_filter = ("company", "mode", "visibility", "in_carousel", "is_active")
    search_fields = ("name", "slug", "description", "owner__username")
    raw_id_fields = ("owner", "published_by", "created_by", "updated_by")
    filter_horizontal = ("audience",)
    inlines = [BoardPlacementInline]
