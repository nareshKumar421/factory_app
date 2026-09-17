<#
.SYNOPSIS
  Land a task branch onto main, from inside the task's own worktree.

.DESCRIPTION
  Replaces the old "commit, push, get rejected, cherry-pick onto origin/main in
  a temp worktree, push the sha" workaround. That workaround never moved the
  local branch forward, so every task left a duplicate commit behind and the
  divergence compounded forever (it reached 18-ahead/23-behind, of which 13 were
  twins of commits already upstream).

  Here the branch is rebased onto origin/main and pushed straight to main. If
  the push races another tab, nothing is left behind - just run it again.

.EXAMPLE
  pwsh tools/worktree/land-task.ps1            # lands the current worktree
  pwsh tools/worktree/land-task.ps1 -DryRun    # show what would be pushed
#>
param(
  [string]$RepoPath = '.',
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$branch = (git -C $RepoPath rev-parse --abbrev-ref HEAD).Trim()
if ($branch -eq 'main') { throw "Already on main - run this from a task worktree." }
if ($branch -eq 'HEAD') { throw "Detached HEAD - checkout the task branch first." }

# A dirty tree means unfinished work; rebasing under it loses or stashes edits.
$dirty = git -C $RepoPath status --porcelain --untracked-files=no
if ($dirty) {
  Write-Host $dirty
  throw "Uncommitted changes - commit them before landing."
}

git -C $RepoPath fetch origin --quiet
if ($LASTEXITCODE -ne 0) { throw "fetch failed." }

$counts = (git -C $RepoPath rev-list --left-right --count "origin/main...HEAD").Trim() -split '\s+'
Write-Host "behind origin/main: $($counts[0])   ahead: $($counts[1])" -ForegroundColor Cyan

if ([int]$counts[1] -eq 0) { Write-Host "Nothing to land."; exit 0 }

# Rebase drops any commit already upstream by patch-id, so re-running after a
# partial push is safe and produces no duplicates.
git -C $RepoPath rebase origin/main
if ($LASTEXITCODE -ne 0) {
  Write-Host "`nRebase stopped on a conflict. Resolve, 'git rebase --continue', re-run." -ForegroundColor Yellow
  Write-Host "If a commit is already upstream under another sha, 'git rebase --skip' it." -ForegroundColor Yellow
  exit 1
}

Write-Host "`n--- commits to land ---" -ForegroundColor Cyan
git -C $RepoPath log --oneline origin/main..HEAD

if ($DryRun) { Write-Host "`n(dry run - nothing pushed)" -ForegroundColor Yellow; exit 0 }

# Fast-forward origin/main. Rejection here just means another tab landed first.
git -C $RepoPath push origin "HEAD:main"
if ($LASTEXITCODE -ne 0) {
  Write-Host "`nPush rejected - another tab landed first. Re-run this script." -ForegroundColor Yellow
  Write-Host "Do NOT cherry-pick onto origin/main in a temp worktree." -ForegroundColor Red
  exit 1
}

Write-Host "`nLanded $branch on main." -ForegroundColor Green
