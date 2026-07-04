[CmdletBinding()]
param(
    [int]$IntervalSeconds = 2,
    [int]$Top = 10,
    [int]$Iterations = 0,
    [string[]]$WatchPath,
    [double]$MinMBPerSec = 0.05
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ProcessSample {
    $rows = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process
    foreach ($row in $rows) {
        if ($row.Name -in @('_Total', 'Idle')) {
            continue
        }
        [pscustomobject]@{
            Id = [int]$row.IDProcess
            Name = [string]$row.Name
            WriteBytesPerSec = [double]$row.IOWriteBytesPersec
        }
    }
}

function Get-PathSizes {
    param([string[]]$Paths)
    $items = @{}
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path
            $items[$path] = $item.Length
        } else {
            $items[$path] = $null
        }
    }
    return $items
}

$previousPaths = if ($WatchPath) { Get-PathSizes -Paths $WatchPath } else { @{} }
$iteration = 0

while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    $iteration++

    $rows = foreach ($item in Get-ProcessSample) {
        $mbPerSec = [math]::Round($item.WriteBytesPerSec / 1MB, 3)
        $level = if ($mbPerSec -ge 5) {
            'HEAVY'
        } elseif ($mbPerSec -ge 1) {
            'HIGH'
        } elseif ($mbPerSec -ge 0.2) {
            'MEDIUM'
        } elseif ($mbPerSec -ge $MinMBPerSec) {
            'LOW'
        } else {
            'IGNORE'
        }
        [pscustomobject]@{
            Level = $level
            MBPerSec = $mbPerSec
            PID = $item.Id
            Process = $item.Name
            WriteBytesPerSec = [math]::Round($item.WriteBytesPerSec, 0)
        }
    }
    $visibleRows = $rows |
        Where-Object { $_.MBPerSec -ge $MinMBPerSec } |
        Sort-Object WriteBytesPerSec -Descending |
        Select-Object -First $Top

    Clear-Host
    Write-Host ("[{0}] Disk writers above {1} MB/s" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $MinMBPerSec)
    if ($visibleRows) {
        $visibleRows |
            Select-Object Level, MBPerSec, PID, Process |
            Format-Table -AutoSize
    } else {
        Write-Host "No process is writing enough to care about right now."
    }

    if ($WatchPath) {
        $currentPaths = Get-PathSizes -Paths $WatchPath
        Write-Host ""
        Write-Host "Watched paths"
        $pathRows = foreach ($path in $WatchPath) {
            $beforeSize = $previousPaths[$path]
            $afterSize = $currentPaths[$path]
            $delta = if ($null -ne $beforeSize -and $null -ne $afterSize) { $afterSize - $beforeSize } else { $null }
            $status = if ($delta -gt 0) { 'GROWING' } elseif ($delta -eq 0) { 'STABLE' } else { 'N/A' }
            [pscustomobject]@{
                Status = $status
                Path = $path
                Delta = $delta
            }
        }
        $pathRows | Format-Table -AutoSize
        $previousPaths = $currentPaths
    }

    if ($Iterations -gt 0 -and $iteration -ge $Iterations) {
        break
    }
}
