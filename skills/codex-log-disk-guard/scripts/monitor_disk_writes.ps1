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
            ReadBytesPerSec = [double]$row.IOReadBytesPersec
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

function Format-Cell {
    param(
        [string]$Text,
        [int]$Width
    )
    if ($null -eq $Text) { $Text = '' }
    if ($Text.Length -gt $Width) {
        return $Text.Substring(0, $Width - 1) + '…'
    }
    return $Text.PadRight($Width)
}

function Get-LevelText {
    param([double]$MaxMBPerSec)
    if ($MaxMBPerSec -ge 5) { return '重度' }
    if ($MaxMBPerSec -ge 1) { return '高' }
    if ($MaxMBPerSec -ge 0.2) { return '中' }
    if ($MaxMBPerSec -ge $MinMBPerSec) { return '轻' }
    return '空闲'
}

function Get-MaxOfList {
    param([double[]]$Values)
    $max = [double]::MinValue
    foreach ($value in $Values) {
        if ($value -gt $max) {
            $max = $value
        }
    }
    return $max
}

function Ensure-History {
    param([string]$Key)
    if (-not $history.ContainsKey($Key)) {
        $history[$Key] = New-Object System.Collections.ArrayList
    }
}

function Add-HistoryPoint {
    param(
        [string]$Key,
        [datetime]$Now,
        [double]$ReadBytes,
        [double]$WriteBytes
    )
    Ensure-History -Key $Key
    [void]$history[$Key].Add([pscustomobject]@{
        Ts = $Now
        ReadBytes = $ReadBytes
        WriteBytes = $WriteBytes
    })
    $cutoff = $Now.AddHours(-24)
    while ($history[$Key].Count -gt 1 -and $history[$Key][0].Ts -lt $cutoff) {
        $history[$Key].RemoveAt(0)
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
    if (-not $Points -or $Points.Count -eq 0) {
        return 0.0
    }
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
    if ($deltaBytes -lt 0) { $deltaBytes = 0 }
    return [math]::Round($deltaBytes / 1MB, 2)
}

function Show-Table {
    param(
        [string]$Title,
        [object[]]$Rows,
        [int]$Take
    )
    Write-Host ""
    Write-Host $Title
    Write-Host ("{0} {1} {2} {3} {4} {5} {6} {7}" -f `
        (Format-Cell '级别' 4), `
        (Format-Cell '1m' 7), `
        (Format-Cell '1h' 7), `
        (Format-Cell '24h' 7), `
        (Format-Cell '当前MB/s' 9), `
        (Format-Cell '累计MB' 8), `
        (Format-Cell 'PID' 7), `
        (Format-Cell '进程' 24))
    Write-Host ("{0} {1} {2} {3} {4} {5} {6} {7}" -f `
        (Format-Cell '----' 4), `
        (Format-Cell '-------' 7), `
        (Format-Cell '-------' 7), `
        (Format-Cell '-------' 7), `
        (Format-Cell '---------' 9), `
        (Format-Cell '--------' 8), `
        (Format-Cell '-------' 7), `
        (Format-Cell '------------------------' 24))

    $selected = @($Rows | Select-Object -First $Take)
    foreach ($row in $selected) {
        Write-Host ("{0} {1} {2} {3} {4} {5} {6} {7}" -f `
            (Format-Cell ([string]$row.级别) 4), `
            (Format-Cell ([string]('{0:N2}' -f $row.'1m')) 7), `
            (Format-Cell ([string]('{0:N2}' -f $row.'1h')) 7), `
            (Format-Cell ([string]('{0:N2}' -f $row.'24h')) 7), `
            (Format-Cell ([string]('{0:N3}' -f $row.当前MB每秒)) 9), `
            (Format-Cell ([string]('{0:N2}' -f $row.累计MB)) 8), `
            (Format-Cell ([string]$row.PID) 7), `
            (Format-Cell ([string]$row.进程) 24))
    }
    for ($i = $selected.Count; $i -lt $Take; $i++) {
        Write-Host ("{0} {1} {2} {3} {4} {5} {6} {7}" -f `
            (Format-Cell '' 4), (Format-Cell '' 7), (Format-Cell '' 7), (Format-Cell '' 7), `
            (Format-Cell '' 9), (Format-Cell '' 8), (Format-Cell '' 7), (Format-Cell '' 24))
    }
}

$sessionTotals = @{}
$history = @{}
$previousPaths = if ($WatchPath) { Get-PathSizes -Paths $WatchPath } else { @{} }
$iteration = 0
$startedAt = Get-Date

while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    $iteration++
    $now = Get-Date

    $rows = foreach ($item in Get-ProcessSample) {
        $readMBPerSec = [math]::Round($item.ReadBytesPerSec / 1MB, 3)
        $writeMBPerSec = [math]::Round($item.WriteBytesPerSec / 1MB, 3)
        $key = "$($item.Id)|$($item.Name)"
        if (-not $sessionTotals.ContainsKey($key)) {
            $sessionTotals[$key] = @{
                ReadBytes = 0.0
                WriteBytes = 0.0
            }
        }
        $sessionTotals[$key].ReadBytes += ($item.ReadBytesPerSec * $IntervalSeconds)
        $sessionTotals[$key].WriteBytes += ($item.WriteBytesPerSec * $IntervalSeconds)

        Add-HistoryPoint -Key $key -Now $now -ReadBytes $sessionTotals[$key].ReadBytes -WriteBytes $sessionTotals[$key].WriteBytes
        $maxMBPerSec = [math]::Max($readMBPerSec, $writeMBPerSec)

        [pscustomobject]@{
            级别 = Get-LevelText -MaxMBPerSec $maxMBPerSec
            PID = $item.Id
            进程 = $item.Name
            当前读MB每秒 = $readMBPerSec
            当前写MB每秒 = $writeMBPerSec
            当前总MB每秒 = [math]::Round($readMBPerSec + $writeMBPerSec, 3)
            累计读MB = [math]::Round($sessionTotals[$key].ReadBytes / 1MB, 2)
            累计写MB = [math]::Round($sessionTotals[$key].WriteBytes / 1MB, 2)
            累计总MB = [math]::Round(($sessionTotals[$key].ReadBytes + $sessionTotals[$key].WriteBytes) / 1MB, 2)
            读1m = Get-WindowMB -Points $history[$key] -Now $now -CurrentBytes $sessionTotals[$key].ReadBytes -WindowSeconds 60 -FieldName 'ReadBytes'
            读1h = Get-WindowMB -Points $history[$key] -Now $now -CurrentBytes $sessionTotals[$key].ReadBytes -WindowSeconds 3600 -FieldName 'ReadBytes'
            读24h = Get-WindowMB -Points $history[$key] -Now $now -CurrentBytes $sessionTotals[$key].ReadBytes -WindowSeconds 86400 -FieldName 'ReadBytes'
            写1m = Get-WindowMB -Points $history[$key] -Now $now -CurrentBytes $sessionTotals[$key].WriteBytes -WindowSeconds 60 -FieldName 'WriteBytes'
            写1h = Get-WindowMB -Points $history[$key] -Now $now -CurrentBytes $sessionTotals[$key].WriteBytes -WindowSeconds 3600 -FieldName 'WriteBytes'
            写24h = Get-WindowMB -Points $history[$key] -Now $now -CurrentBytes $sessionTotals[$key].WriteBytes -WindowSeconds 86400 -FieldName 'WriteBytes'
        }
    }

    $visibleRows = @($rows | Where-Object {
        $_.当前读MB每秒 -ge $MinMBPerSec -or
        $_.当前写MB每秒 -ge $MinMBPerSec -or
        $_.累计读MB -gt 0 -or
        $_.累计写MB -gt 0
    })

    $readTop = @($visibleRows |
        Sort-Object @{Expression = { Get-MaxOfList @($_.读24h, $_.读1h, $_.读1m, $_.累计读MB) }; Descending = $true } |
        ForEach-Object {
            [pscustomobject]@{
                级别 = $_.级别
                '1m' = $_.读1m
                '1h' = $_.读1h
                '24h' = $_.读24h
                当前MB每秒 = $_.当前读MB每秒
                累计MB = $_.累计读MB
                PID = $_.PID
                进程 = $_.进程
            }
        })

    $writeTop = @($visibleRows |
        Sort-Object @{Expression = { Get-MaxOfList @($_.写24h, $_.写1h, $_.写1m, $_.累计写MB) }; Descending = $true } |
        ForEach-Object {
            [pscustomobject]@{
                级别 = $_.级别
                '1m' = $_.写1m
                '1h' = $_.写1h
                '24h' = $_.写24h
                当前MB每秒 = $_.当前写MB每秒
                累计MB = $_.累计写MB
                PID = $_.PID
                进程 = $_.进程
            }
        })

    Write-Host ""
    Write-Host ("[{0}] 进程磁盘读写监视" -f $now.ToString('yyyy-MM-dd HH:mm:ss'))
    Write-Host ("采样间隔: {0}s | 说明: 1h/24h 需要脚本持续运行足够久才会逐渐完整" -f $IntervalSeconds)
    Show-Table -Title ("累计读取 Top {0}" -f $Top) -Rows $readTop -Take $Top
    Show-Table -Title ("累计写入 Top {0}" -f $Top) -Rows $writeTop -Take $Top

    if ($WatchPath) {
        $currentPaths = Get-PathSizes -Paths $WatchPath
        Write-Host ""
        Write-Host "监视文件"
        Write-Host ("{0} {1} {2}" -f (Format-Cell '状态' 6), (Format-Cell '增量字节' 12), '路径')
        Write-Host ("{0} {1} {2}" -f (Format-Cell '------' 6), (Format-Cell '------------' 12), '----')
        foreach ($path in $WatchPath) {
            $beforeSize = $previousPaths[$path]
            $afterSize = $currentPaths[$path]
            $delta = if ($null -ne $beforeSize -and $null -ne $afterSize) { $afterSize - $beforeSize } else { $null }
            $status = if ($delta -gt 0) { '增长' } elseif ($delta -eq 0) { '稳定' } else { '未知' }
            Write-Host ("{0} {1} {2}" -f `
                (Format-Cell $status 6), `
                (Format-Cell ([string]$delta) 12), `
                $path)
        }
        $previousPaths = $currentPaths
    }

    if ($Iterations -gt 0 -and $iteration -ge $Iterations) {
        break
    }
}
