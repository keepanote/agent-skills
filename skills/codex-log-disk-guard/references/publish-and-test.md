# Publish And Test

## Support Matrix

- `scripts/codex_log_guard.py`
  Supported on Windows, Ubuntu, and macOS as long as Python 3 with stdlib `sqlite3` is available.

- `scripts/monitor_disk_writes.ps1`
  Supported on Windows only. It depends on PowerShell and Windows performance counters.

- `scripts/monitor_disk_writes_linux.py`
  Supported on Ubuntu and other Linux systems with `/proc/<pid>/io`.

- `scripts/monitor_disk_writes_macos.py`
  Supported on macOS. Uses `iostat` for aggregate disk throughput and `psutil` for process information (CPU/RSS). Requires `psutil` (`pip install psutil`). Per-process disk write bytes are not available on macOS.

## Publish Strategy

Prefer a small release artifact instead of shipping the whole skill folder blindly.

### Option 1: Copy the skill folder

Use this when the target machine already has Codex and should auto-discover the skill.

- Target path:
  `~/.codex/skills/codex-log-disk-guard`
- Copy:
  `SKILL.md`
  `agents/openai.yaml`
  `scripts/`
  `references/`

### Option 2: Package the scripts as a standalone toolkit

Use this when the target machine does not need Codex skill discovery.

- Keep the layout:
  `codex-log-disk-guard/scripts/codex_log_guard.py`
  `codex-log-disk-guard/scripts/monitor_disk_writes.ps1`
  `codex-log-disk-guard/scripts/monitor_disk_writes_linux.py`
- Add executable bits on Unix:
  `chmod +x scripts/*.py`
- Invoke directly with:
  `python3 scripts/codex_log_guard.py inspect`

## OS-Specific Install Notes

### Ubuntu

Install Python 3:

```bash
sudo apt-get update
sudo apt-get install -y python3
```

Smoke test:

```bash
python3 scripts/codex_log_guard.py --help
python3 scripts/monitor_disk_writes_linux.py --help
```

### macOS

Use the system Python only if it includes `sqlite3`; otherwise install a current Python 3.

Install psutil:

```bash
pip install psutil
```

Smoke test:

```bash
python3 scripts/codex_log_guard.py --help
python3 scripts/monitor_disk_writes_macos.py --help
```

For per-process filesystem activity:

```bash
sudo fs_usage -w -f filesystem
```

### Windows

Smoke test:

```powershell
python scripts\codex_log_guard.py --help
powershell -ExecutionPolicy Bypass -File scripts\monitor_disk_writes.ps1 -Iterations 1
```

## Compatibility Test Plan

Run these tests on each supported OS before claiming compatibility.

### Test 1: CLI help

This catches broken entrypoints, missing imports, and syntax errors.

```bash
python3 scripts/codex_log_guard.py --help
```

Windows:

```powershell
python scripts\codex_log_guard.py --help
```

### Test 2: Synthetic SQLite DB

Create a disposable SQLite database with a `logs` table that matches the expected schema, then run:

```bash
python3 scripts/codex_log_guard.py inspect --db ./test-logs.sqlite
python3 scripts/codex_log_guard.py guard --db ./test-logs.sqlite --sample-seconds 2
python3 scripts/codex_log_guard.py vacuum --db ./test-logs.sqlite
python3 scripts/codex_log_guard.py unblock --db ./test-logs.sqlite
```

Verify:

- `guard` creates a backup file
- `guard` creates `codex_block_logs_insert`
- `checkpoint` returns successfully
- `vacuum` does not fail
- `unblock` drops the trigger

### Test 3: Trigger behavior

After `guard`, try inserting a row into `logs`.

Expected result:

- the insert is ignored
- `MAX(id)` does not advance
- WAL does not regrow under repeated samples

### Test 4: Platform monitor

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\monitor_disk_writes.ps1 -Iterations 1 -Top 5
```

Ubuntu:

```bash
python3 scripts/monitor_disk_writes_linux.py --iterations 1 --top 5
```

macOS:

```bash
pip install psutil
python3 scripts/monitor_disk_writes_macos.py --iterations 1 --top 5 --no-refresh
```

Verify: disk write throughput from iostat is displayed, top processes are listed. Per-process disk write bytes not supported; validate with `sudo fs_usage -w -f filesystem` if needed.

## CI Recommendation

Use a matrix job with:

- `windows-latest`
- `ubuntu-latest`
- `macos-latest`

Run on every platform:

- `python -m py_compile scripts/codex_log_guard.py`
- `python scripts/codex_log_guard.py --help`

Run on Ubuntu only:

- `python scripts/monitor_disk_writes_linux.py --help`

Run on Windows only:

- `powershell -ExecutionPolicy Bypass -File scripts/monitor_disk_writes.ps1 -Iterations 1 -Top 3`

Do not run the Windows monitor on macOS or Linux, and do not advertise macOS per-process write-rate support unless a dedicated implementation is added.
