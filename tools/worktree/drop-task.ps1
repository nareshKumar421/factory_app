<#
.SYNOPSIS
  Tear down a task's worktrees once its work is landed on main.

.DESCRIPTION
  ORDERING MATTERS. The worktrees contain junctions pointing at the hub's .venv
  (351M) and node_modules (589M). Those junctions must be unlinked BEFORE the
  directory is removed, using Directory.Delete(path, recursive:$false), which
  deletes the reparse point only.

  Do not use 'Remove-Item -Recurse' on a junction: under Windows PowerShell 5.1
  it can descend into the target and delete the hub's real dependencies.

.EXAMPLE
  pwsh tools/worktree/drop-task.ps1 cash-book-bunching
  pwsh tools/worktree/drop-task.ps1 cash-book-bunching -KeepBranch
#>
param(
  [Parameter(Mandatory = $true)][string]$Slug,
  [string]$DevRoot = 'C:\Users\gurpa\dev',
  [switch]$KeepBranch,
  [switch]$Force
)

$ErrorActionPreference = 'Stop'

$branch   = "task/$Slug"
$taskRoot = Join-Path $DevRoot "wt\$Slug"
$repos    = @(
  @{ Name = 'factory_app'; Hub = Join-Path $DevRoot 'factory_app';   Shared = @('.venv') },
  @{ Name = 'FactoryFlow'; Hub = Join-Path $DevRoot 'FactoryFlow';   Shared = @('node_modules') }
)

if (-not (Test-Path $taskRoot)) { Write-Host "No worktree at $taskRoot."; exit 0 }

foreach ($repo in $repos) {
  $dest = Join-Path $taskRoot $repo.Name
  if (-not (Test-Path $dest)) { continue }

  Write-Host "`n=== $($repo.Name) ===" -ForegroundColor Cyan

  # Refuse to discard work that never reached main.
  if (-not $Force) {
    $unlanded = git -C $dest log --oneline "origin/main..HEAD" 2>$null
    if ($unlanded) {
      Write-Host $unlanded -ForegroundColor Yellow
      throw "$($repo.Name): commits above are not on origin/main. Land them first, or pass -Force."
    }
    $dirty = git -C $dest status --porcelain 2>$null
    if ($dirty) {
      Write-Host $dirty -ForegroundColor Yellow
      throw "$($repo.Name): uncommitted changes. Commit or discard them, or pass -Force."
    }
  }

  # Unlink the shared dirs FIRST - see the ordering note above.
  foreach ($dir in $repo.Shared) {
    $link = Join-Path $dest $dir
    if (Test-Path $link) {
      $item = Get-Item $link -Force
      if ($item.LinkType -eq 'Junction') {
        [System.IO.Directory]::Delete($link, $false)
        Write-Host "  unlinked  $dir (hub copy untouched)"
      } else {
        Write-Warning "  $dir is a real directory, not a junction - leaving it alone."
      }
    }
  }

  git -C $repo.Hub worktree remove --force $dest
  if ($LASTEXITCODE -ne 0) { throw "$($repo.Name): worktree remove failed." }

  if (-not $KeepBranch) {
    git -C $repo.Hub branch -D $branch 2>&1 | Out-Null
  }
  Write-Host "  removed   worktree$(if (-not $KeepBranch) { ' and branch' })"
}

# git leaves the junction shells behind, so the task dir needs a final sweep.
Get-ChildItem $taskRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
  if (-not (Get-ChildItem $_.FullName -Force)) { Remove-Item $_.FullName -Force }
}
if (Test-Path $taskRoot -PathType Container) {
  if (-not (Get-ChildItem $taskRoot -Force)) { Remove-Item $taskRoot -Force }
  else { Write-Warning "$taskRoot still has files - left in place." }
}

Write-Host "`nDropped $Slug." -ForegroundColor Green
