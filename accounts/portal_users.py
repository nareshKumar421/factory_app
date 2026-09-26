"""Bring SAP Portal's users into JI (``manage.py import_portal_users``).

SAP Portal kept its own users table (``ZCUST_USERS`` in SAP's Oil schema) with
a username, a bcrypt password, one of four roles, a list of sidebar modules and
one numeric SAP user id. JI has none of those shapes (sap-portal-merge plan,
decision D3), so a portal user becomes:

* a JI login, matched to an existing one by email or created without a usable
  password — portal password hashes are never carried over, so a new user sets
  a password through the normal reset;
* ``UserCompany`` rows for the companies they worked in (portal users could act
  on every company; which ones to grant is the operator's choice);
* membership of the groups that correspond to their portal modules and role
  (``group_names_for``);
* a ``SapApproverIdentity`` per company, translating the portal's one numeric
  ``OUSR.USERID`` into that company's ``USER_CODE``.

Everything is additive: an existing user keeps their name, active flag, groups,
companies and SAP identities; nothing is removed. ``plan()`` decides everything
without writing, so ``--dry-run`` shows exactly what ``apply()`` will do.
"""

import json
import re
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction

from company.models import Company, UserCompany, UserRole
from sap_client.models import SapApproverIdentity

# The portal's seed logins (backend_v1/services/hanaUsers.js:113-124) — never people.
SEED_USERNAMES = ("admin", "manager1", "srmanager1")
# Addresses the portal seeded or that cannot receive a reset link.
_PLACEHOLDER_EMAIL = re.compile(r"@(company\.com|example\.(com|org)|test)$", re.IGNORECASE)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Roles that the portal let past every module check (server.js requireRole,
# public/sidebar.js isAdmin): their module list meant nothing.
UNRESTRICTED_ROLES = ("admin", "sap_adder")

# Every module the portal's sidebar knew (public/sidebar.js:8-28).
ALL_MODULES = (
    "bom", "production", "grpo", "approvals", "customers", "vendors", "budget",
    "journal-entries", "documents", "sap-approvals", "credit-notes", "reports",
)

# Portal module -> JI groups, when the group does not depend on the role.
MODULE_GROUPS = {
    "journal-entries": ["SAP Finance - Ledger Viewer"],
    "budget": ["SAP Finance - Budget Editor"],
    # The portal let anyone with Documents download attachments.
    "documents": ["SAP Documents - Viewer with attachments"],
    # ... and decide or withdraw any request that was theirs to act on.
    "sap-approvals": ["SAP Approvals - Approver"],
    "credit-notes": ["Credit Note A/R Approver", "Credit Note A/P Approver"],
    "credit-notes-ar": ["Credit Note A/R Approver"],
    "credit-notes-ap": ["Credit Note A/P Approver"],
    "production": ["Production SAP Orders"],
    "reports": ["SAP Reports"],
}

# Portal module -> {role: groups}, where the role decides the step a person takes.
ROLE_MODULE_GROUPS = {
    # BOM approval levels (server.js LEVEL_ROLES): manager L1, sr_manager L2,
    # sap_adder the final push, admin the direct create/update.
    "bom": {
        "manager": ["BOM Changes - Level 1 Approver"],
        "sr_manager": ["BOM Changes - Level 2 Approver"],
        "sap_adder": ["BOM Changes - SAP Pusher"],
        "admin": ["BOM Changes - Admin"],
    },
}
ROLE_MODULE_DEFAULT = {"bom": ["BOM Changes - Requester"]}

# Modules with nothing to grant, and why (shown in the report).
NOT_GRANTED = {
    "grpo": "GRPO without a purchase order was not merged (decision D4); use the GRPO module.",
    "home": "",
    "admin": "Portal user admin maps to Django admin; grant it by hand if needed.",
}


def register_module_groups(module: str, groups_by_role: dict, default: list | None = None) -> None:
    """Let another merged app declare its role-dependent groups for a portal module."""
    ROLE_MODULE_GROUPS[module] = groups_by_role
    if default is not None:
        ROLE_MODULE_DEFAULT[module] = default


def parse_modules(value):
    """The portal's MODULES column: JSON text, a list, or NULL (= every module)."""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [str(m) for m in value]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [part.strip() for part in str(value).split(",") if part.strip()]
    return [str(m) for m in parsed] if isinstance(parsed, list) else None


def group_names_for(role: str, modules) -> tuple[list[str], list[str]]:
    """(groups to grant, notes) for a portal role and module list."""
    groups: list[str] = []
    notes: list[str] = []
    for module in modules:
        if module in MODULE_GROUPS:
            groups.extend(MODULE_GROUPS[module])
        elif module in ROLE_MODULE_GROUPS:
            by_role = ROLE_MODULE_GROUPS[module]
            groups.extend(by_role.get(role) or ROLE_MODULE_DEFAULT.get(module, []))
        elif module in NOT_GRANTED:
            if NOT_GRANTED[module]:
                notes.append(f"{module}: {NOT_GRANTED[module]}")
        else:
            notes.append(f"{module}: no JI group is mapped to this portal module.")
    unique = list(dict.fromkeys(groups))
    return unique, notes


@dataclass
class UserPlan:
    portal_id: object
    username: str
    email: str
    full_name: str
    active: bool
    existing_user_id: int | None = None
    groups: list[str] = field(default_factory=list)
    missing_groups: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    identities: dict[str, str] = field(default_factory=dict)  # company code -> USER_CODE
    notes: list[str] = field(default_factory=list)
    skip_reason: str = ""


def plan(rows, *, companies, sap_codes, grant_unrestricted=False, skip_usernames=SEED_USERNAMES):
    """Decide, without writing, what importing ``rows`` would do.

    ``sap_codes`` is ``{company code: {USERID: {"user_code", "user_name"}}}``.
    """
    User = get_user_model()
    existing_groups = set(Group.objects.values_list("name", flat=True))
    plans = []
    for row in rows:
        username = str(row.get("USERNAME") or "").strip()
        email = str(row.get("EMAIL") or "").strip().lower()
        item = UserPlan(
            portal_id=row.get("ID"),
            username=username,
            email=email,
            full_name=str(row.get("FULL_NAME") or "").strip() or username,
            active=str(row.get("ACTIVE", 1)) in ("1", "True", "true", "Y"),
        )
        plans.append(item)
        if username.lower() in {u.lower() for u in skip_usernames}:
            item.skip_reason = "portal seed account"
            continue
        if not email or not _EMAIL.match(email) or _PLACEHOLDER_EMAIL.search(email):
            item.skip_reason = f"no usable email ({email or 'blank'}) — add the person by hand"
            continue

        user = User.objects.filter(email__iexact=email).first()
        item.existing_user_id = user.pk if user else None

        role = str(row.get("ROLE") or "").strip()
        modules = parse_modules(row.get("MODULES"))
        unrestricted = modules is None or role in UNRESTRICTED_ROLES
        if unrestricted and not grant_unrestricted:
            modules_to_grant = [m for m in (modules or []) if modules is not None]
            item.notes.append(
                "unrestricted in the portal (role %s%s): only its listed modules were mapped; "
                "review by hand or rerun with --grant-unrestricted"
                % (role or "?", ", no module list" if modules is None else "")
            )
        else:
            modules_to_grant = list(ALL_MODULES) if unrestricted else modules
        groups, notes = group_names_for(role, modules_to_grant)
        item.notes.extend(notes)
        item.groups = [g for g in groups if g in existing_groups]
        item.missing_groups = [g for g in groups if g not in existing_groups]
        item.companies = list(companies)

        sap_user_id = row.get("SAP_USER_ID")
        if sap_user_id not in (None, ""):
            for code in companies:
                found = (sap_codes.get(code) or {}).get(int(sap_user_id))
                if found and found.get("user_code"):
                    item.identities[code] = found["user_code"].upper()
                else:
                    item.notes.append(f"{code}: SAP user id {sap_user_id} not found — map on the SAP Identities page")
    return plans


def apply(plans, *, role_name="SAP Portal"):
    """Write the planned changes. Returns (counts, messages). Additive only.

    ``messages`` are what could only be found while writing: a SAP account that
    already belongs to someone else, a person already mapped to another code.
    """
    User = get_user_model()
    role, _ = UserRole.objects.get_or_create(name=role_name)
    companies = {c.code: c for c in Company.objects.all()}
    groups = {g.name: g for g in Group.objects.all()}
    counts = {"created": 0, "matched": 0, "skipped": 0, "groups_added": 0, "companies_added": 0, "identities_added": 0}
    messages: list[str] = []
    with transaction.atomic():
        for item in plans:
            if item.skip_reason:
                counts["skipped"] += 1
                continue
            user = User.objects.filter(email__iexact=item.email).first()
            if user is None:
                user = User.objects.create_user(
                    email=item.email, full_name=item.full_name, password=None, is_active=item.active
                )
                counts["created"] += 1
            else:
                counts["matched"] += 1
            held = set(user.groups.values_list("name", flat=True))
            for name in item.groups:
                if name not in held:
                    user.groups.add(groups[name])
                    counts["groups_added"] += 1
            has_default = UserCompany.objects.filter(user=user, is_default=True).exists()
            for code in item.companies:
                company = companies.get(code)
                if company is None:
                    messages.append(f"{item.email}: {code}: no such company in JI")
                    continue
                _, created = UserCompany.objects.get_or_create(
                    user=user, company=company, defaults={"role": role, "is_default": not has_default}
                )
                if created:
                    counts["companies_added"] += 1
                    has_default = True
            for code, sap_code in item.identities.items():
                company = companies.get(code)
                if company is None:
                    continue
                mine = SapApproverIdentity.objects.filter(user=user, company=company).first()
                if mine:
                    if mine.sap_user_code != sap_code:
                        messages.append(
                            f"{item.email}: {code}: already mapped to {mine.sap_user_code}, "
                            f"portal says {sap_code} — left as is"
                        )
                    continue
                taken = SapApproverIdentity.objects.filter(company=company, sap_user_code=sap_code).first()
                if taken:
                    messages.append(f"{item.email}: {code}: {sap_code} already belongs to another user — not mapped")
                    continue
                SapApproverIdentity.objects.create(user=user, company=company, sap_user_code=sap_code)
                counts["identities_added"] += 1
    return counts, messages
