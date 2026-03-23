param(
  [switch]$Apply
)

$ErrorActionPreference = "Stop"

function Remove-Matches {
  param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string[]]$Patterns
  )

  $items = @()
  foreach ($pat in $Patterns) {
    $items += Get-ChildItem -LiteralPath $Root -Force -Recurse -File -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -like $pat }
  }
  $items = $items | Sort-Object FullName -Unique

  if (-not $items -or $items.Count -eq 0) {
    return 0
  }

  $totalBytes = ($items | Measure-Object -Property Length -Sum).Sum
  Write-Host ("Trovati {0} file (~{1} KB)" -f $items.Count, [Math]::Round(($totalBytes/1KB), 1))
  foreach ($it in $items) {
    Write-Host (" - {0}" -f $it.FullName)
  }

  if (-not $Apply) {
    Write-Host "Dry-run: nessun file eliminato. Riesegui con -Apply per cancellare."
    return $items.Count
  }

  foreach ($it in $items) {
    try { Remove-Item -LiteralPath $it.FullName -Force -ErrorAction Stop } catch {}
  }
  return $items.Count
}

function Remove-Dirs {
  param(
    [Parameter(Mandatory=$true)][string[]]$Dirs
  )
  $n = 0
  foreach ($d in $Dirs) {
    try {
      if (Test-Path -LiteralPath $d) {
        if (-not $Apply) {
          Write-Host (" - {0} (dir)" -f $d)
        } else {
          Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction Stop
        }
        $n++
      }
    } catch {}
  }
  return $n
}

$root = Split-Path -Parent $PSScriptRoot
Write-Host ("Repo: {0}" -f $root)

Write-Host "`nFile backup / temporanei:"
$removed = 0
$removed += Remove-Matches -Root $root -Patterns @("*.bak.*", "*.tmp", "*.tmp_*", "*.old")

Write-Host "`nFile generati (dev):"
$removed += Remove-Matches -Root $root -Patterns @("tmp_index_debug.html", "debug_server.log", "debug_server.err")

Write-Host "`nCache Python:"
$removed += Remove-Dirs -Dirs @(
  (Join-Path $root "scripts\\__pycache__"),
  (Join-Path $root "app\\__pycache__")
)

Write-Host ""
if ($Apply) {
  Write-Host ("Pulizia completata. Elementi rimossi (stimati): {0}" -f $removed)
} else {
  Write-Host ("Dry-run completato. Elementi trovati: {0}" -f $removed)
}

