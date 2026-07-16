# sysclean — 系统内存工作集压缩与磁盘临时清理
# 压缩进程工作集、清理临时文件、可选清空回收站
# 用法: clean.ps1 [-IncludeRecycleBin] [-MinProcessMB 50] [-TempAgeDays 2]

[CmdletBinding()]
param(
    [switch]$IncludeRecycleBin,          # 显式开启才清空回收站(不可恢复)
    [int]$MinProcessMB = 50,             # 仅压缩工作集大于此值(MB)的进程
    [int]$TempAgeDays  = 2               # 仅删除修改时间早于该天数的临时文件
)

# ── EmptyWorkingSet P/Invoke(最小权限)─────────────────────────────────
Add-Type -Name Native -Namespace Win32 -MemberDefinition @'
[DllImport("psapi.dll", SetLastError=true)]
public static extern bool EmptyWorkingSet(System.IntPtr hProcess);
[DllImport("kernel32.dll")]
public static extern System.IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, uint dwProcessId);
[DllImport("kernel32.dll")]
public static extern bool CloseHandle(System.IntPtr hObject);
'@

# EmptyWorkingSet 只需 PROCESS_QUERY_INFORMATION(0x0400)| PROCESS_SET_QUOTA(0x0100)。
# 用 PROCESS_ALL_ACCESS 会让 OpenProcess 对多数进程失败,覆盖率反而更低。
$ACCESS    = 0x0400 -bor 0x0100          # 0x0500
$MIN_BYTES = $MinProcessMB * 1MB

# 必然打不开、无需尝试的系统进程(避免 openFail 计数虚高)
$excludeNames = @('System','Idle','MemCompression','Registry','csrss','smss','wininit','winlogon','services','lsass')

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

# ── 清理前内存 ────────────────────────────────────────────────────────
$os = Get-CimInstance Win32_OperatingSystem
$totalGB    = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$freeBefore = [math]::Round($os.FreePhysicalMemory   / 1MB, 1)
$usedBefore = [math]::Round($totalGB - $freeBefore, 1)
$pctBefore  = [math]::Round($usedBefore / $totalGB * 100, 1)

$adminTag = if ($isAdmin) { '管理员' } else { '普通权限' }
Write-Host "=== 清理前: 总计 ${totalGB}GB | 已用 ${usedBefore}GB | 可用 ${freeBefore}GB | ${pctBefore}% | $adminTag ===" -ForegroundColor Cyan

# ── 1. 压缩进程工作集 ─────────────────────────────────────────────────
Write-Host "`n[1/4] 压缩进程工作集..." -ForegroundColor Yellow
$trimmed = 0; $freedBytes = 0; $openFail = 0

Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Id -ne $PID -and $_.WorkingSet64 -gt $MIN_BYTES -and $excludeNames -notcontains $_.Name
} | ForEach-Object {
    $before = $_.WorkingSet64
    $h = [Win32.Native]::OpenProcess($ACCESS, $false, $_.Id)
    if ($h -eq [IntPtr]::Zero) { $openFail++; return }
    try {
        [void][Win32.Native]::EmptyWorkingSet($h)
        $_.Refresh()
        $diff = $before - $_.WorkingSet64
        if ($diff -gt 5MB) { $freedBytes += $diff; $trimmed++ }
    } finally {
        [void][Win32.Native]::CloseHandle($h)
    }
}
Write-Host "  压缩了 $trimmed 个进程, 挤出 $([math]::Round($freedBytes/1MB,1)) MB 工作集" -ForegroundColor Green
if (-not $isAdmin -and $openFail -gt 0) {
    Write-Host "  ($openFail 个进程无权访问;以管理员运行可覆盖更多)" -ForegroundColor DarkYellow
}

# ── 2. 临时文件 ────────────────────────────────────────────────────────
Write-Host "`n[2/4] 清理临时文件(>${TempAgeDays}天)..." -ForegroundColor Yellow
$tempRoots = @([System.IO.Path]::GetTempPath(), "$env:WINDIR\Temp") | Select-Object -Unique
$cutoff = (Get-Date).AddDays(-$TempAgeDays)
$fileCount = 0; $fileFail = 0; $bytesDel = 0

foreach ($root in $tempRoots) {
    if (-not (Test-Path $root)) { continue }
    Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        ForEach-Object {
            try {
                $sz = $_.Length
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
                $fileCount++; $bytesDel += $sz
            } catch { $fileFail++ }
        }
    # 删除随之变空的目录(深层优先)
    Get-ChildItem -LiteralPath $root -Recurse -Directory -Force -ErrorAction SilentlyContinue |
        Sort-Object { $_.FullName.Length } -Descending |
        ForEach-Object {
            try {
                if (-not (Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue)) {
                    Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
                }
            } catch {}
        }
}
$msg = "  删除 $fileCount 个文件, 释放磁盘 $([math]::Round($bytesDel/1MB,1)) MB"
if ($fileFail) { $msg += " ($fileFail 个被占用跳过)" }
Write-Host $msg -ForegroundColor Green

# ── 3. 回收站(默认跳过,清空不可恢复)────────────────────────────────
Write-Host "`n[3/4] 回收站..." -ForegroundColor Yellow
if ($IncludeRecycleBin) {
    try {
        Clear-RecycleBin -Force -Confirm:$false -ErrorAction Stop
        Write-Host "  已清空(不可恢复)" -ForegroundColor Green
    } catch {
        Write-Host "  回收站为空或无法访问" -ForegroundColor DarkGray
    }
} else {
    Write-Host "  已跳过(加 -IncludeRecycleBin 可清空,注意不可恢复)" -ForegroundColor DarkGray
}

# ── 4. 最终报告 ────────────────────────────────────────────────────────
Write-Host "`n[4/4] 内存状态" -ForegroundColor Yellow
$os2 = Get-CimInstance Win32_OperatingSystem
$freeAfter = [math]::Round($os2.FreePhysicalMemory / 1MB, 1)
$usedAfter = [math]::Round($totalGB - $freeAfter, 1)
$pctAfter  = [math]::Round($usedAfter / $totalGB * 100, 1)
$netFree   = [math]::Round($freeAfter - $freeBefore, 1)
$color = if ($pctAfter -gt 80) { 'Red' } elseif ($pctAfter -gt 60) { 'Yellow' } else { 'Green' }
$sign  = if ($netFree -ge 0) { "+$netFree" } else { "$netFree" }

Write-Host ""
Write-Host "  总计 ${totalGB}GB | 已用 ${usedAfter}GB | 可用 ${freeAfter}GB | 使用率 ${pctAfter}%" -ForegroundColor $color
Write-Host "  物理可用内存变化: ${sign} GB" -ForegroundColor $color
Write-Host "  注: 挤出的页面多为可被换回的缓存,活跃进程会重新读回,数字为瞬时值。" -ForegroundColor DarkGray

# Top 5 内存大户(针对性处理比无差别压缩更有效)
Write-Host "`n内存 Top 5:" -ForegroundColor Magenta
Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 5 | ForEach-Object {
    Write-Host "  $($_.Name) (PID $($_.Id)): $([math]::Round($_.WorkingSet64/1MB,1)) MB"
}
