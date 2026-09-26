"""Import SAP Portal's users (``ZCUST_USERS``) as JI users, groups and SAP identities.

    # 1. Export the portal's users (run by an operator on the SAP side; the
    #    PASSWORD column is deliberately not exported — hashes are not carried over):
    #    SELECT "ID","USERNAME","FULL_NAME","EMAIL","ROLE","ACTIVE","MODULES","SAP_USER_ID"
    #    FROM "JIVO_OIL_HANADB"."ZCUST_USERS"
    # 2. See what would happen:
    python manage.py import_portal_users --from-file zcust_users.json --read-sap --dry-run
    # 3. Do it:
    python manage.py import_portal_users --from-file zcust_users.json --read-sap --yes

``--read-sap`` translates each portal ``SAP_USER_ID`` (``OUSR.USERID``) into the
company's ``USER_CODE`` by reading HANA — a production-server step. Without SAP,
pass ``--sap-users-file`` with ``{"JIVO_OIL": {"12": "USER12", ...}, ...}`` (or
leave both out and map people on the SAP Identities page afterwards).

Run the merged apps' ``setup_*_groups`` commands first: a group that does not
exist yet is reported and not created here. See ``accounts/portal_users.py``.
"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from accounts import portal_users
from company.models import Company


class Command(BaseCommand):
    help = "Import SAP Portal users (ZCUST_USERS export) as JI users, groups and SAP identities."

    def add_arguments(self, parser):
        parser.add_argument("--from-file", required=True, help="JSON array of ZCUST_USERS rows.")
        parser.add_argument("--companies", default="", help="Company codes to grant (default: every company in JI).")
        parser.add_argument("--role-name", default="SAP Portal", help="UserRole label for new UserCompany rows.")
        parser.add_argument("--read-sap", action="store_true", help="Read OUSR per company to translate SAP user ids.")
        parser.add_argument("--sap-users-file", default="", help="JSON {company: {USERID: USER_CODE}} instead of --read-sap.")
        parser.add_argument("--grant-unrestricted", action="store_true",
                            help="Treat portal admins, sap_adders and users with no module list as holding every module.")
        parser.add_argument("--skip-usernames", default=",".join(portal_users.SEED_USERNAMES),
                            help="Portal usernames never imported (default: the seed accounts).")
        parser.add_argument("--dry-run", action="store_true", help="Report the plan; write nothing.")
        parser.add_argument("--yes", action="store_true", help="Required to write.")

    def handle(self, *args, **options):
        rows = self._rows(options["from_file"])
        if any("PASSWORD" in row for row in rows):
            self.stdout.write(self.style.WARNING("PASSWORD values in the file are ignored: hashes are never imported."))
        codes = [c.strip() for c in options["companies"].split(",") if c.strip()] or list(
            Company.objects.values_list("code", flat=True)
        )
        sap_codes = self._sap_codes(options, codes, rows)
        plans = portal_users.plan(
            rows,
            companies=codes,
            sap_codes=sap_codes,
            grant_unrestricted=options["grant_unrestricted"],
            skip_usernames=[u.strip() for u in options["skip_usernames"].split(",") if u.strip()],
        )
        self._report(plans)
        if options["dry_run"]:
            self.stdout.write(self.style.NOTICE("Dry run: nothing written."))
            return
        if not options["yes"]:
            raise CommandError("Pass --yes to write (or --dry-run to look first).")
        self.stdout.write(f"Writing to database {connection.settings_dict.get('NAME')!r}…")
        counts, messages = portal_users.apply(plans, role_name=options["role_name"])
        for message in messages:
            self.stdout.write(self.style.WARNING(f"    {message}"))
        self.stdout.write(self.style.SUCCESS(", ".join(f"{k}: {v}" for k, v in counts.items())))

    def _rows(self, path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise CommandError(f"Cannot read {path}: {exc}")
        if isinstance(data, dict) and len(data) == 1:
            data = next(iter(data.values()))  # a SQL tool's {"<query>": [...]} export
        if not isinstance(data, list):
            raise CommandError("The file must hold a JSON array of ZCUST_USERS rows.")
        return data

    def _sap_codes(self, options, codes, rows):
        if options["sap_users_file"]:
            try:
                with open(options["sap_users_file"], encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, ValueError) as exc:
                raise CommandError(f"Cannot read {options['sap_users_file']}: {exc}")
            return {
                company: {int(k): {"user_code": str(v), "user_name": ""} for k, v in (mapping or {}).items()}
                for company, mapping in raw.items()
            }
        if not options["read_sap"]:
            return {}
        from sap_client.client import SAPClient

        ids = [row.get("SAP_USER_ID") for row in rows if row.get("SAP_USER_ID") not in (None, "")]
        result = {}
        for code in codes:
            try:
                result[code] = SAPClient(company_code=code).sap_user_codes_by_id(ids)
            except Exception as exc:  # report and carry on; identities can be mapped by hand
                self.stdout.write(self.style.WARNING(f"{code}: could not read SAP users ({exc})"))
                result[code] = {}
        return result

    def _report(self, plans):
        for item in plans:
            head = f"[{item.portal_id}] {item.username} <{item.email or '-'}>"
            if item.skip_reason:
                self.stdout.write(self.style.WARNING(f"SKIP {head}: {item.skip_reason}"))
                continue
            action = "match user %s" % item.existing_user_id if item.existing_user_id else "create user"
            self.stdout.write(f"{head}: {action}; companies {', '.join(item.companies) or '-'}")
            if item.groups:
                self.stdout.write(f"    groups: {', '.join(item.groups)}")
            if item.missing_groups:
                self.stdout.write(self.style.WARNING(f"    groups not set up yet: {', '.join(item.missing_groups)}"))
            if item.identities:
                self.stdout.write("    SAP: " + ", ".join(f"{c}={u}" for c, u in item.identities.items()))
            for note in item.notes:
                self.stdout.write(f"    note: {note}")
        skipped = sum(1 for p in plans if p.skip_reason)
        self.stdout.write(f"{len(plans)} rows: {len(plans) - skipped} to import, {skipped} skipped.")
