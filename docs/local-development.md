# Running the whole thing locally

The default settings module reads `.env`, and **`.env` points at the production
database**. A bare `python manage.py runserver` is therefore a production
client, which is how a local browser session ends up reading and writing live
rows. Everything below keeps off it.

## The database

`config/local_dev_settings.py` points at database `factory_local` on the
`factory-pgtest` container (`postgres:16`, `127.0.0.1:55432`). Migrations are
enabled and the data persists between commands, which is what separates it from
the other four modules: the two SQLite ones throw the database away, and
`pgtest_settings` drives Django's *test* database. It ignores `.env`'s `DB_*`
so a stale shell cannot redirect it at a live host.

```bash
docker start factory-pgtest
./venv/bin/python manage.py migrate --settings=config.local_dev_settings   # ~4 min
```

## The data

The employee directory comes from the HR workbook in the parent directory:

```bash
./venv/bin/python manage.py import_hierarchy_jwpl \
    --file "../Factory New Heirarchy.xlsx" --company JIVO_OIL --commit \
    --settings=config.local_dev_settings
```

That is the full rebuild, which is the right mode on a local sandbox with
nothing to lose. It yields 246 people, 224 real JWPL/TP codes, 13 `NOCODE-00xx`
(the sheet has no code for them), 68 departments and 28 designations, 11 roots,
3 levels deep. On a database that already holds a directory worth keeping, use
`--update-codes` instead — see the command's docstring, which explains why the
default mode deletes salaries.

## Logins

Creating a user is not enough. Every view states its own permissions, and
`setup_employee_hierarchy_groups` creates the six groups **without putting
anyone in them** — so a fresh login gets a 403 on every employee endpoint, which
the UI renders as *"The directory could not be loaded."* Put the user in a
group:

```python
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from company.models import Company, UserCompany, UserRole

user = get_user_model().objects.create_user(
    email="head@local.test", password="local12345", full_name="Factory Head")
user.groups.add(Group.objects.get(name="Department Head"))
UserCompany.objects.create(
    user=user, company=Company.objects.get(code="JIVO_OIL"),
    role=UserRole.objects.get_or_create(name="Admin")[0], is_active=True)
```

The six groups, narrowest first: `Employee (Self Service)`, `Manager`,
`Department Head`, `HR`, `Finance (Payroll)`, `HR Administrator`.

Requests also need a `Company-Code` header matching an **active** `UserCompany`
row, or `HasCompanyContext` raises before the view is reached. The frontend
sends it from the company switcher.

## The two servers

```bash
./venv/bin/python manage.py runserver 8001 --settings=config.local_dev_settings
```

The frontend reads `VITE_API_BASE_URL`. `FactoryFlow/.env` sets `VITE_API_URL`,
which is deliberately **not** that name, so `npm run dev` falls through to the
`http://localhost:8000/api/v1` default. Point it at the local backend with a
gitignored `FactoryFlow/.env.local`:

```
VITE_API_BASE_URL=http://localhost:8001/api/v1
```

Vite values are baked in at build time, so changing that file needs a restart.
