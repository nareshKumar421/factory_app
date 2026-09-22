"""
What the API sends and accepts.

Two things here are worth reading before the rest.

**Salary is never a plain field.** :class:`EmployeeListSerializer` and
:class:`EmployeeDetailSerializer` expose ``salary`` as a method field that
consults the request's :class:`~employee_hierarchy.access.SalaryReach` per row
and returns ``None`` for anyone the viewer may not see. A hidden figure is
``None``, not zero and not omitted -- the screens need to tell "you cannot see
this" apart from "there is no salary on record", and those mean very different
things to an HR user looking at a new joiner.

**Nothing writable touches the tree.** ``hierarchy_path``, ``hierarchy_level``
and ``full_name`` are read-only everywhere. They are derived, and the only code
allowed to derive them lives in :mod:`employee_hierarchy.hierarchy`. A manager
change comes in through its own endpoint and its own serializer, so it can
carry the reason the audit trail needs.
"""

from django.utils import timezone
from rest_framework import serializers

from accounts.models import Department as OrgDepartment, User

from .access import salary_reach
from .constants import EmploymentStatus, LabourShift, RecordStatus, RevisionType
from .models import (
    Branch,
    Department,
    Designation,
    Employee,
    EmployeeAuditLog,
    EmployeeHistory,
    EmployeeSalary,
    PermanentLabourAudit,
    PermanentLabourPresence,
    PermanentLabourStrength,
    SalaryRevision,
)


def photo_url(employee, request):
    if not employee.photo:
        return None
    url = employee.photo.url
    return request.build_absolute_uri(url) if request is not None else url


class UserBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "full_name", "email", "employee_code"]


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------


class EmployeeBriefSerializer(serializers.ModelSerializer):
    """The card-sized employee: enough for a chart node, a chip or a row.

    Used everywhere one employee is mentioned inside another's payload --
    manager, direct reports, peers, department head -- so it stays small on
    purpose. No salary, ever.
    """

    department_name = serializers.CharField(source="department.name", default=None, read_only=True)
    designation_name = serializers.CharField(source="designation.name", default=None, read_only=True)
    status_display = serializers.CharField(source="get_employment_status_display", read_only=True)
    photo = serializers.SerializerMethodField()
    initials = serializers.CharField(read_only=True)

    class Meta:
        model = Employee
        fields = [
            "id",
            "employee_code",
            "full_name",
            "email",
            "phone",
            "photo",
            "initials",
            "job_title",
            "department",
            "department_name",
            "designation",
            "designation_name",
            "employment_status",
            "status_display",
            "hierarchy_level",
            "is_manager",
            "location",
        ]

    def get_photo(self, employee):
        return photo_url(employee, self.context.get("request"))


class DepartmentSerializer(serializers.ModelSerializer):
    head_detail = EmployeeBriefSerializer(source="head", read_only=True)
    employee_count = serializers.IntegerField(read_only=True, default=0)
    total_employee_count = serializers.IntegerField(read_only=True, default=0)
    parent_name = serializers.CharField(source="parent.name", default=None, read_only=True)

    class Meta:
        model = Department
        fields = [
            "id",
            "code",
            "name",
            "description",
            "head",
            "head_detail",
            "parent",
            "parent_name",
            "status",
            "sort_order",
            "employee_count",
            "total_employee_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate_head(self, head):
        """A department head has to be an employee of the same company.

        The company comes from the request context rather than the payload:
        headers decide which plant is being edited, and a body that could name
        another company's employee would be a way around that.
        """
        company = self.context.get("company")
        if head is not None and company is not None and head.company_id != company.id:
            raise serializers.ValidationError(
                "The department head must be an employee of this company."
            )
        return head


class DesignationSerializer(serializers.ModelSerializer):
    employee_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Designation
        fields = [
            "id",
            "name",
            "code",
            "description",
            "level",
            "is_managerial",
            "status",
            "employee_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class BranchSerializer(serializers.ModelSerializer):
    """A branch master row.

    ``is_default`` is writable, but setting it does not simply write the
    column: the view hands it to
    :func:`employee_hierarchy.services.make_default_branch`, which demotes the
    branch that held it. Writing it here directly would be refused by
    ``uniq_default_branch_per_company`` the moment a second branch claimed it.
    """

    employee_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Branch
        fields = [
            "id",
            "code",
            "name",
            "description",
            "status",
            "is_default",
            "employee_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate_code(self, code):
        return self._unique("code", code.strip(), f"Branch code {code.strip()} is already used")

    def validate_name(self, name):
        return self._unique("name", name.strip(), f"A branch called {name.strip()} already exists")

    def _unique(self, field, value, message):
        """Refuse a duplicate here rather than letting the database do it.

        ``uniq_branch_code_per_company`` and its name twin are real constraints,
        but ``company`` is not a field on this serializer -- it comes from the
        request header, not the body -- so DRF cannot build its own uniqueness
        validator and the clash surfaced as a 500 instead of a message anybody
        could act on.
        """
        company = self.context.get("company")
        if company is None or not value:
            return value
        clash = Branch.objects.filter(company=company, **{f"{field}__iexact": value})
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(f"{message} in this company.")
        return value


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------


class SalaryRecordSerializer(serializers.ModelSerializer):
    """One salary record. Only ever reached through a salary-gated view."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    approved_by_detail = UserBriefSerializer(source="approved_by", read_only=True)
    created_by_detail = UserBriefSerializer(source="created_by", read_only=True)
    revision = serializers.SerializerMethodField()

    class Meta:
        model = EmployeeSalary
        fields = [
            "id",
            "basic_salary",
            "allowances",
            "bonuses",
            "deductions",
            "total_compensation",
            "currency",
            "effective_from",
            "revision_date",
            "status",
            "status_display",
            "approved_by",
            "approved_by_detail",
            "approved_at",
            "notes",
            "revision",
            "created_at",
            "created_by_detail",
        ]

    def get_revision(self, record):
        revision = getattr(record, "revision", None)
        if revision is None:
            return None
        return {
            "id": revision.id,
            "revision_type": revision.revision_type,
            "revision_type_display": revision.get_revision_type_display(),
            "previous_amount": revision.previous_amount,
            "new_amount": revision.new_amount,
            "change_amount": revision.change_amount,
            "change_percent": (
                round(float(revision.change_percent), 2)
                if revision.change_percent is not None
                else None
            ),
            "reason": revision.reason,
            "notes": revision.notes,
            "effective_date": revision.effective_date,
            "revision_date": revision.revision_date,
        }


class SalaryRevisionSerializer(serializers.ModelSerializer):
    """A revision on its own, for the revision-history report."""

    employee_detail = EmployeeBriefSerializer(source="employee", read_only=True)
    revision_type_display = serializers.CharField(
        source="get_revision_type_display", read_only=True
    )
    approved_by_detail = UserBriefSerializer(source="approved_by", read_only=True)
    created_by_detail = UserBriefSerializer(source="created_by", read_only=True)
    change_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    status = serializers.CharField(source="salary_record.status", read_only=True)

    class Meta:
        model = SalaryRevision
        fields = [
            "id",
            "employee",
            "employee_detail",
            "salary_record",
            "previous_amount",
            "new_amount",
            "change_amount",
            "currency",
            "effective_date",
            "revision_date",
            "revision_type",
            "revision_type_display",
            "reason",
            "notes",
            "status",
            "approved_by_detail",
            "created_by_detail",
            "created_at",
        ]


class SalaryWriteSerializer(serializers.Serializer):
    """A proposed revision.

    Components rather than a single total, because that is what a revision
    letter says and because the total has to remain a *derived* number -- one
    place computing it, one answer.
    """

    basic_salary = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    allowances = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, default=0
    )
    bonuses = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, default=0
    )
    deductions = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, default=0
    )
    currency = serializers.CharField(max_length=3, required=False, allow_blank=True)
    effective_from = serializers.DateField()
    revision_date = serializers.DateField(required=False)
    revision_type = serializers.ChoiceField(
        choices=RevisionType.choices, default=RevisionType.ANNUAL_INCREMENT
    )
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    #: Ignored unless the caller holds the approval right -- see the view.
    approve = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        total = (
            attrs["basic_salary"]
            + (attrs.get("allowances") or 0)
            + (attrs.get("bonuses") or 0)
            - (attrs.get("deductions") or 0)
        )
        if total <= 0:
            raise serializers.ValidationError(
                "Total compensation has to be more than zero. Check the deductions."
            )
        return attrs


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


class _SalaryMixin(serializers.ModelSerializer):
    """Adds the permission-checked ``salary`` block.

    ``None`` means "not yours to see"; a block with ``amount: null`` means
    "nobody has put a salary on record yet". The distinction is the whole
    reason this is a method field.
    """

    salary = serializers.SerializerMethodField()

    def get_salary(self, employee):
        request = self.context.get("request")
        if request is None:
            return None
        reach = salary_reach(request)
        if not reach.can_view(employee):
            return None

        # The detail view has already fetched the record in force and passes it
        # in; a list row reads the cached figure, which is what that cache is
        # for -- a hundred rows must not become a hundred salary lookups.
        record = self.context.get("current_salary")
        if record is not None and record.employee_id == employee.id:
            return {
                "amount": record.total_compensation,
                "currency": record.currency,
                "effective_from": record.effective_from,
                "can_view_history": reach.can_view_history(employee),
            }
        return {
            "amount": employee.current_salary_amount,
            "currency": employee.current_salary_currency or None,
            "effective_from": None,
            "can_view_history": reach.can_view_history(employee),
        }


class EmployeeListSerializer(_SalaryMixin):
    """A directory row: who they are, where they sit, how big their team is."""

    department_name = serializers.CharField(source="department.name", default=None, read_only=True)
    designation_name = serializers.CharField(source="designation.name", default=None, read_only=True)
    branch_name = serializers.CharField(source="branch.name", default=None, read_only=True)
    status_display = serializers.CharField(source="get_employment_status_display", read_only=True)
    manager_name = serializers.CharField(
        source="reporting_manager.full_name", default=None, read_only=True
    )
    manager_code = serializers.CharField(
        source="reporting_manager.employee_code", default=None, read_only=True
    )
    direct_report_count = serializers.IntegerField(read_only=True, default=0)
    photo = serializers.SerializerMethodField()
    initials = serializers.CharField(read_only=True)

    class Meta:
        model = Employee
        fields = [
            "id",
            "employee_code",
            "full_name",
            "first_name",
            "last_name",
            "email",
            "phone",
            "photo",
            "initials",
            "job_title",
            "location",
            "joining_date",
            "employment_status",
            "status_display",
            "department",
            "department_name",
            "designation",
            "designation_name",
            "branch",
            "branch_name",
            "reporting_manager",
            "manager_name",
            "manager_code",
            "hierarchy_level",
            "is_manager",
            "direct_report_count",
            "salary",
        ]

    def get_photo(self, employee):
        return photo_url(employee, self.context.get("request"))


class EmployeeDetailSerializer(_SalaryMixin):
    """One employee, in full, plus everything about their place in the org."""

    department_detail = DepartmentSerializer(source="department", read_only=True)
    designation_detail = DesignationSerializer(source="designation", read_only=True)
    branch_detail = BranchSerializer(source="branch", read_only=True)
    manager = EmployeeBriefSerializer(source="reporting_manager", read_only=True)
    status_display = serializers.CharField(source="get_employment_status_display", read_only=True)
    user_detail = UserBriefSerializer(source="user", read_only=True)
    photo = serializers.SerializerMethodField()
    initials = serializers.CharField(read_only=True)
    direct_report_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Employee
        fields = [
            "id",
            "employee_code",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "photo",
            "initials",
            "date_of_birth",
            "joining_date",
            "exit_date",
            "employment_status",
            "status_display",
            "department",
            "department_detail",
            "designation",
            "designation_detail",
            "branch",
            "branch_detail",
            "job_title",
            "location",
            "reporting_manager",
            "manager",
            "hierarchy_level",
            "hierarchy_path",
            "is_manager",
            "direct_report_count",
            "user",
            "user_detail",
            "salary",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["full_name", "hierarchy_level", "hierarchy_path"]

    def get_photo(self, employee):
        return photo_url(employee, self.context.get("request"))


class EmployeeWriteSerializer(serializers.ModelSerializer):
    """Creating and editing an employee.

    ``reporting_manager`` is accepted here only when *creating*: placing a new
    joiner under their manager is part of hiring them. Changing an existing
    employee's manager goes through the manager endpoint instead, which takes a
    reason and moves their team -- see
    :func:`employee_hierarchy.services.change_manager`.
    """

    initial_salary = SalaryWriteSerializer(required=False, allow_null=True)

    class Meta:
        model = Employee
        fields = [
            "employee_code",
            "first_name",
            "last_name",
            "email",
            "phone",
            "photo",
            "date_of_birth",
            "joining_date",
            "employment_status",
            "department",
            "designation",
            "branch",
            "job_title",
            "location",
            "reporting_manager",
            "is_manager",
            "user",
            "initial_salary",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = self.context.get("company")
        if company is not None:
            self.fields["department"].queryset = Department.objects.filter(company=company)
            self.fields["designation"].queryset = Designation.objects.filter(company=company)
            # Retired branches stay selectable only for somebody who already
            # holds one -- offering one on a fresh hire is how a withdrawn
            # branch keeps gaining people.
            branches = Branch.objects.filter(company=company)
            if self.instance is None or self.instance.branch_id is None:
                branches = branches.filter(status=RecordStatus.ACTIVE)
            self.fields["branch"].queryset = branches
            self.fields["reporting_manager"].queryset = Employee.objects.filter(company=company)

    def validate_employee_code(self, code):
        company = self.context.get("company")
        code = code.strip()
        existing = Employee.objects.filter(company=company, employee_code__iexact=code)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError(
                f"Employee code {code} is already used in this company."
            )
        return code

    def validate_email(self, email):
        if not email:
            return ""
        company = self.context.get("company")
        existing = Employee.objects.filter(company=company, email__iexact=email)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("Another employee already uses this email.")
        return email


class ManagerChangeSerializer(serializers.Serializer):
    """Moving somebody in the tree.

    ``manager: null`` is "remove manager" -- it makes them a top-level
    employee, which is a real thing to want and not an error.

    ``carry_team`` defaults to true because that is what the brief requires:
    moving a manager preserves their subordinate hierarchy.
    """

    manager = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.all(), allow_null=True
    )
    carry_team = serializers.BooleanField(required=False, default=True)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class DepartmentChangeSerializer(serializers.Serializer):
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(), allow_null=True
    )
    include_team = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class DesignationChangeSerializer(serializers.Serializer):
    designation = serializers.PrimaryKeyRelatedField(
        queryset=Designation.objects.all(), allow_null=True
    )
    promotion = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class PromotionSerializer(serializers.Serializer):
    """A promotion: the new rung, and whatever else moved with it."""

    designation = serializers.PrimaryKeyRelatedField(
        queryset=Designation.objects.all(), required=False, allow_null=True
    )
    manager = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.all(), required=False, allow_null=True
    )
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(), required=False, allow_null=True
    )
    salary = SalaryWriteSerializer(required=False, allow_null=True)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class StatusChangeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=EmploymentStatus.choices)
    exit_date = serializers.DateField(required=False, allow_null=True)
    reassign_reports_to = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.all(), required=False, allow_null=True
    )
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class DecisionSerializer(serializers.Serializer):
    """Approving or rejecting a proposed revision."""

    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# History and audit
# ---------------------------------------------------------------------------


class EmployeeHistorySerializer(serializers.ModelSerializer):
    event_display = serializers.CharField(source="get_event_display", read_only=True)
    created_by_detail = UserBriefSerializer(source="created_by", read_only=True)

    class Meta:
        model = EmployeeHistory
        fields = [
            "id",
            "event",
            "event_display",
            "occurred_on",
            "from_value",
            "to_value",
            "notes",
            "created_at",
            "created_by_detail",
        ]


class EmployeeAuditSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    performed_by_detail = UserBriefSerializer(source="performed_by", read_only=True)

    class Meta:
        model = EmployeeAuditLog
        fields = [
            "id",
            "action",
            "action_display",
            "field",
            "previous_value",
            "new_value",
            "performed_at",
            "performed_by_detail",
            "reason",
            "notes",
        ]


# ---------------------------------------------------------------------------
# Permanent labour: the strength, and the day's presence
# ---------------------------------------------------------------------------


class PermanentLabourStrengthSerializer(serializers.ModelSerializer):
    """One department's strength. The plant-wide figure is the sum of these."""

    updated_by_detail = UserBriefSerializer(source="updated_by", read_only=True)
    department_name = serializers.CharField(
        source="department.name", read_only=True, default=None
    )

    class Meta:
        model = PermanentLabourStrength
        fields = [
            "id",
            "department",
            "department_name",
            "headcount",
            "note",
            "updated_at",
            "updated_by_detail",
        ]
        read_only_fields = ["id", "department_name", "updated_at", "updated_by_detail"]


class PermanentLabourStrengthWriteSerializer(serializers.Serializer):
    """A strength being set, for one department or for the undivided plant.

    ``department`` is required but may be null, deliberately: leaving it out of
    the payload is far more likely to be a screen that forgot to send it than a
    plant declaring it does not split its labour, and the two write to different
    rows. Making the caller say which it means keeps a department's figure from
    silently landing in the undivided bucket.
    """

    department = serializers.PrimaryKeyRelatedField(
        queryset=OrgDepartment.objects.all(), allow_null=True
    )
    headcount = serializers.IntegerField(min_value=0)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class PermanentLabourPresenceSerializer(serializers.ModelSerializer):
    shift_display = serializers.CharField(source="get_shift_display", read_only=True)
    absent_count = serializers.IntegerField(read_only=True)
    is_over_strength = serializers.BooleanField(read_only=True)
    recorded_by_detail = UserBriefSerializer(source="updated_by", read_only=True)
    department_name = serializers.CharField(
        source="department.name", read_only=True, default=None
    )
    # A real row is one department's count and can be corrected; the totals the
    # view synthesises for "all departments" carry this false, and the screen
    # reads it rather than guessing from a null id.
    is_editable = serializers.SerializerMethodField()

    class Meta:
        model = PermanentLabourPresence
        fields = [
            "id",
            "department",
            "department_name",
            "is_editable",
            "work_date",
            "shift",
            "shift_display",
            "present_count",
            "strength",
            "absent_count",
            "is_over_strength",
            "remark",
            "recorded_by_detail",
            "created_at",
            "updated_at",
        ]

    def get_is_editable(self, row):
        return True


class PermanentLabourPresenceWriteSerializer(serializers.Serializer):
    """One shift's count, written by date + shift rather than by row id.

    The register has exactly one row per company, department, date and shift,
    and the screen's user thinks in those terms ("production, today, day shift,
    seventy-eight"), so the endpoint takes the key rather than making them find
    the row first. The view upserts on it.

    ``department`` is required-but-nullable for the same reason it is on the
    strength: a missing department and an undivided plant are different rows,
    and the caller has to say which one it means.

    ``strength`` is not accepted: it is snapshotted from the master, so the
    number a day is measured against cannot be typed into the same form as the
    count and quietly disagree with the register.
    """

    department = serializers.PrimaryKeyRelatedField(
        queryset=OrgDepartment.objects.all(), allow_null=True
    )
    work_date = serializers.DateField()
    shift = serializers.ChoiceField(choices=LabourShift.choices)
    present_count = serializers.IntegerField(min_value=0)
    remark = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def validate_work_date(self, work_date):
        # A presence count is a record of a shift that happened. Tomorrow's has
        # not, and a typo in the year is far likelier than a genuine future
        # entry.
        if work_date > timezone.localdate():
            raise serializers.ValidationError("A shift in the future has not happened yet.")
        return work_date


class PermanentLabourAuditSerializer(serializers.ModelSerializer):
    performed_by_detail = UserBriefSerializer(source="performed_by", read_only=True)
    is_first = serializers.BooleanField(read_only=True)
    department_name = serializers.CharField(
        source="department.name", read_only=True, default=None
    )
    shift_display = serializers.SerializerMethodField()

    class Meta:
        model = PermanentLabourAudit
        fields = [
            "id",
            "subject",
            "department",
            "department_name",
            "work_date",
            "shift",
            "shift_display",
            "previous_count",
            "new_count",
            "previous_remark",
            "new_remark",
            "strength",
            "is_first",
            "performed_at",
            "performed_by_detail",
        ]

    def get_shift_display(self, entry):
        return dict(LabourShift.choices).get(entry.shift, entry.shift)
