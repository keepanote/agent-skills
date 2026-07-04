---
name: codex-log-disk-guard
description: Inspect, mitigate, and monitor runaway Codex SQLite log writes on Windows, macOS, and Linux. Use when Codex needs to analyze `~/.codex/logs.sqlite` or `~/.codex/logs_2.sqlite`, estimate write amplification or disk wear, back up the database, block further `logs` inserts with a SQLite trigger, checkpoint or truncate WAL, compact the database, or monitor which processes are writing heavily to disk.
---

# Codex Log Disk Guard

Use the bundled scripts instead of hand-writing ad hoc SQL or one-off PowerShell unless the task clearly needs a different workflow.

## Prerequisites

Python 3 with stdlib `sqlite3` module. macOS ships with Python 3 + sqlite3 by default.

### macOS

```bash
# Verify Python 3 and sqlite3 are available
python3 -c "import sqlite3; print('ready')"
```

### Ubuntu / Debian

```bash
sudo apt-get update && sudo apt-get install -y python3
```

### Windows

Install Python 3 from [python.org](https://www.python.org/downloads/) (ensure "Add Python to PATH" is checked).

## Quick Start

Inspect the default Codex log database and estimate write volume:

```bash
python3 scripts/codex_log_guard.py inspect
```

Back up the database, install a `BEFORE INSERT` trigger on `logs`, truncate WAL, and verify that `MAX(id)` stops growing:

```bash
python3 scripts/codex_log_guard.py guard --sample-seconds 8
```

Compact the database after inserts are blocked:

```bash
python3 scripts/codex_log_guard.py vacuum
```

Watch top disk-writing processes on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/monitor_disk_writes.ps1 -Top 12 -IntervalSeconds 2
```

Watch top disk-writing processes on Linux:

```bash
python3 scripts/monitor_disk_writes_linux.py --top 12 --interval-seconds 2
```

Watch top processes and disk throughput on macOS:

```bash
python3 scripts/monitor_disk_writes_macos.py --top 12 --interval-seconds 2
```

For per-process filesystem detail (requires sudo):

```bash
sudo fs_usage -w -f filesystem
```

## Example user prompts

- `Use $codex-log-disk-guard to inspect ~/.codex/logs_2.sqlite, back it up, block further logs inserts with a trigger, checkpoint and truncate WAL, then verify that MAX(id) and WAL stop growing.`
- `使用 $codex-log-disk-guard 检查 ~/.codex/logs_2.sqlite，必要时先备份，再拦截 logs insert，checkpoint/truncate WAL，最后确认 MAX(id) 和 WAL 不再增长。`

## Workflow

1. Resolve the target DB path.
   Prefer `~/.codex/logs_2.sqlite` when present, otherwise use `~/.codex/logs.sqlite`.

2. Inspect before changing anything.
   Run `python scripts/codex_log_guard.py inspect` and review:
   `MAX(id)`, `COUNT(*)`, `SUM(estimated_bytes)`, `page_count`, `freelist_count`, WAL size, and the hottest `level/target` groups.

3. Decide whether the DB is in a write storm.
   Treat it as suspicious when one or more of these are true:
   `TRACE` dominates recent rows, `MAX(id)` advances quickly, WAL keeps regrowing, or `page_count` is huge while live rows are small.

4. Mitigate safely.
   Run `python scripts/codex_log_guard.py guard`.
   This script backs up the DB, creates `codex_block_logs_insert` if absent, runs `PRAGMA wal_checkpoint(TRUNCATE)`, and samples stability.

5. Reclaim space only after writes are blocked.
   Run `python scripts/codex_log_guard.py vacuum`.
   Avoid `VACUUM` first when the DB is still being hammered; it just competes with the write storm.

6. Monitor broader disk churn when the machine still writes heavily.
   Run `scripts/monitor_disk_writes.ps1` on Windows or `scripts/monitor_disk_writes_linux.py` on Linux.

## Operating Rules

- Preserve a backup before creating or replacing a trigger.
- Prefer the SQLite backup API or `VACUUM`; do not copy a hot DB file blindly unless you accept an inconsistent snapshot.
- Use `RAISE(IGNORE)` in the trigger so new inserts are silently dropped instead of crashing callers.
- Treat `MAX(id)` as a monotonic sequence with gaps. A stable `MAX(id)` across repeated samples is the main stop-the-bleeding signal.
- Expect SSD wear estimates to be lower-bounded by DB and WAL growth. Real physical writes are usually higher because SQLite writes pages and checkpoints, not only row payload bytes.

## Platform Support

| Script | Windows | Ubuntu | macOS |
|--------|---------|--------|-------|
| `codex_log_guard.py` | Yes | Yes | Yes |
| `monitor_disk_writes.ps1` | Yes | No | No |
| `monitor_disk_writes_linux.py` | No | Yes | No |
| `monitor_disk_writes_macos.py` | No | No | Yes |
| Per-process write counters | Via PS | Via `/proc/<pid>/io` | Not available |

macOS shows aggregate disk throughput via `iostat` and top processes by CPU/RSS via `psutil`. Use `fs_usage` for per-process filesystem detail.

## Scripts

- `scripts/codex_log_guard.py`
  Inspect, back up, guard, sample, checkpoint, and compact the Codex SQLite log DB. Cross-platform (Windows, macOS, Linux).

- `scripts/monitor_disk_writes.ps1`
  Sample Windows per-process write rates and optionally watch one or more file paths for size changes.

- `scripts/monitor_disk_writes_linux.py`
  Sample Linux per-process write deltas from `/proc/<pid>/io` and optionally watch one or more file paths for size changes.

- `scripts/monitor_disk_writes_macos.py`
  Sample macOS aggregate disk write throughput from `iostat` and top processes by CPU/RSS from `psutil`. Per-process disk write bytes are not available on macOS; use `fs_usage` for deeper diagnosis.

## References

- `references/publish-and-test.md`
  Publish, package, and test this skill on Windows, Ubuntu, and macOS. Read this before claiming cross-platform support for the monitoring scripts.
