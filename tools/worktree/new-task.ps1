<#
.SYNOPSIS
  Create an isolated worktree pair (backend + frontend) for one task/tab.

.DESCRIPTION
  Each Claude tab gets its own directory and its own branch, so tabs can never
  overwrite each other's in-flight edits or race on a shared working tree.

  The two heavy, gitignored dependency dirs (.venv ~351M, node_modules ~589M)
  are NOT copied. They are junctioned back to the hub checkouts, so a worktree
  costs a few MB instead of a gigabyte.

.EXAMPLE
  pwsh tools/worktree/new-task.ps1 cash-book-bunching
#>
param(
  [Parameter(Mandatory = $true)][string]$Slug,
  [string]$DevRoot = 'C:\Users\gurpa\dev'
)

$ErrorActionPreference = 'Stop'

if ($Slug -notmatch '^[a-z0-9][a-z0-9-]*$') {
  throw "Slug must be lowercase letters, digits and dashes (got '$Slug')."
}

$branch = "task/$Slug"
$repos = @(
  @{ Name = 'factory_app'; Hub = Join-Path $DevRoot 'factory_app';
     Shared = @('.venv'); Files = @('.env', '.env.live') },
  @{ Name = 'FactoryFlow'; Hub = Join-Path $DevRoot 'FactoryFlow';
     Shared = @('node_modules'); Files = @('.env') }
)

$taskRoot = Join-Path $DevRoot "wt\$Slug"
New-Item -ItemType Directory -Force $taskRoot | Out-Null

foreach ($repo in $repos) {
  $hub = $repo.Hub
  $dest = Join-Path $taskRoot $repo.Name

  if (-not (Test-Path (Join-Path $hub '.git'))) {
    Write-Warning "$($repo.Name): no repo at $hub - skipping."
    continue
  }
  if (Test-Path $dest) {
    Write-Host "$($repo.Name): worktree already exists at $dest - skipping." -ForegroundColor Yellow
    continue
  }

  Write-Host "`n=== $($repo.Name) ===" -ForegroundColor Cyan

  # Always branch from freshly fetched origin/main, never from a stale local main.
  git -C $hub fetch origin --quiet
  if ($LASTEXITCODE -ne 0) { throw "$($repo.Name): fetch failed." }

  $exists = git -C $hub rev-parse --verify --quiet "refs/heads/$branch"
  if ($exists) {
    git -C $hub worktree add $dest $branch
  } else {
    git -C $hub worktree add -b $branch $dest origin/main
  }
  if ($LASTEXITCODE -ne 0) { throw "$($repo.Name): worktree add failed." }

  # Share the expensive dirs rather than reinstalling them per tab.
  foreach ($dir in $repo.Shared) {
    $src = Join-Path $hub $dir
    if (Test-Path $src) {
      New-Item -ItemType Junction -Path (Join-Path $dest $dir) -Target $src | Out-Null
      Write-Host "  junction  $dir -> hub"
    } else {
      Write-Warning "  $dir missing in hub - install it there first, then re-run."
    }
  }

  # Secrets are gitignored, so a fresh worktree has none. Copy, don't link:
  # a task may need to point at a different database than the hub does.
  foreach ($file in $repo.Files) {
    $src = Join-Path $hub $file
    if (Test-Path $src) {
      Copy-Item $src (Join-Path $dest $file)
      Write-Host "  copied    $file"
    }
  }
}

Write-Host "`nReady. Point the tab at:" -ForegroundColor Green
Write-Host "  $taskRoot\factory_app"
Write-Host "  $taskRoot\FactoryFlow"
Write-Host "Branch: $branch"
