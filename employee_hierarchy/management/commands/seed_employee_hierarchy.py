"""
Seed a company with the organisation from the module's brief.

Usage::

    python manage.py seed_employee_hierarchy --company JIVO_OIL
    python manage.py seed_employee_hierarchy --company JIVO_OIL --force

Creates the department tree, the designation ladder, and the eleven-person org
the brief draws -- CEO over a CTO and an HR Head, engineering and QA under the
CTO, recruitment and payroll under HR -- with salary histories on a few of them
so the history panel and the distribution chart have something real to show.

This is a **development and demo** seed, not a fixture the app depends on. It
refuses to touch a company that already has employees unless ``--force`` is
given, because the one thing worse than an empty directory is a real one with
invented people in it.
"""

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from company.models import Company
from employee_hierarchy import services
from employee_hierarchy.constants import EmploymentStatus, RevisionType
from employee_hierarchy.models import Department, Designation, Employee

#: (code, name, parent code, description)
DEPARTMENTS = [
    ("CORP", "Company", None, "The organisation as a whole."),
    ("TECH", "Technology", "CORP", "Engineering, quality and infrastructure."),
    ("ENG", "Engineering", "TECH", "Product and platform development."),
    ("QA", "QA", "TECH", "Quality assurance and release testing."),
    ("DEVOPS", "DevOps", "TECH", "Build, deploy and run."),
    ("HR", "Human Resources", "CORP", "People, hiring and payroll."),
    ("REC", "Recruitment", "HR", "Sourcing and hiring."),
    ("PAY", "Payroll", "HR", "Compensation and statutory payroll."),
    ("FIN", "Finance", "CORP", "Accounts, treasury and reporting."),
]

#: (code, name, level, is_managerial)
DESIGNATIONS = [
    ("CEO", "CEO", 1, True),
    ("CTO", "CTO", 2, True),
    ("DIR", "Director", 3, True),
    ("GM", "General Manager", 4, True),
    ("MGR", "Manager", 5, True),
    ("TL", "Team Lead", 6, True),
    ("SR_DEV", "Senior Developer", 7, False),
    ("DEV", "Developer", 8, False),
    ("JR_DEV", "Junior Developer", 9, False),
    ("INTERN", "Intern", 10, False),
]

#: (code, first, last, designation, department, job title, manager code, joined,
#:  status, [(effective date, basic, allowances, bonus, type), ...])
PEOPLE = [
    (
        "EMP001", "Arun", "Mehta", "CEO", "CORP", "Chief Executive Officer", None,
        date(2016, 4, 1), EmploymentStatus.ACTIVE,
        [
            (date(2024, 1, 1), 3600000, 1000000, 400000, RevisionType.ANNUAL_INCREMENT),
            (date(2025, 4, 1), 4200000, 1200000, 600000, RevisionType.PERFORMANCE),
            (date(2026, 4, 1), 5000000, 1400000, 800000, RevisionType.ANNUAL_INCREMENT),
        ],
    ),
    (
        "EMP002", "Priya", "Nair", "CTO", "TECH", "Chief Technology Officer", "EMP001",
        date(2017, 7, 10), EmploymentStatus.ACTIVE,
        [
            (date(2024, 4, 1), 2600000, 700000, 300000, RevisionType.ANNUAL_INCREMENT),
            (date(2026, 4, 1), 3100000, 850000, 450000, RevisionType.MARKET_ADJUSTMENT),
        ],
    ),
    (
        "EMP003", "Sandeep", "Rao", "MGR", "ENG", "Engineering Manager", "EMP002",
        date(2019, 2, 18), EmploymentStatus.ACTIVE,
        [
            (date(2024, 4, 1), 1500000, 400000, 150000, RevisionType.ANNUAL_INCREMENT),
            (date(2026, 4, 1), 1750000, 480000, 220000, RevisionType.PROMOTION),
        ],
    ),
    (
        "EMP004", "Kavya", "Iyer", "SR_DEV", "ENG", "Senior Developer", "EMP003",
        date(2020, 6, 1), EmploymentStatus.ACTIVE,
        [
            (date(2024, 4, 1), 900000, 240000, 90000, RevisionType.ANNUAL_INCREMENT),
            (date(2026, 4, 1), 1080000, 280000, 140000, RevisionType.PERFORMANCE),
        ],
    ),
    (
        "EMP005", "Rohit", "Sharma", "DEV", "ENG", "Developer", "EMP004",
        date(2022, 9, 5), EmploymentStatus.ACTIVE,
        [(date(2025, 4, 1), 620000, 160000, 40000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP006", "Neha", "Gupta", "DEV", "ENG", "Developer", "EMP004",
        date(2023, 1, 16), EmploymentStatus.ACTIVE,
        [(date(2025, 4, 1), 580000, 150000, 35000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP007", "Imran", "Sheikh", "JR_DEV", "ENG", "Developer", "EMP003",
        date(2025, 8, 1), EmploymentStatus.PROBATION,
        [(date(2025, 8, 1), 420000, 90000, 0, RevisionType.INITIAL)],
    ),
    (
        "EMP008", "Meera", "Krishnan", "MGR", "QA", "QA Manager", "EMP002",
        date(2019, 11, 4), EmploymentStatus.ACTIVE,
        [(date(2026, 4, 1), 1250000, 340000, 120000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP009", "Vikram", "Bose", "DEV", "QA", "QA Engineer", "EMP008",
        date(2022, 3, 21), EmploymentStatus.ACTIVE,
        [(date(2025, 4, 1), 560000, 140000, 30000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP010", "Anjali", "Verma", "DEV", "QA", "QA Engineer", "EMP008",
        date(2024, 5, 13), EmploymentStatus.ON_LEAVE,
        [(date(2025, 4, 1), 520000, 130000, 25000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP011", "Rajesh", "Kulkarni", "DIR", "HR", "HR Head", "EMP001",
        date(2018, 5, 2), EmploymentStatus.ACTIVE,
        [(date(2026, 4, 1), 2100000, 560000, 260000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP012", "Sneha", "Pillai", "MGR", "HR", "HR Manager", "EMP011",
        date(2021, 1, 11), EmploymentStatus.ACTIVE,
        [(date(2026, 4, 1), 1100000, 300000, 100000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP013", "Farhan", "Qureshi", "TL", "HR", "HR Executive", "EMP012",
        date(2023, 4, 3), EmploymentStatus.ACTIVE,
        [(date(2025, 4, 1), 640000, 170000, 40000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP014", "Divya", "Menon", "TL", "REC", "Recruiter", "EMP012",
        date(2023, 10, 9), EmploymentStatus.ACTIVE,
        [(date(2025, 4, 1), 600000, 160000, 45000, RevisionType.ANNUAL_INCREMENT)],
    ),
    (
        "EMP015", "Suresh", "Bhatt", "MGR", "PAY", "Payroll Manager", "EMP011",
        date(2020, 2, 24), EmploymentStatus.ACTIVE,
        [(date(2026, 4, 1), 980000, 260000, 90000, RevisionType.ANNUAL_INCREMENT)],
    ),
]


class Command(BaseCommand):
    help = "Seed one company's departments, designations and a demo organisation."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Company code, e.g. JIVO_OIL.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Seed even though the company already has employees.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        company = Company.objects.filter(code=options["company"]).first()
        if company is None:
            raise SystemExit(f"No company with code {options['company']}.")

        existing = Employee.objects.filter(company=company).count()
        if existing and not options["force"]:
            raise SystemExit(
                f"{company.code} already has {existing} employee(s). "
                "Re-run with --force if you really mean to add the demo org to it."
            )

        departments = {}
        for code, name, parent_code, description in DEPARTMENTS:
            department, _ = Department.objects.update_or_create(
                company=company,
                code=code,
                defaults={
                    "name": name,
                    "description": description,
                    "parent": departments.get(parent_code),
                    "sort_order": len(departments),
                },
            )
            departments[code] = department

        designations = {}
        for code, name, level, is_managerial in DESIGNATIONS:
            designation, _ = Designation.objects.update_or_create(
                company=company,
                code=code,
                defaults={"name": name, "level": level, "is_managerial": is_managerial},
            )
            designations[code] = designation

        people = {}
        for (
            code,
            first,
            last,
            designation_code,
            department_code,
            job_title,
            manager_code,
            joined,
            employment_status,
            salaries,
        ) in PEOPLE:
            if Employee.objects.filter(company=company, employee_code=code).exists():
                self.stdout.write(f"  {code} already exists — skipped.")
                people[code] = Employee.objects.get(company=company, employee_code=code)
                continue

            employee = services.create_employee(
                company=company,
                data={
                    "employee_code": code,
                    "first_name": first,
                    "last_name": last,
                    "email": f"{first.lower()}.{last.lower()}@example.com",
                    "phone": "",
                    "joining_date": joined,
                    "employment_status": employment_status,
                    "department": departments[department_code],
                    "designation": designations[designation_code],
                    "job_title": job_title,
                    "location": "Head Office",
                    "reporting_manager": people.get(manager_code),
                },
            )
            people[code] = employee

            for effective_from, basic, allowances, bonuses, revision_type in salaries:
                services.create_salary_record(
                    employee,
                    effective_from=effective_from,
                    basic_salary=Decimal(basic),
                    allowances=Decimal(allowances),
                    bonuses=Decimal(bonuses),
                    deductions=Decimal(0),
                    currency="INR",
                    revision_type=revision_type,
                    reason={
                        RevisionType.INITIAL: "Joining salary",
                        RevisionType.PROMOTION: "Promotion",
                        RevisionType.PERFORMANCE: "Performance review",
                        RevisionType.MARKET_ADJUSTMENT: "Market correction",
                    }.get(revision_type, "Annual increment"),
                    approve=True,
                )

        # The department heads, now that the people they head exist.
        for department_code, head_code in (
            ("CORP", "EMP001"),
            ("TECH", "EMP002"),
            ("ENG", "EMP003"),
            ("QA", "EMP008"),
            ("HR", "EMP011"),
            ("REC", "EMP012"),
            ("PAY", "EMP015"),
        ):
            department = departments[department_code]
            department.head = people.get(head_code)
            department.save(update_fields=["head", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {company.code}: {len(departments)} departments, "
                f"{len(designations)} designations, {len(people)} employees."
            )
        )
