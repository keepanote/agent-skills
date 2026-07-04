[CmdletBinding()]
param(
    [int]$IntervalSeconds = 2,
    [int]$Top = 10,
    [int]$Iterations = 0,
    [string[]]$WatchPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ProcessSample {
    $counterPaths = @(
        '\Process(*)\IO Write Bytes/sec',
        '\Process(*)\ID Process'
    )
    $samples = (Get-Counter -Counter $counterPaths).CounterSamples
    $writes = $samples | Where-Object { $_.Path -like '*\IO Write Bytes/sec' }
    $ids = @{}
    foreach ($sample in ($samples | Where-Object { $_.Path -like '*\ID Process' })) {
        $ids[$sample.InstanceName] = [int]$sample.CookedValue
    }
    foreach ($sample in $writes) {
        if ($sample.InstanceName -in @('_total', 'idle')) {
            continue
        }
        [pscustomobject]@{
            Id = if ($ids.ContainsKey($sample.InstanceName)) { $ids[$sample.InstanceName] } else { $null }
            Name = $sample.InstanceName
            WriteBytesPerSec = [double]$sample.CookedValue
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
        [pscustomobject]@{
            MBPerSec = [math]::Round($item.WriteBytesPerSec / 1MB, 3)
            PID = $item.Id
            Process = $item.Name
            WriteBytesPerSec = [math]::Round($item.WriteBytesPerSec, 0)
        }
    }

    Clear-Host
    Write-Host ("[{0}] Top {1} process write rates" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Top)
    $rows |
        Sort-Object WriteBytesPerSec -Descending |
        Select-Object -First $Top |
        Format-Table -AutoSize

    if ($WatchPath) {
        $currentPaths = Get-PathSizes -Paths $WatchPath
        Write-Host ""
        Write-Host "Watched paths"
        $pathRows = foreach ($path in $WatchPath) {
            $beforeSize = $previousPaths[$path]
            $afterSize = $currentPaths[$path]
            $delta = if ($null -ne $beforeSize -and $null -ne $afterSize) { $afterSize - $beforeSize } else { $null }
            [pscustomobject]@{
                Path = $path
                Previous = $beforeSize
                Current = $afterSize
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
