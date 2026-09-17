# Working in this repo

## One tab, one worktree

Several Claude tabs work this repo at once. A git repo has only **one** working
tree, so two tabs in the same directory overwrite each other's in-flight edits
and cannot be on different branches. Each tab therefore gets its own worktree.

**Do not work directly in `C:\Users\gurpa\dev\factory_app`.** That checkout is
the hub: it stays on `main`, and it owns the two dependency directories every
worktree borrows (`.venv`, and `node_modules` in the FactoryFlow hub).

### Start a task

```powershell
powershell -File C:\Users\gurpa\dev\factory_app\tools\worktree\new-task.ps1 -Slug <task-slug>
```

Creates `task/<task-slug>` off a freshly fetched `origin/main` in **both** repos and
puts the worktrees at `C:\Users\gurpa\dev\wt\<task-slug>\{factory_app,FactoryFlow}`.
Work there for the rest of the task.

`.venv` (351M) and `node_modules` (589M) are junctioned to the hub, so a worktree
costs ~61M, not ~1G. `.env` files are copied, not linked, so a task can point at a
different database without disturbing anything else.

### Land the work

From inside the task's worktree, per repo:

```powershell
powershell -File C:\Users\gurpa\dev\factory_app\tools\worktree\land-task.ps1
```

Fetches, rebases onto `origin/main`, pushes to `main`. If the push is rejected
another tab landed first — **just run it again**. Rebase drops anything already
upstream by patch-id, so re-running never duplicates a commit.

### Finish

```powershell
powershell -File C:\Users\gurpa\dev\factory_app\tools\worktree\drop-task.ps1 -Slug <task-slug>
```

Refuses to run while commits are unlanded or the tree is dirty.

## Never cherry-pick onto origin/main in a temp worktree

When a push was rejected, the old habit was to cherry-pick the commit onto
`origin/main` in a scratch worktree and push that sha. It works once, but the
local branch never moves forward, so the original commit stays behind forever as
a twin of the pushed one. This compounded to 18-ahead/23-behind on this repo, of
which **13 outgoing commits were already upstream under different shas**.

If a push is rejected, rebase and push again. Nothing else.

## Committing

- Commit with an **explicit pathspec** (`git commit -- path/one path/two`), never
  `git add -A`. Even with worktrees, a stray `-A` in the hub sweeps up whatever
  another tab left lying there.
- Stay on the task branch. Don't switch branches in a worktree another tab is using.

## After a rebase, check the migrations

Two branches can both add `0004_*` to the same app. Once one lands, the other
rebases and ends up with two `0004`s. After landing, run:

```powershell
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
```

and renumber the loser if it complains.

## Changing dependencies

`.venv` and `node_modules` are **shared with every other worktree**. Installing a
package inside a worktree changes them for all tabs. If a task needs different
dependencies, unlink the junction and make a real one for that worktree:

```powershell
[System.IO.Directory]::Delete("$PWD\.venv", $false)   # removes the link only
```

Never `Remove-Item -Recurse` a junction — under Windows PowerShell 5.1 it can
descend into the target and delete the hub's real dependencies.

## Tests

Never run a bare `manage.py test` or pass `--parallel`: it picks up root-level
scripts and has corrupted the tree before. Always name the app labels explicitly.
