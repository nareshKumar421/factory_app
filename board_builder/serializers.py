"""
board_builder/serializers.py

Saving a board, and refusing to save one that cannot be drawn.

WHERE VALIDATION LIVES AND WHY IT IS HERE RATHER THAN ON THE MODEL
--------------------------------------------------------------------
Two of the three rules below cannot be expressed as field validators, because
they are about a row's relationship to its siblings and to another table:

* the row ceiling depends on ``mode`` (a wall board may be five rows, a page
  board twelve), so it needs the whole object;
* whether a card FITS depends on its footprint, which lives in the catalogue,
  not in the database at all.

The database still carries what it can -- one card per cell is a unique
constraint, so two requests racing to drop a tile on the same square cannot
both win. Everything else is checked here, once, on the whole submitted
layout.

THE LAYOUT IS REPLACED WHOLE, NOT PATCHED
------------------------------------------
``placements`` is written as a set: what the editor sends becomes the board's
entire layout. Per-placement PATCH was rejected for a reason worth recording.
A drag is rarely one change -- moving a card into an occupied cell displaces
its neighbour, which displaces another -- so an editor patching one row at a
time would walk the board through intermediate states that violate the overlap
rule, and would have to either relax the rule or order its requests. Sending
the finished arrangement makes every save atomic and every stored board valid.
"""

from __future__ import annotations

from django.contrib.auth.models import Group
from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from . import catalogue
from .constants import (
    BOARD_DENSITIES,
    BOARD_SURFACES,
    CARD_ACCENTS,
    GRID_MAX_COLUMNS,
    GRID_MIN_COLUMNS,
    GRID_MIN_ROWS,
    max_rows_for,
)
from .models import BoardMode, BoardPlacement, CustomBoard
from .permissions import readable_cards


def unique_slug(company, name: str, *, exclude_pk=None) -> str:
    """A slug that is free within this company.

    Suffixed rather than rejected: two desks naming a board "Dispatch" is not
    a conflict anybody wants to be told about, and the slug is an address
    rather than a name.

    Module level rather than a serializer method because the duplicate
    endpoint needs it too, and reaching for it through a serializer built for
    the purpose was the kind of indirection that reads as a mistake.
    """
    base = slugify(name)[:120] or "board"
    candidate = base
    siblings = CustomBoard.objects.filter(company=company)
    if exclude_pk:
        siblings = siblings.exclude(pk=exclude_pk)
    suffix = 2
    while siblings.filter(slug=candidate).exists():
        candidate = f"{base}-{suffix}"[:140]
        suffix += 1
    return candidate


class PlacementSerializer(serializers.ModelSerializer):
    """One card at one spot.

    ``columns``/``rows`` are read-only and come from the catalogue. They are
    echoed back because the editor needs the footprint to draw the card, and
    they are not writable because the footprint is the card's, not the
    board's -- see ``models.py``.
    """

    columns = serializers.SerializerMethodField()
    rows = serializers.SerializerMethodField()
    card_title = serializers.SerializerMethodField()

    class Meta:
        model = BoardPlacement
        fields = [
            "id",
            "card_key",
            "card_title",
            "column",
            "row",
            "columns",
            "rows",
            "title",
            "accent",
            "options",
        ]
        read_only_fields = ["id"]

    def get_columns(self, obj) -> int:
        spec = catalogue.get(obj.card_key)
        return spec.columns if spec else 1

    def get_rows(self, obj) -> int:
        spec = catalogue.get(obj.card_key)
        return spec.rows if spec else 1

    def get_card_title(self, obj) -> str:
        """The catalogue's own name, so the editor can offer to restore it."""
        spec = catalogue.get(obj.card_key)
        return spec.title if spec else obj.card_key


class BoardListSerializer(serializers.ModelSerializer):
    """A board in a list: enough to choose one, nothing that costs a query."""

    owner_name = serializers.SerializerMethodField()
    card_count = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = CustomBoard
        fields = [
            "id",
            "slug",
            "name",
            "description",
            "mode",
            "columns",
            "rows",
            "surface",
            "accent",
            "visibility",
            "in_carousel",
            "owner_name",
            "card_count",
            "is_mine",
            "updated_at",
        ]

    def get_owner_name(self, obj) -> str:
        owner = obj.owner
        full = owner.get_full_name() if hasattr(owner, "get_full_name") else ""
        return full or owner.get_username()

    def get_card_count(self, obj) -> int:
        # Annotated by the view. Falls back to a count rather than failing, so
        # the serializer stays usable from a shell or a test.
        return getattr(obj, "card_count", None) or obj.placements.count()

    def get_is_mine(self, obj) -> bool:
        request = self.context.get("request")
        return bool(request and obj.owner_id == request.user.id)


class BoardSerializer(serializers.ModelSerializer):
    """One board, with its whole layout. Read and write."""

    placements = PlacementSerializer(many=True, required=False)
    #: The groups a published board is shown to. Empty means the whole
    #: company -- which is not the same as "everybody", because each card is
    #: still gated on its own feed once the board is open.
    audience = serializers.PrimaryKeyRelatedField(
        many=True,
        required=False,
        queryset=Group.objects.all(),
    )
    owner_name = serializers.SerializerMethodField()
    max_rows = serializers.SerializerMethodField()
    max_columns = serializers.SerializerMethodField()

    class Meta:
        model = CustomBoard
        fields = [
            "id",
            "slug",
            "name",
            "description",
            "mode",
            "columns",
            "rows",
            "surface",
            "density",
            "accent",
            "show_heading",
            "visibility",
            "in_carousel",
            "audience",
            "owner_name",
            "placements",
            "max_rows",
            "max_columns",
            "published_at",
            "updated_at",
        ]
        read_only_fields = ["id", "slug", "visibility", "published_at", "updated_at"]

    def get_owner_name(self, obj) -> str:
        owner = obj.owner
        full = owner.get_full_name() if hasattr(owner, "get_full_name") else ""
        return full or owner.get_username()

    def get_max_rows(self, obj) -> int:
        """The ceiling for THIS board's mode, so the editor's stepper stops
        where the server would refuse rather than one step later."""
        return max_rows_for(obj.mode if obj else BoardMode.WALL)

    def get_max_columns(self, obj) -> int:
        return GRID_MAX_COLUMNS

    # -- validation ---------------------------------------------------------

    def validate_columns(self, value: int) -> int:
        if not GRID_MIN_COLUMNS <= value <= GRID_MAX_COLUMNS:
            raise serializers.ValidationError(
                f"A board is between {GRID_MIN_COLUMNS} and {GRID_MAX_COLUMNS} "
                "columns wide."
            )
        return value

    def validate_surface(self, value: str) -> str:
        if value not in BOARD_SURFACES:
            raise serializers.ValidationError(f"Unknown surface {value!r}.")
        return value

    def validate_density(self, value: str) -> str:
        if value not in BOARD_DENSITIES:
            raise serializers.ValidationError(f"Unknown density {value!r}.")
        return value

    def validate_accent(self, value: str) -> str:
        if value not in CARD_ACCENTS:
            raise serializers.ValidationError(
                f"Unknown accent {value!r}. Choose one of {', '.join(CARD_ACCENTS)}."
            )
        return value

    def validate(self, attrs: dict) -> dict:
        instance = self.instance
        mode = attrs.get("mode", instance.mode if instance else BoardMode.WALL)
        rows = attrs.get("rows", instance.rows if instance else None)
        columns = attrs.get("columns", instance.columns if instance else None)

        ceiling = max_rows_for(mode)
        if rows is not None and not GRID_MIN_ROWS <= rows <= ceiling:
            raise serializers.ValidationError(
                {
                    "rows": (
                        f"A {mode.lower()} board is between {GRID_MIN_ROWS} and "
                        f"{ceiling} rows tall."
                        + (
                            " A wall board does not scroll, so every row has to "
                            "fit the screen at once."
                            if mode == BoardMode.WALL
                            else ""
                        )
                    )
                }
            )

        if "placements" in attrs:
            self._validate_layout(
                attrs["placements"], columns=columns, rows=rows, mode=mode
            )
        return attrs

    def _validate_layout(self, placements: list[dict], *, columns, rows, mode) -> None:
        """Every card known, placeable by this author, inside the grid, alone
        in its cells.

        The author's own rights are checked here and not only in the palette.
        The palette is a convenience; this is the boundary. Without it an
        author could post a card key they were never offered and build
        themselves a tile they may not read -- which the service would then
        withhold, but the attempt should fail at the door, where it can be
        explained.
        """
        request = self.context.get("request")
        allowed = {spec.key for spec in readable_cards(request.user)} if request else None

        taken: dict[tuple[int, int], str] = {}
        for index, placement in enumerate(placements):
            key = placement.get("card_key", "")
            spec = catalogue.get(key)
            if spec is None:
                raise serializers.ValidationError(
                    {"placements": f"Card {index}: no card called {key!r} exists."}
                )
            if allowed is not None and key not in allowed:
                raise serializers.ValidationError(
                    {
                        "placements": (
                            f"Card {index}: you do not hold the data right for "
                            f"{spec.title!r}, so it cannot be placed."
                        )
                    }
                )

            accent = placement.get("accent") or ""
            if accent and accent not in CARD_ACCENTS:
                raise serializers.ValidationError(
                    {"placements": f"Card {index}: unknown accent {accent!r}."}
                )

            column, row = placement.get("column", 0), placement.get("row", 0)
            if column + spec.columns > columns or row + spec.rows > rows:
                raise serializers.ValidationError(
                    {
                        "placements": (
                            f"{spec.title} is {spec.columns}x{spec.rows} and does "
                            f"not fit at column {column + 1}, row {row + 1} of a "
                            f"{columns}x{rows} board."
                        )
                    }
                )

            for dx in range(spec.columns):
                for dy in range(spec.rows):
                    cell = (column + dx, row + dy)
                    if cell in taken:
                        raise serializers.ValidationError(
                            {
                                "placements": (
                                    f"{spec.title} overlaps {taken[cell]} at "
                                    f"column {cell[0] + 1}, row {cell[1] + 1}."
                                )
                            }
                        )
                    taken[cell] = spec.title

    # -- writing ------------------------------------------------------------

    def _write_placements(self, board: CustomBoard, placements: list[dict]) -> None:
        """Replace the layout wholesale. See the module docstring."""
        board.placements.all().delete()
        BoardPlacement.objects.bulk_create(
            [
                BoardPlacement(
                    board=board,
                    card_key=placement["card_key"],
                    column=placement.get("column", 0),
                    row=placement.get("row", 0),
                    title=placement.get("title", ""),
                    accent=placement.get("accent", ""),
                    # Coerced through the card's own declarations, so a stale
                    # key from an older version of the card is dropped here
                    # rather than confusing the card at read time.
                    options=catalogue.require(placement["card_key"]).coerce_options(
                        placement.get("options")
                    ),
                )
                for placement in placements
            ]
        )

    @transaction.atomic
    def create(self, validated_data: dict) -> CustomBoard:
        placements = validated_data.pop("placements", [])
        audience = validated_data.pop("audience", [])
        request = self.context["request"]

        board = CustomBoard.objects.create(
            company=request.company.company,
            owner=request.user,
            created_by=request.user,
            updated_by=request.user,
            slug=unique_slug(request.company.company, validated_data["name"]),
            **validated_data,
        )
        if audience:
            board.audience.set(audience)
        self._write_placements(board, placements)
        return board

    @transaction.atomic
    def update(self, instance: CustomBoard, validated_data: dict) -> CustomBoard:
        placements = validated_data.pop("placements", None)
        audience = validated_data.pop("audience", None)
        request = self.context["request"]

        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.updated_by = request.user
        instance.save()

        if audience is not None:
            instance.audience.set(audience)
        if placements is not None:
            self._write_placements(instance, placements)
        return instance


class CardSpecSerializer(serializers.Serializer):
    """One catalogue entry, as the palette needs it.

    Plain ``Serializer`` because a card is a frozen dataclass in code and not
    a row: there is nothing to save, and a ``ModelSerializer`` would imply
    otherwise to the next person to read this.
    """

    key = serializers.CharField()
    title = serializers.CharField()
    summary = serializers.CharField()
    category = serializers.CharField()
    columns = serializers.IntegerField()
    rows = serializers.IntegerField()
    accent = serializers.CharField()
    note = serializers.CharField()
    needs_sap = serializers.BooleanField()
    options = serializers.SerializerMethodField()

    def get_options(self, spec) -> list[dict]:
        return [
            {
                "key": option.key,
                "label": option.label,
                "kind": option.kind,
                "default": option.default,
                "minimum": option.minimum,
                "maximum": option.maximum,
                "choices": [
                    {"value": value, "label": label} for value, label in option.choices
                ],
                "help": option.help,
            }
            for option in spec.options
        ]
