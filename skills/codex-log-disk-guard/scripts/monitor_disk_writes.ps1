<#
.SYNOPSIS
    Monitor Windows per-process disk read/write rates with sliding-window history.

.DESCRIPTION
    Samples Win32_PerfFormattedData_PerfProc_Process every N seconds and shows
    per-process read and write rates (current, 1m, 1h, 24h windows) along with
    session-accumulated totals.  Supports JSON output, ANSI colour, and
    graceful Ctrl+C shutdown.

.PARAMETER IntervalSeconds
    Seconds between samples (default: 2).

.PARAMETER Top
    Number of top processes to show in each table (default: 10).

.PARAMETER Iterations
    Number of iterations; 0 = run until interrupted (default: 0).

.PARAMETER WatchPath
    File or directory paths whose sizes to track (repeatable).

.PARAMETER MinMBPerSec
    Minimum MB/s threshold for a process to appear in the table (default: 0.05).

.PARAMETER Json
    Emit JSON lines instead of human-readable tables.

.PARAMETER NoColor
    Disable ANSI colour output.
#>

[CmdletBinding()]
param(
    [int]$IntervalSeconds = 2,
    [int]$Top = 10,
    [int]$Iterations = 0,
    [string[]]$WatchPath,
    [double]$MinMBPerSec = 0.05,
    [switch]$Json,
    [switch]$NoColor
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------
$script:UseColor = (-not $NoColor) -and (-not [string]::IsNullOrEmpty($env:WT_SESSION)) -or
                   ($host.UI.RawUI.WindowTitle -ne $null)

function Write-Colored {
    param([string]$Text, [string]$Color)
    if ($script:UseColor -and $Color) {
        $codes = @{
            Red    = "`e[91m"
            Yellow = "`e[93m"
            Green  = "`e[92m"
            Bold   = "`e[1m"
            Dim    = "`e[2m"
            Reset  = "`e[0m"
        }
        Write-Host "$($codes[$Color])$Text$($codes['Reset'])"
    } else {
        Write-Host $Text
    }
}

# ---------------------------------------------------------------------------
# Signal handling (Ctrl+C)
# ---------------------------------------------------------------------------
$script:shutdown = $false
$null = [Console]::TreatControlCAsInput($false)
try {
    $null = [Console]::CancelKeyPress
} catch { }

# We can't easily trap Ctrl+C in a non-advanced PS session, so we rely on
# the while-loop checking $Iterations and clean up in the `finally` block.

function Get-TerminalWidth {
    try { return $host.UI.RawUI.WindowSize.Width }
    catch { return 120 }
}

# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
function Get-ProcessSample {
    <#
    .SYNOPSIS
        Query Win32_PerfFormattedData_PerfProc_Process and return sanitised rows.
    #>
    try {
        $perf = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process -ErrorAction SilentlyContinue
    } catch {
        return @()
    }
    if (-not $perf) { return @() }

    foreach ($row in $perf) {
        if ($row.Name -in @('_Total', 'Idle') -or -not $row.IDProcess) { continue }
        # Strip WMI instance suffixes (#1, #2, ...) from process names
        $cleanName = $row.Name -replace '#\d+$', ''
        [pscustomobject]@{
            Id                = [int]$row.IDProcess
            Name              = [string]$cleanName
            ReadBytesPerSec   = [double]$row.IOReadBytesPersec
            WriteBytesPerSec  = [double]$row.IOWriteBytesPersec
        }
    }
}

function Get-PathSizes {
    param([string[]]$Paths)
    $items = @{}
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path -ErrorAction SilentlyContinue) {
            try   { $items[$path] = (Get-Item -LiteralPath $path -ErrorAction Stop).Length }
            catch { $items[$path] = $null }
        } else {
            $items[$path] = $null
        }
    }
    return $items
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Add-HistoryPoint {
    param(
        [string]$Key,
        [datetime]$Now,
        [double]$ReadBytes,
        [double]$WriteBytes
    )
    if (-not $script:history.ContainsKey($Key)) {
        $script:history[$Key] = [System.Collections.ArrayList]::new()
    }
    [void]$script:history[$Key].Add([pscustomobject]@{
        Ts         = $Now
        ReadBytes  = $ReadBytes
        WriteBytes = $WriteBytes
    })
    # Prune points older than 24h
    $cutoff = $Now.AddHours(-24)
    while ($script:history[$Key].Count -gt 1 -and $script:history[$Key][0].Ts -lt $cutoff) {
        $script:history[$Key].RemoveAt(0)
    }
}

function Get-WindowMB {
    param(
        [System.Collections.ArrayList]$Points,
        [datetime]$Now,
        [double]$CurrentBytes,
        [int]$WindowSeconds,
        [string]$FieldName
    )
    if (-not $Points -or $Points.Count -eq 0) { return 0.0 }
    $cutoff = $Now.AddSeconds(-1 * $WindowSeconds)
    $baseline = $Points[0]
    foreach ($point in $Points) {
        if ($point.Ts -ge $cutoff) {
            $baseline = $point
            break
        }
        $baseline = $point
    }
    $deltaBytes = $CurrentBytes - [double]$baseline.$FieldName
    # Negative delta signals a counter reset (process restart with same PID)
    if ($deltaBytes -lt 0) { return $null }    # sentinel for "reset"
    return [math]::Round($deltaBytes / 1MB, 2)
}

function Get-LevelText {
    param([double]$MaxMBPerSec)
    if ($MaxMBPerSec -ge 5)   { return '重度' }
    if ($MaxMBPerSec -ge 1)   { return '高' }
    if ($MaxMBPerSec -ge 0.2) { return '中' }
    if ($MaxMBPerSec -ge $MinMBPerSec) { return '轻' }
    return '空闲'
}

function Get-LevelColor {
    param([double]$MaxMBPerSec)
    if ($MaxMBPerSec -ge 5)   { return 'Red' }
    if ($MaxMBPerSec -ge 1)   { return 'Yellow' }
    if ($MaxMBPerSec -ge 0.2) { return 'Green' }
    return $null
}

function Get-MaxOfList {
    param([double[]]$Values)
    if (-not $Values -or $Values.Count -eq 0) { return 0.0 }
    return ($Values | Measure-Object -Maximum).Maximum
}

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
function Format-Cell {
    param([string]$Text, [int]$Width)
    if ($null -eq $Text) { $Text = '' }
    if ($Text.Length -gt $Width) {
        return $Text.Substring(0, $Width - 1) + '…'
    }
    return $Text.PadRight($Width)
}

function Show-Table {
    param(
        [string]$Title,
        [object[]]$Rows,
        [int]$Take
    )
    Write-Host ""
    Write-Colored $Title 'Bold'

    $header = ("{0} {1} {2} {3} {4} {5} {6} {7}" -f `
        (Format-Cell '级别' 4),
        (Format-Cell '1m(MB)' 8),
        (Format-Cell '1h(MB)' 8),
        (Format-Cell '24h(MB)' 8),
        (Format-Cell '当前MB/s' 10),
        (Format-Cell '累计MB' 8),
        (Format-Cell 'PID' 7),
        (Format-Cell '进程' 24))
    Write-Colored $header 'Dim'

    $selected = @($Rows | Select-Object -First $Take)
    $totalCurrent = 0.0
    $totalAccum = 0.0

    foreach ($row in $selected) {
        $level = [string]$row.级别
        $levelColor = if ($level -eq '重度') { 'Red' }
                      elseif ($level -eq '高') { 'Yellow' }
                      elseif ($level -eq '中') { 'Green' }
                      else { $null }

        $m1  = if ($row.'1m' -eq $null) { '   n/a ' }  else { '{0,7:N2} ' -f $row.'1m' }
        $h1  = if ($row.'1h' -eq $null) { '   n/a ' }  else { '{0,7:N2} ' -f $row.'1h' }
        $h24 = if ($row.'24h' -eq $null) { '   n/a ' } else { '{0,7:N2} ' -f $row.'24h' }

        $line = ("{0} {1} {2} {3} {4} {5} {6} {7}" -f `
            (Format-Cell $level 4),
            $m1,
            $h1,
            $h24,
            ('{0,9:N3} ' -f $row.当前MB每秒),
            ('{0,7:N2} ' -f $row.累计MB),
            (Format-Cell ([string]$row.PID) 7),
            (Format-Cell ([string]$row.进程) 24))
        Write-Colored $line $levelColor

        $totalCurrent += $row.当前MB每秒
        $totalAccum  += $row.累计MB
    }

    # Fill remaining rows with blanks for alignment stability
    for ($i = $selected.Count; $i -lt $Take; $i++) {
        Write-Host ("{0} {1} {2} {3} {4} {5} {6} {7}" -f `
            (Format-Cell '' 4), (Format-Cell '' 8), (Format-Cell '' 8), (Format-Cell '' 8),
            (Format-Cell '' 10), (Format-Cell '' 8), (Format-Cell '' 7), (Format-Cell '' 24))
    }

    # Summary
    $sumLine = ("{0} {1} {2} {3} {4} {5} {6} {7}" -f `
        (Format-Cell 'Σ' 4),
        (Format-Cell '' 8),
        (Format-Cell '' 8),
        (Format-Cell '' 8),
        ('{0,9:N3} ' -f $totalCurrent),
        ('{0,7:N2} ' -f $totalAccum),
        (Format-Cell '' 7),
        (Format-Cell "$($selected.Count) 进程" 24))
    Write-Colored $sumLine 'Dim'
}

# ---------------------------------------------------------------------------
# Dead process history cleanup
# ---------------------------------------------------------------------------
function Remove-DeadProcessHistory {
    param([hashtable]$LiveKeys)
    $dead = @($script:history.Keys | Where-Object { $_ -notin $LiveKeys })
    foreach ($key in $dead) {
        $script:history.Remove($key)
    }
    if ($dead.Count -gt 0) {
        Write-Host ("[清理] 移除 {0} 个已退出进程的历史记录" -f $dead.Count)
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$script:sessionTotals = @{}
$script:history = @{}
$previousPaths = if ($WatchPath) { Get-PathSizes -Paths $WatchPath } else { @{} }
$iteration = 0
$startedAt = Get-Date
$lastCleanup = $startedAt

while ($true) {
    try {
        Start-Sleep -Seconds $IntervalSeconds
    } catch {
        # Interrupted during sleep
        break
    }
    $iteration++
    $now = Get-Date

    # --- Sample ----------------------------------------------------------
    $sample = Get-ProcessSample
    if (-not $sample) {
        Write-Colored "[$($now.ToString('yyyy-MM-dd HH:mm:ss'))] 无法获取进程性能数据，等待下次采样..." 'Yellow'
        if ($Iterations -gt 0 -and $iteration -ge $Iterations) { break }
        continue
    }

    $liveKeys = [System.Collections.Generic.HashSet[string]]::new()

    $rows = foreach ($item in $sample) {
        $readMBPerSec  = [math]::Round($item.ReadBytesPerSec / 1MB, 3)
        $writeMBPerSec = [math]::Round($item.WriteBytesPerSec / 1MB, 3)
        $key = "$($item.Id)|$($item.Name)"
        [void]$liveKeys.Add($key)

        if (-not $script:sessionTotals.ContainsKey($key)) {
            $script:sessionTotals[$key] = @{ ReadBytes = 0.0; WriteBytes = 0.0 }
        }

        # Detect PID reuse: if the current perf-counter rate is huge relative
        # to accumulated history, the PID was likely recycled.
        $resetDetected = $false
        if ($script:sessionTotals[$key].WriteBytes -gt 100MB -and
            $item.WriteBytesPerSec -lt 1MB -and
            $item.ReadBytesPerSec -lt 1MB) {
            # Low activity after heavy accumulation → PID reused
            $script:sessionTotals[$key] = @{ ReadBytes = 0.0; WriteBytes = 0.0 }
            $script:history.Remove($key)
            $resetDetected = $true
        }

        $script:sessionTotals[$key].ReadBytes  += ($item.ReadBytesPerSec * $IntervalSeconds)
        $script:sessionTotals[$key].WriteBytes += ($item.WriteBytesPerSec * $IntervalSeconds)

        Add-HistoryPoint -Key $key -Now $now `
            -ReadBytes $script:sessionTotals[$key].ReadBytes `
            -WriteBytes $script:sessionTotals[$key].WriteBytes

        $maxMBPerSec = [math]::Max($readMBPerSec, $writeMBPerSec)

        [pscustomobject]@{
            级别       = Get-LevelText -MaxMBPerSec $maxMBPerSec
            PID        = $item.Id
            进程       = if ($resetDetected) { "$($item.Name) [reset]" } else { $item.Name }
            当前读MB每秒 = $readMBPerSec
            当前写MB每秒 = $writeMBPerSec
            当前总MB每秒 = [math]::Round($readMBPerSec + $writeMBPerSec, 3)
            累计读MB    = [math]::Round($script:sessionTotals[$key].ReadBytes / 1MB, 2)
            累计写MB    = [math]::Round($script:sessionTotals[$key].WriteBytes / 1MB, 2)
            累计总MB    = [math]::Round(($script:sessionTotals[$key].ReadBytes + $script:sessionTotals[$key].WriteBytes) / 1MB, 2)
            读1m = Get-WindowMB -Points $script:history[$key] -Now $now -CurrentBytes $script:sessionTotals[$key].ReadBytes  -WindowSeconds 60   -FieldName 'ReadBytes'
            读1h = Get-WindowMB -Points $script:history[$key] -Now $now -CurrentBytes $script:sessionTotals[$key].ReadBytes  -WindowSeconds 3600 -FieldName 'ReadBytes'
            读24h= Get-WindowMB -Points $script:history[$key] -Now $now -CurrentBytes $script:sessionTotals[$key].ReadBytes  -WindowSeconds 86400 -FieldName 'ReadBytes'
            写1m = Get-WindowMB -Points $script:history[$key] -Now $now -CurrentBytes $script:sessionTotals[$key].WriteBytes -WindowSeconds 60   -FieldName 'WriteBytes'
            写1h = Get-WindowMB -Points $script:history[$key] -Now $now -CurrentBytes $script:sessionTotals[$key].WriteBytes -WindowSeconds 3600 -FieldName 'WriteBytes'
            写24h= Get-WindowMB -Points $script:history[$key] -Now $now -CurrentBytes $script:sessionTotals[$key].WriteBytes -WindowSeconds 86400 -FieldName 'WriteBytes'
        }
    }

    # --- Filter ----------------------------------------------------------
    $visibleRows = @($rows | Where-Object {
        $_.当前读MB每秒 -ge $MinMBPerSec -or
        $_.当前写MB每秒 -ge $MinMBPerSec -or
        $_.累计读MB -gt 0 -or
        $_.累计写MB -gt 0
    })

    # --- Build read / write sorted views --------------------------------
    $readTop = @($visibleRows |
        Sort-Object @{Expression = { Get-MaxOfList @($_.读24h, $_.读1h, $_.读1m, $_.累计读MB) }; Descending = $true } |
        ForEach-Object {
            [pscustomobject]@{
                级别       = $_.级别
                '1m'       = $_.读1m
                '1h'       = $_.读1h
                '24h'      = $_.读24h
                当前MB每秒 = $_.当前读MB每秒
                累计MB     = $_.累计读MB
                PID        = $_.PID
                进程       = $_.进程
            }
        })

    $writeTop = @($visibleRows |
        Sort-Object @{Expression = { Get-MaxOfList @($_.写24h, $_.写1h, $_.写1m, $_.累计写MB) }; Descending = $true } |
        ForEach-Object {
            [pscustomobject]@{
                级别       = $_.级别
                '1m'       = $_.写1m
                '1h'       = $_.写1h
                '24h'      = $_.写24h
                当前MB每秒 = $_.当前写MB每秒
                累计MB     = $_.累计写MB
                PID        = $_.PID
                进程       = $_.进程
            }
        })

    # --- Output ---------------------------------------------------------
    if ($Json) {
        $output = [pscustomobject]@{
            ts               = $now.ToString('yyyy-MM-dd HH:mm:ss')
            interval_seconds = $IntervalSeconds
            iteration        = $iteration
            read_top = @($readTop | Select-Object -First $Top | ForEach-Object {
                [pscustomobject]@{
                    level            = $_.级别
                    pid              = $_.PID
                    process          = $_.进程
                    rate_mb_per_sec  = $_.当前MB每秒
                    accum_mb         = $_.累计MB
                    window_1m_mb     = $_.'1m'
                    window_1h_mb     = $_.'1h'
                    window_24h_mb    = $_.'24h'
                }
            })
            write_top = @($writeTop | Select-Object -First $Top | ForEach-Object {
                [pscustomobject]@{
                    level            = $_.级别
                    pid              = $_.PID
                    process          = $_.进程
                    rate_mb_per_sec  = $_.当前MB每秒
                    accum_mb         = $_.累计MB
                    window_1m_mb     = $_.'1m'
                    window_1h_mb     = $_.'1h'
                    window_24h_mb    = $_.'24h'
                }
            })
            paths = if ($WatchPath) {
                @($WatchPath | ForEach-Object {
                    $prev = $previousPaths[$_]
                    $curr = Get-PathSizes -Paths @($_) | ForEach-Object { $_.Values }
                    [pscustomobject]@{
                        path     = $_
                        previous = $prev
                        current  = $curr
                        delta    = if ($null -ne $prev -and $null -ne $curr) { $curr - $prev } else { $null }
                    }
                })
            } else { @() }
        }
        Write-Host ($output | ConvertTo-Json -Depth 4 -Compress)
    } else {
        Write-Host ""
        Write-Colored ("[{0}] 进程磁盘读写监视" -f $now.ToString('yyyy-MM-dd HH:mm:ss')) 'Bold'
        Write-Colored ("采样间隔: {0}s | 运行时长: {1:hh\hmm\m} | 说明: 1h/24h 需持续运行足够久才完整" -f
            $IntervalSeconds, ($now - $startedAt)) 'Dim'

        Show-Table -Title ("累计读取 Top {0}" -f $Top) -Rows $readTop -Take $Top
        Show-Table -Title ("累计写入 Top {0}" -f $Top) -Rows $writeTop -Take $Top

        if ($WatchPath) {
            $currentPaths = Get-PathSizes -Paths $WatchPath
            Write-Host ""
            Write-Colored "监视文件" 'Bold'
            Write-Colored ("{0} {1} {2}" -f (Format-Cell '状态' 6), (Format-Cell '增量' 14), '路径') 'Dim'
            foreach ($path in $WatchPath) {
                $beforeSize = $previousPaths[$path]
                $afterSize  = $currentPaths[$path]
                $delta = if ($null -ne $beforeSize -and $null -ne $afterSize) { $afterSize - $beforeSize } else { $null }
                if ($delta -gt 0) {
                    $status = '增长'
                    $color  = 'Yellow'
                } elseif ($null -eq $delta) {
                    $status = '未知'
                    $color  = 'Red'
                } else {
                    $status = '稳定'
                    $color  = 'Green'
                }
                Write-Colored ("{0} {1} {2}" -f `
                    (Format-Cell $status 6),
                    (Format-Cell ([string]$delta) 14),
                    $path) $color
            }
            $previousPaths = $currentPaths
        }
    }

    # --- Periodic dead-process cleanup ---------------------------------
    if (($now - $lastCleanup).TotalMinutes -ge 5) {
        Remove-DeadProcessHistory -LiveKeys $liveKeys
        $lastCleanup = $now
    }

    if ($Iterations -gt 0 -and $iteration -ge $Iterations) {
        break
    }
}

Write-Colored "`n监视结束。" 'Dim'
