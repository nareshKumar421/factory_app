# Issue tracker — API reference

All paths are under `/api/v1/issues/`. JWT auth, as everywhere else.

Issues are addressed by **number** (`#41` → `41`), not by primary key.
Comments and attachments are the only things keyed by id.

Every response that the UI makes decisions from carries a `permissions` block,
so the client hides what it cannot do instead of clicking into a 403:

```json
{
  "can_view": true,
  "can_create": true,
  "can_triage": false,
  "can_manage_settings": false,
  "can_edit": true          // detail responses only: may I edit THIS issue?
}
```

---

## `GET meta/`

Everything the screens need to render their pickers, in one call — the list
page, the new-issue form and the sidebar all want the same lists.

```json
{
  "labels":   [{ "id": 1, "name": "bug", "color": "#d73a4a", "description": "…",
                 "sequence": 10, "open_issues": 4 }],
  "areas":    [{ "id": 1, "name": "Dispatch", "code": "dispatch",
                 "description": "…", "sequence": 20, "owners": [ … ] }],
  "users":    [{ "id": 7, "name": "Priya Kaur", "email": "…",
                 "employee_code": "…", "initials": "PK" }],
  "companies":[{ "id": 2, "code": "JIVO_OIL", "name": "Jivo Oil" }],
  "priorities":    [{ "value": "URGENT", "label": "Urgent" }, … ],
  "close_reasons": [{ "value": "COMPLETED", "label": "Completed" }, … ],
  "sorts": ["comments", "created", "oldest", "priority", "updated", … ],
  "permissions": { … },
  "me": { "id": 7, "name": "Priya Kaur", … }
}
```

Needs any `issues.*` right.

---

## `GET issues/` — the list

| Query param | Meaning |
| --- | --- |
| `q` | The search box. Free text plus `key:value` qualifiers — see the [search grammar](README.md#the-search-box). |
| `state` | `OPEN` (default), `CLOSED`, or `ALL`. **Overrides** any `is:` in `q` — the tab is a control the user clicked, the qualifier is text they may have left behind. |
| `sort` | `updated` (default), `created`, `oldest`, `priority`, `comments`, or any alias in `meta.sorts`. |
| `page`, `page_size` | 25 per page, max 100. |

Response — the repo's usual pagination envelope, plus three extras:

```json
{
  "results": [ IssueListItem, … ],
  "count": 137, "page": 1, "page_size": 25, "total_pages": 6,
  "next": 2, "previous": null,

  "state_counts": { "open": 12, "closed": 125 },
  "permissions": { … },
  "unknown_qualifiers": ["labels:bug"]
}
```

`state_counts` is computed with the query's **own `is:` filter removed**, the way
GitHub does it — otherwise the open count would already have `is:open` applied
and the Closed tab would always read zero.

`unknown_qualifiers` lists anything the parser did not recognise. A mistyped
`labels:bug` is reported rather than searched for literally, which would return
nothing while looking like a working filter.

`IssueListItem` carries what a row needs: `id`, `number`, `title`, `state`,
`state_reason`, `state_reason_display`, `priority`, `priority_display`, `author`,
`assignees`, `labels`, `area` / `area_name` / `area_code`, `company` /
`company_code`, `pinned`, `locked`, `comment_count`, `created_at`,
`last_activity_at`, `closed_at`. Pinned issues sort to the top of every ordering.

---

## `POST issues/` — file a new issue

Needs `can_create_issues` (or `can_triage_issues`).

```json
{
  "title": "Gate pass will not print",
  "body": "### What happened\nNothing happens when I hit print.",
  "priority": "HIGH",
  "area": 1,
  "company": 2,
  "label_ids": [1, 4],
  "assignee_ids": [7],
  "page_url": "/dispatch/gate-passes",
  "attachment_ids": [12, 13]
}
```

Only `title` is required. `attachment_ids` claims files previously sent to
`uploads/`. `label_ids` / `assignee_ids` are accepted from anyone here but the
client only offers them to a triager.

`201` with the full `IssueDetail`. The number is allocated by the server; a
number supplied by the caller is ignored.

---

## `GET issues/<number>/`

`IssueDetail` — everything a row has, plus `body`, `page_url`, `closed_by`,
`attachments` (the issue's own; a comment's files ride with the comment),
`duplicate_of` / `duplicate_of_number` / `duplicate_of_title`, `updated_at`, and
the `permissions` block including `can_edit`.

## `PATCH issues/<number>/`

Partial. Only the keys sent are changed, and a key whose value equals what is
stored writes nothing — saving an unchanged form should not add noise to the
timeline.

Accepts `title`, `body`, `priority`, `area`, `company`, `page_url`, `label_ids`,
`assignee_ids`, `pinned`, `locked`.

- The **author** may send `title`, `body`, `priority`, `area`, `company`,
  `page_url`.
- `label_ids`, `assignee_ids`, `pinned` and `locked` need `can_triage_issues`;
  sending one without it is a `403`, not a silent drop.

Returns the updated `IssueDetail`.

## `DELETE issues/<number>/`

Needs `can_triage_issues`. `204`. The number is not reused.

---

## `POST issues/<number>/state/` — close or reopen

The author may close their own; a triager may close any.

```json
{ "state": "CLOSED", "reason": "NOT_PLANNED" }
{ "state": "CLOSED", "reason": "DUPLICATE", "duplicate_of": 12 }
{ "state": "OPEN" }
```

`reason` is `COMPLETED` (default), `NOT_PLANNED` or `DUPLICATE`; closing as a
duplicate without `duplicate_of` is a `400`, and an issue cannot duplicate
itself. Reopening clears the reason — it no longer holds. Returns the updated
`IssueDetail`.

## `POST bulk-state/`

Needs `can_triage_issues`. For the list's checkboxes.

```json
{ "numbers": [41, 42, 43], "state": "CLOSED", "reason": "COMPLETED" }
→ { "changed": 3 }
```

Issues that cannot be changed are skipped rather than failing the batch.

---

## `GET issues/<number>/timeline/`

The comments and events, merged into one chronological list, so the client does
not have to interleave two sorted lists itself.

```json
[
  { "kind": "event",   "at": "…", "event":   { … }, "comment": null },
  { "kind": "comment", "at": "…", "comment": { … }, "event": null }
]
```

An `event` is `{ id, event, event_display, actor, detail, created_at }`. The
`detail` object holds whatever that event is about, **frozen when it happened**:
`{label, color}` for `LABELED`, `{user_id, name}` for `ASSIGNED`,
`{previous, current}` for `RENAMED`, `{reason}` for `CLOSED`, `{number, title}`
for `MARKED_DUPLICATE`.

Event kinds: `OPENED`, `CLOSED`, `REOPENED`, `LABELED`, `UNLABELED`, `ASSIGNED`,
`UNASSIGNED`, `RENAMED`, `EDITED`, `PRIORITY_CHANGED`, `AREA_CHANGED`,
`MARKED_DUPLICATE`, `PINNED`, `UNPINNED`, `LOCKED`, `UNLOCKED`.

---

## Comments

- `POST issues/<number>/comments/` — `{ "body": "…", "attachment_ids": [14] }`
  → `201` with the comment. A **locked** conversation returns `400`.
- `PATCH comments/<id>/` — `{ "body": "…" }`. Stamps `edited_at` rather than
  rewriting history silently. Author or triager only.
- `DELETE comments/<id>/` — `204`. Author or triager only. The issue's
  `comment_count` is corrected.

---

## `POST uploads/`

`multipart/form-data`, field `files` (repeatable) or `file`.

```json
{
  "results": [{ "id": 12, "original_filename": "screen.png", "content_type": "image/png",
                "size_bytes": 84213, "uploaded_at": "…",
                "url": "https://…/media/issue_attachments/2026/09/screen.png",
                "is_image": true }],
  "errors":  [{ "filename": "payload.exe", "detail": "'.exe' is not accepted. Allowed: …" }]
}
```

A batch where **some** files are rejected still returns `201` with the rest and
the per-file errors. A batch where **all** are rejected returns `400`.

Rows come back unclaimed; send their ids as `attachment_ids` on the issue or
comment. Only the uploader can claim their own rows. 10 MB per file, extensions
per `issues.constants.ALLOWED_ATTACHMENT_EXTENSIONS`.

---

## Masters

Reading needs any `issues.*` right (every form needs the lists to render);
writing needs `can_manage_issue_settings`.

| Endpoint | Notes |
| --- | --- |
| `GET/POST labels/` | Label rows carry `open_issues`, the count that says whether a label is doing any work. |
| `PATCH/DELETE labels/<id>/` | **DELETE deactivates**: `is_active=False` and the label comes off its issues. The row survives because its name and colour are frozen into every `LABELED` event. |
| `GET/POST areas/` | `owner_ids` on write sets the suggested assignees; `owners` on read expands them. |
| `PATCH/DELETE areas/<id>/` | DELETE deactivates. Issues already filed against the area keep it. |
