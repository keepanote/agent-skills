[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$canonicalRoot = Join-Path $RepoRoot 'skills'
$targets = @(
    @{ Root = Join-Path $HOME '.codex\skills';  Label = 'Codex user skills' },
    @{ Root = Join-Path $HOME '.agents\skills'; Label = 'Codex repo-style user skills' },
    @{ Root = Join-Path $HOME '.claude\skills'; Label = 'Claude Code user skills' }
)

New-Item -ItemType Directory -Force -Path $canonicalRoot | Out-Null

$skills = Get-ChildItem -LiteralPath $canonicalRoot -Directory
foreach ($target in $targets) {
    New-Item -ItemType Directory -Force -Path $target.Root | Out-Null
    foreach ($skill in $skills) {
        $linkPath = Join-Path $target.Root $skill.Name
        if (Test-Path -LiteralPath $linkPath) {
            $item = Get-Item -LiteralPath $linkPath -Force
            $isReparse = $item.Attributes.ToString().Contains('ReparsePoint')
            if ($isReparse) {
                Remove-Item -LiteralPath $linkPath -Force
            } else {
                Write-Host "Skip existing non-link path: $linkPath"
                continue
            }
        }
        New-Item -ItemType Junction -Path $linkPath -Target $skill.FullName | Out-Null
        Write-Host "Linked [$($target.Label)] $linkPath -> $($skill.FullName)"
    }
}
