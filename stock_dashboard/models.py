"""
stock_dashboard/models.py

Custom permissions for the Stock Dashboard and the StockAlertLog
model that prevents duplicate notifications.
"""

from django.db import models


class StockDashboardPermission(models.Model):
    """
    Sentinel model that holds custom permissions for the Stock Dashboard.
    No database rows are ever written to this table.
    """

    class Meta:
        managed = False  # No DB table created
        default_permissions = ()  # Don't generate add/view/change/delete
        permissions = [
            ("can_view_stock_dashboard", "Can view Stock Dashboard"),
        ]


class StockAlertLog(models.Model):
    """
    Tracks notifications sent for low/critical stock items to prevent
    duplicate alerts within the cooldown window.

    One record per (company_code, item_code, warehouse) combination.
    If cooldown_until > now(), the alert is suppressed.
    """

    company_code = models.CharField(max_length=50)
    item_code = models.CharField(max_length=50)
    warehouse = models.CharField(max_length=20)
    stock_status = models.CharField(
        max_length=10,
        choices=[("low", "Low"), ("critical", "Critical")],
    )
    on_hand = models.FloatField()
    min_stock = models.FloatField()
    notified_at = models.DateTimeField(auto_now=True)
    cooldown_until = models.DateTimeField(
        help_text="Do not re-send this alert until after this timestamp"
    )

    class Meta:
        unique_together = ("company_code", "item_code", "warehouse")
        indexes = [
            models.Index(fields=["cooldown_until"]),
        ]

    def __str__(self):
        return f"{self.item_code} @ {self.warehouse} ({self.stock_status})"


class WarehouseBoardSettings(models.Model):
    """Facts about a warehouse that SAP does not hold, typed in by an operator.

    Two of them, both needed by the operations board and neither derivable:

    ``capacity_tonnes`` — what the warehouse is rated to hold. SAP records no
    tonnage capacity anywhere, and WMS knows only pallet slots, which answer a
    different question: a half-empty pallet still occupies a whole slot, so slot
    utilisation and tonnage utilisation legitimately disagree. This is the
    tonnage one.

    ``last_audit_date`` — when stock was last physically verified. Derivable
    only in the weakest sense: a clean WMS cycle count writes no movement row at
    all, so a date inferred from the movement log would really mean "the last
    count that found a discrepancy", which is not the question anybody asks.

    One row per (company, warehouse), created on first read so the board never
    404s on a warehouse nobody has configured yet. Both figures are nullable and
    must stay that way — an unset capacity has to read as "not configured"
    rather than as zero, which would make every warehouse infinitely full.

    Keyed on ``company_code`` rather than a foreign key, matching
    ``StockAlertLog`` above: this app addresses SAP by company code throughout
    and holds no other relations.
    """

    company_code = models.CharField(max_length=50)
    warehouse = models.CharField(max_length=20)

    capacity_tonnes = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Rated capacity in tonnes. Leave empty where the warehouse has no rated figure.",
    )
    last_audit_date = models.DateField(
        null=True,
        blank=True,
        help_text="When stock in this warehouse was last physically verified.",
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_board_settings_updates",
    )

    class Meta:
        unique_together = ("company_code", "warehouse")
        verbose_name = "Warehouse board settings"
        verbose_name_plural = "Warehouse board settings"

    def __str__(self):
        return f"{self.warehouse} @ {self.company_code}"


class LogisticsBoardSettings(models.Model):
    """Board figures an operator types because no system holds them.

    Companions to :class:`WarehouseBoardSettings` above, and here for the same
    reason — this app already owns the operations board's configuration, and a
    second table in a second app for four more fields would only spread it.
    The difference is the key: those are facts about a warehouse, these are
    facts about a company.

    ``labour_rate_per_day`` — what one labourer costs for a day. Cost Master
    holds a ``factory-labour`` rate for exactly this, but none is seeded, so the
    factory-expense board deliberately reports ₹0 with a warning. Typing it here
    lets this board price the gate's head count without waiting on that.

    ``owned_vehicle_numbers`` — the registrations themselves, which is what lets
    the board say *which* trucks are working rather than only how many. Kept
    alongside ``owned_vehicles`` rather than replacing it: a site that only wants
    a headline count should not have to list every plate.

    ``vehicles_out_of_service`` — trucks that cannot work. No feed records it:
    a damaged truck looks identical to an idle one from the gate and from the
    transfer register, so somebody has to say. Without this the board calls a
    truck in the workshop "free", which reads as spare capacity.

    ``owned_vehicles`` — how many trucks the company owns. Not derivable:
    `vehicle_management.Vehicle` has no ownership field in any of its
    migrations, and the master is written by the gate forms, so it is a log of
    every truck that ever reached the barrier rather than a fleet register.
    Note the board can show the total but not the on-duty split, which changes
    by the hour and has no source at all.

    The three staffing pairs are the per-section Employees figure and its
    monthly salary. The attribution does not exist in the data: the two
    department masters this system runs on -- ``accounts.Department`` and
    ``employee_hierarchy.Department`` -- are disjoint, and "Warehouse",
    "Dispatch" and "Transportation" appear in neither. Salary is also withheld
    by the employee endpoint unless the viewer holds a salary grant, which a
    wall-board login does not.

    Every field is nullable and must stay so: unset has to read as "not
    configured" rather than as zero, or a board with nobody assigned looks
    identical to a board with nobody at work.
    """

    company_code = models.CharField(max_length=50, unique=True)

    owned_vehicles = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Trucks the company owns. Leave empty if not tracked.",
    )

    owned_vehicle_numbers = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Registrations of the trucks the company owns, one per line. The "
            "board looks each one up in today's gate arrivals to say whether it "
            "is working or free."
        ),
    )

    vehicles_out_of_service = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Registrations that are off the road -- damaged, in the workshop, "
            "sold. One per line. Nothing in any feed records this."
        ),
    )

    labour_rate_per_day = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "What one labourer costs for a day. Priced against the gate's own "
            "head count. Leave empty to fall back to the Cost Master rate."
        ),
    )

    warehouse_employees = models.PositiveIntegerField(null=True, blank=True)
    warehouse_salary_monthly = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    dispatch_employees = models.PositiveIntegerField(null=True, blank=True)
    dispatch_salary_monthly = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    transport_employees = models.PositiveIntegerField(null=True, blank=True)
    transport_salary_monthly = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logistics_board_settings_updates",
    )

    class Meta:
        verbose_name = "Logistics board settings"
        verbose_name_plural = "Logistics board settings"

    def __str__(self):
        return f"Logistics board settings for {self.company_code}"


    @staticmethod
    def _plate_list(raw):
        """Registrations from a newline-separated block, normalised.

        Upper-cased and stripped of spaces and dashes because a plate is typed
        differently every time -- "hr69 e 4548" and "HR69E4548" are one truck,
        and each register holds whichever form the operator entered.
        """
        seen = []
        for line in (raw or "").splitlines():
            plate = line.strip().upper().replace(" ", "").replace("-", "")
            if plate and plate not in seen:
                seen.append(plate)
        return seen

    @property
    def out_of_service_list(self):
        """Registrations that cannot work today."""
        return self._plate_list(self.vehicles_out_of_service)

    @property
    def owned_vehicle_list(self):
        """The configured registrations, normalised and de-duplicated.

        Upper-cased and stripped of spaces because a plate is typed differently
        every time — "pb01 ab 1234" and "PB01AB1234" are one truck, and the gate
        records whichever the guard entered.
        """
        return self._plate_list(self.owned_vehicle_numbers)


class PlantBoardWorkforce(models.Model):
    """Head count and monthly wage bill for one department of the plant.

    Here rather than in ``plant_board`` for the same reason
    :class:`LogisticsBoardSettings` is here: this app already owns the
    operations boards' configuration, and ``plant_board`` has no models at all —
    giving it its first one would put a migration and a permission row on the
    live database for four columns.

    A ROW PER DEPARTMENT, NOT A COLUMN PER DEPARTMENT. The wage bill arrives as
    a six-line table today and the business has already split two of those lines
    into company and outside; flattening that into twelve columns would make the
    next split a migration. Which band a department belongs to, and whether it
    counts as staff or as labour, is not stored here at all — it lives in
    ``plant_board.constants.WORKFORCE_DEPARTMENTS``, because it is a statement
    about the factory rather than a figure somebody types.

    BOTH FIGURES ARE NULLABLE AND MUST STAY SO. Unset has to read as "nobody has
    told the board" and not as zero: a department with nobody assigned would
    otherwise look identical to a department nobody has configured, and on a
    wall that difference is the whole point of showing the number.
    """

    company_code = models.CharField(max_length=50)
    #: Key from ``WORKFORCE_DEPARTMENTS``, or one slugified from a label an
    #: operator typed. Not a foreign key: the catalogue is code, so a row whose
    #: key has been retired is ignored rather than orphaned.
    department = models.CharField(max_length=64)

    # --- Departments an operator added -------------------------------------
    #
    # BLANK ON A BUILT-IN, AND THAT IS HOW THE TWO ARE TOLD APART. A department
    # that ships with the board takes its label, band and kind from
    # ``plant_board.constants.WORKFORCE_DEPARTMENTS`` and leaves these empty, so
    # a deploy that re-bands one moves it everywhere at once and no stale copy
    # survives in the database. A department somebody added has no entry in code
    # to read, so it carries its own here.
    #
    # This is also the line the settings page draws: a row with a band of its
    # own can be deleted and re-banded, a row without one cannot. Which keeps
    # the original guarantee — nobody can move Oil Production out of Production
    # by editing this table — while still letting the plant grow a department.
    label = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="Display name. Blank on a built-in department, which takes "
                  "its name from the code catalogue.",
    )
    band = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Which band's workforce strip this department reports under: "
                  "purchase, store, production or shifting. Blank on a built-in.",
    )
    kind = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="'employee' for people on the payroll, 'labour' for hired in. "
                  "Blank on a built-in.",
    )

    employees = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="People in this department. Leave empty if nobody has counted.",
    )
    salary_monthly = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "The department's wage bill for a MONTH. The board divides it by "
            "the days in the month to show a daily run rate."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plant_board_workforce_updates",
    )

    class Meta:
        unique_together = ("company_code", "department")
        verbose_name = "Plant board workforce"
        verbose_name_plural = "Plant board workforce"

    def __str__(self):
        return f"{self.department} @ {self.company_code}"


class PlantBoardSettings(models.Model):
    """Board figures for the plant that belong to the company, not a warehouse.

    Companion to :class:`LogisticsBoardSettings` and here for the same reason:
    this app already owns the operations boards' configuration.

    ``sqft_per_pallet`` -- THE LAST STEP FROM STOCK TO FLOOR. SAP cannot bridge
    pieces and square feet: verified on 12 September 2026 against all 878
    packaging items in Oil, ``OITM`` holds no volume (``SVolume``, ``BVolume``
    zero everywhere), no dimensions (``SLength1``/``SWidth1``/``SHeight1``
    likewise) and a gross weight on only 155 of them.

    The factory bridges it in two measured steps instead, and this is the
    second. The first lives in ``plant_board/data/stacking.json``: how many
    pieces of each item fit on a pallet, measured per item rather than blended,
    so a carton and a cap are never treated as the same size. This field is the
    footprint of one pallet -- 15 sq ft, as measured -- and it is per company
    because a site with different pallets has a different number.

    Nullable, and must stay so: unset has to read as "nobody has measured this"
    rather than as an empty warehouse.
    """

    company_code = models.CharField(max_length=50, unique=True)

    sqft_per_pallet = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        default=15,
        help_text=(
            "Floor a single pallet stands on. The factory measured 15 sq ft. "
            "Leave empty and the board reports the floor and the stock "
            "separately rather than guessing how full the stores are."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plant_board_settings_updates",
    )

    class Meta:
        verbose_name = "Plant board settings"
        verbose_name_plural = "Plant board settings"

    def __str__(self):
        return f"Plant board settings for {self.company_code}"
