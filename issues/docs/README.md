# Issue tracker

The software's own bug list, kept inside the software. A GitHub-shaped issue
list: an issue has a **number**, a title, a markdown body, an author, a state
(open / closed), any number of **labels** and **assignees**, and a **timeline**
made of comments and events.

- Backend app: `issues/`
- API prefix: `/api/v1/issues/`
- Frontend module: `FactoryFlow/src/modules/issues/`
- Pages: `/issues`, `/issues/new`, `/issues/:number`, `/issues/labels`

## Why it looks like GitHub

The shape is not decoration. Open / Closed tabs with live counts, one text box
that takes both free text and `key:value` qualifiers, and rows dense enough to
skim thirty at a time are what make a backlog usable, and they are already in
everyone's fingers. Copying the shape means nobody has to be taught the screen.

## Where it differs, and why

Three things are not on GitHub's form, because this tracker sits *inside* the
software it tracks:

| Field | Why |
| --- | --- |
| `page_url` — "where it happened" | The header's bug button links to `/issues/new?from=<current path>`, so a report arrives already saying which screen to open. Nobody has to ask "where were you?". |
| `company` | Half the bugs in this app are one company's data looking wrong in another's, so the active company unit is captured with the report rather than reconstructed later. |

## The permission split

Four rights, and the split between the first two is the point of the module:

| Permission | Who it is for |
| --- | --- |
| `can_view_issues` | Read the list. |
| `can_create_issues` | **Everybody who uses the software.** If someone can hit a bug, they should be able to report it. |
| `can_triage_issues` | The handful of people who own the backlog: label, assign, close, reopen, pin, delete, edit anyone's issue. |
| `can_manage_issue_settings` | Maintain the labels, and the support number every user sees. |

On top of those there is one **object-level** rule in `issues/permissions.py`:
an author may always edit and close their **own** issue, and edit or delete
their **own** comment, triage right or not — someone who filed a duplicate by
mistake should be able to withdraw it without waiting for a maintainer. They
still cannot label, assign, pin or lock it; that is triage.

The detail response reports this back as `permissions.can_edit`, so the client
hides the buttons rather than letting anyone click into a 403.

Groups: `python manage.py setup_issue_groups` creates **Issue Reporter**,
**Issue Maintainer** and **Issue Admin**. Reporter is not something an admin is
meant to remember per person -- a new account joins it automatically
(`issues/signals.py`), and the accounts that predate the group are swept in
once with `setup_issue_groups --assign-everyone`.

## The timeline is the record

Every change writes its own event, because the only way to change an issue is
through `issues/services.py`. `set_labels` does not just rewrite the M2M — it
works out what was added and removed and appends a `LABELED` / `UNLABELED`
event for each, with the label's **name and colour frozen into `detail`**.

That freezing is deliberate: renaming the "bug" label to "defect" must not
rewrite what happened last week. It is also why deleting a label **deactivates**
the row instead of removing it — the row has to survive for its history to keep
reading correctly.

Events are append-only. Nothing edits or deletes one, including the Django admin
(the inline is read-only), which is what makes the timeline worth trusting.

## Issue numbers

`Issue.number` is the human handle — the "#41" people quote in chat — and every
URL uses it rather than the primary key.

Numbers come off a locked counter row (`IssueNumberSequence`), **not** from
`MAX(number) + 1`. Two people filing at the same moment get #41 and #42 rather
than colliding, and a deleted #7 is never handed out again: the number gets
quoted in a chat message and referenced in a commit long after the issue is
gone, and a second #7 would make both references wrong.

The counter seeds itself from the existing high-water mark the first time it is
needed, so it could be added to a tracker that already held issues.

## The search box

One text input, speaking GitHub's qualifier language (`issues/search.py`):

```
is:open label:bug assignee:@me sort:updated
is:closed no:assignee "gate pass"
author:priya@example.com priority:urgent scan
-label:duplicate
#41
```

Understood qualifiers: `is:` / `state:`, `label:`, `assignee:`, `author:`,
`priority:`, `company:`, `reason:`, `commenter:`, `involves:`, `number:`,
`no:`, `sort:`. A leading `-` negates a label.
`@me` resolves to the signed-in user. Anything else is free text, matched
against the title and body; a bare `41` or `#41` jumps to that issue.

Two details worth knowing:

- **Repeated `label:` qualifiers AND together** (an issue must carry all of
  them), matching GitHub. Repeated `assignee:` values OR.
- **An unknown qualifier is reported, never swallowed.** A typo like
  `labels:bug` would otherwise be searched for as literal text, quietly return
  nothing, and look like a working filter. The list response carries
  `unknown_qualifiers` and the page says so.

The filter dropdowns and the text box are **one control**: every menu writes a
qualifier into the same string the box shows, so they cannot disagree, and a
filter someone built by clicking can be copied out of the box and pasted to a
colleague. The whole filter lives in the URL, so a filtered list is a link.

## Attachments

A screenshot settles most reports, and the way people take one is PrtSc then
Ctrl-V. So the editor uploads a pasted or dropped image immediately and inserts
its markdown at the caret — no saving a file first.

The upload happens while the reporter is still typing, so the row is stored
**unclaimed** (`issue` and `comment` both null) and its id comes back. Submitting
the issue or comment sends those ids as `attachment_ids`, which claims them. Only
the uploader's own unclaimed rows can be claimed, so nobody can graft someone
else's upload onto their issue by guessing an id.

Uploads are capped at 10 MB and restricted to an allow-list of extensions
(`issues/constants.py`) — an issue attachment is evidence, never something
executable.

## Markdown is rendered as React nodes, never HTML

`FactoryFlow/src/modules/issues/components/Markdown.tsx` has no
`dangerouslySetInnerHTML` anywhere. Issue bodies are typed by users and shown to
other users, so a renderer that injected an HTML string would be a stored-XSS
hole in a page whose entire purpose is pasting in whatever broke. Link and image
URLs are additionally restricted to `http(s):`, `mailto:` and same-origin paths.

Unsupported syntax renders as the literal text the author typed — a report must
never lose characters.

## The support number is a row, not a constant

`SupportContact` holds one row with the support desk's phone number, served
publicly at `GET support-contact/` and changed on the tracker's own settings
screen (`/issues/labels`) by anyone holding `can_manage_issue_settings`. It
lives in this module because the two ways a user asks for help -- phone a
human, or file an issue -- are the same feature from where they are standing,
and a number printed on the login screen cannot need a frontend release to
change.

Deliberately **not** in the Django admin: the people who know the support
number are the people running the tracker, not the handful with a database
login, and two places to edit one number is how the two disagree.

Two details worth keeping: the dialling form (`dial`) is **derived** from what
the admin typed, so fixing a typo cannot leave a stale `tel:` behind; and a
blank number is a legitimate state meaning "no support line right now", at
which point every screen hides its support link instead of publishing a number
nobody answers. The frontend remembers the last number it was served, so an
unreachable backend does not take the number off the login screen -- but a
number blanked on purpose clears that memory too.

## Setting it up

```bash
python manage.py migrate issues         # 0002 the support number, 0004 the labels
python manage.py setup_issue_groups     # Reporter / Maintainer / Admin

python manage.py setup_issue_groups --assign-everyone --dry-run   # who is missing it
python manage.py setup_issue_groups --assign-everyone             # put them all in
```

`migrate` is enough for the masters: **GitHub's nine default labels** are
seeded by migration `0004_seed_github_labels`, so a migrated database has a
working label picker without anyone remembering a command.
`python manage.py seed_issue_masters` writes the same nine and stays for
re-seeding one somebody deleted (`--list` shows what it would do). Both are
idempotent and neither overwrites a row a team has since edited — only
genuinely absent rows are created, which is how a local label like `sap` or
`print`, added from the settings screen, survives every later deploy.

The last step is what makes the tracker work: a tracker is only useful if the
people who hit the bugs are the ones filing them, so `--assign-everyone` drops
every **active** account into **Issue Reporter**. It only adds, so it can be
re-run at will; it skips anyone already in Maintainer or Admin (those groups
carry the right to file on their own) and it skips deactivated accounts. After
this, new accounts need nothing — `issues/signals.py` puts them in the group at
creation. Note the group also carries `can_view_issues`, so the Issues item
appears in everyone's sidebar, which is the intent.

## Tests

```bash
python manage.py test issues
```

58 tests, covering the three things that would quietly break: the search
grammar, the timeline (every change writes its event; a renamed label does not
rewrite history), and the filing-vs-triage permission split.

Frontend:

```bash
npx vitest run src/modules/issues
```

34 tests, including the XSS cases that justify the markdown renderer's design.

See [api.md](api.md) for the endpoint reference.
