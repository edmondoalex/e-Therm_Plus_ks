param(
  [Parameter(Mandatory=$true)][string]$version,
  [string]$author = 'Automator',
  [string]$notes = '',
  [string]$files = ''
)

# Wrapper for bump_release.py
$py = 'python'
$script = Join-Path -Path (Split-Path -Parent $MyInvocation.MyCommand.Definition) -ChildPath 'bump_release.py'
& $py $script --version $version --author $author --notes $notes --files $files
