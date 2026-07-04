#!/usr/bin/env python3
"""Monitor Linux per-process disk write rates via /proc/<pid>/io.

Shows top N processes by write throughput with appropriate rate units,
optional file-size watching, counter-reset detection, colour output,
and graceful shutdown on Ctrl+C.
"""

import argparse
import json
import os
import pathlib
import shutil
import signal
import sys
import time
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_GREEN = "\033[92m"


def _colour_for_rate(rate_bytes_per_sec: float) -> str:
    """Return an ANSI colour code for a write rate, or empty string."""
    if rate_bytes_per_sec > 10 * 1024 * 1024:      # > 10 MB/s
        return _RED
    if rate_bytes_per_sec > 1 * 1024 * 1024:       # >  1 MB/s
        return _YELLOW
    return ""


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def _human_size(n: float) -> str:
    """Return a human-readable size (e.g. '12.34 KB', '-4.00 MB')."""
    sign = ""
    if n < 0:
        sign = "-"
        n = -n
    for unit in _SIZE_UNITS:
        if n < 1024 or unit == _SIZE_UNITS[-1]:
            return f"{sign}{n:,.2f} {unit}"
        n /= 1024
    return f"{sign}{n:,.2f} {_SIZE_UNITS[-1]}"  # unreachable, kept for safety


def _human_rate(bytes_per_sec: float) -> Tuple[float, str]:
    """Return (scaled_value, unit_str) for a byte rate."""
    if bytes_per_sec < 1024:
        return bytes_per_sec, " B/s"
    if bytes_per_sec < 1024 ** 2:
        return bytes_per_sec / 1024, "KB/s"
    if bytes_per_sec < 1024 ** 3:
        return bytes_per_sec / (1024 ** 2), "MB/s"
    return bytes_per_sec / (1024 ** 3), "GB/s"


def _term_width() -> int:
    """Best-effort terminal width."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 120


# ---------------------------------------------------------------------------
# /proc data collection
# ---------------------------------------------------------------------------

def _check_proc() -> pathlib.Path:
    """Return /proc path or exit with a clear message."""
    root = pathlib.Path("/proc")
    if not root.is_dir():
        raise SystemExit("Error: /proc filesystem not available on this system.")
    return root


def read_process_sample() -> Dict[int, dict]:
    """Snapshot every visible process's write_bytes from /proc/<pid>/io.

    Returns:
        {pid: {"pid": int, "name": str, "write_bytes": int}}
    """
    root = _check_proc()
    rows: Dict[int, dict] = {}
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        io_file = entry / "io"
        comm_file = entry / "comm"

        try:
            name = comm_file.read_text(encoding="utf-8", errors="replace").strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue

        try:
            write_bytes = 0
            for line in io_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("write_bytes:"):
                    write_bytes = int(line.split(":", 1)[1].strip())
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue

        rows[pid] = {"pid": pid, "name": name, "write_bytes": write_bytes}
    return rows


def read_path_sizes(paths: List[str]) -> Dict[str, Optional[int]]:
    """Return {path: size_in_bytes} for each path (None if missing)."""
    out: Dict[str, Optional[int]] = {}
    for p_str in paths:
        p = pathlib.Path(p_str)
        out[p_str] = p.stat().st_size if p.exists() else None
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_table(
    rows: List[dict],
    interval: float,
    use_colour: bool,
    total_rate: float,
) -> None:
    """Print a formatted top-N table of per-process write rates."""
    width = _term_width()

    # Header
    print(f"\n{_BOLD}{'RATE':>9}  {'PID':>7}  PROCESS{_RESET}")
    print(f"{_DIM}{'─' * width}{_RESET}")

    if not rows:
        print(f"{_DIM}  (no process write activity detected in this interval){_RESET}")
        return

    for row in rows:
        rate = row["rate"]                       # bytes/sec
        val, unit = _human_rate(rate)

        col = _colour_for_rate(rate) if use_colour else ""
        name = row["name"]
        flag = row.get("flag", "")

        # Reserve space for the flag
        flag_str = f" {_RED}{flag}{_RESET}" if flag and use_colour else f" {flag}" if flag else ""

        # Truncate name so the line fits terminal width
        # 9 (rate) + 2 spaces + 7 (pid) + 2 spaces + name + flag
        prefix_len = 9 + 2 + 7 + 2
        max_name = max(6, width - prefix_len - len(flag_str) - 2)
        if len(name) > max_name:
            name = name[: max_name - 1] + "…"

        print(
            f"{col}{val:8.2f}{unit}  {row['pid']:7d}  {name}{flag_str}{_RESET}"
        )

    # Summary footer
    tval, tunit = _human_rate(total_rate)
    print(f"{_DIM}{'─' * width}{_RESET}")
    print(f"{_DIM}{'Total':>9}  {'':>7}  {tval:.2f}{tunit} across {len(rows)} processes{_RESET}")


def render_path_table(
    before: Dict[str, Optional[int]],
    after: Dict[str, Optional[int]],
    paths: List[str],
) -> None:
    """Print file-size deltas for watched paths."""
    print(f"\n{_BOLD}{'DELTA':>12}  PATH{_RESET}")
    for path in paths:
        prev = before.get(path)
        curr = after.get(path)
        if prev is not None and curr is not None:
            delta = curr - prev
            delta_str = _human_size(delta)
        else:
            delta_str = "n/a"
        print(f"{delta_str:>12}  {path}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Linux per-process disk write rates from /proc/<pid>/io."
    )
    parser.add_argument(
        "--interval-seconds", type=float, default=2.0,
        help="Seconds between samples (default: 2.0)",
    )
    parser.add_argument(
        "--top", type=int, default=10,
        help="Number of top processes to show (default: 10)",
    )
    parser.add_argument(
        "--iterations", type=int, default=0,
        help="Number of iterations; 0 = run until interrupted (default: 0)",
    )
    parser.add_argument(
        "--watch-path", action="append", default=[],
        help="File or directory path to watch for size changes (repeatable)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON lines instead of a human-readable table",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI colour output",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------
    shutdown = False

    def _on_signal(signum: int, frame: object) -> None:
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    use_colour = not args.no_color and sys.stdout.isatty()

    # ------------------------------------------------------------------
    # First sample (baseline)
    # ------------------------------------------------------------------
    try:
        previous = read_process_sample()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error reading /proc: {exc}", file=sys.stderr)
        sys.exit(1)

    previous_paths = read_path_sizes(args.watch_path)
    iteration = 0

    print(f"Sampling every {args.interval_seconds}s, showing top {args.top}. "
          f"Press Ctrl+C to stop.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    while not shutdown:
        time.sleep(args.interval_seconds)
        iteration += 1

        current = read_process_sample()
        rows: List[dict] = []
        total_rate = 0.0
        reset_count = 0
        new_count = 0

        for pid, now in current.items():
            before = previous.get(pid)
            if before is None:
                # Process started after our baseline — skip first delta
                new_count += 1
                continue

            raw_delta = now["write_bytes"] - before["write_bytes"]

            # Detect counter reset: when write_bytes goes backwards the
            # process (or one of its threads) has restarted and the
            # kernel counter started from zero again.
            flag = ""
            if raw_delta < 0:
                flag = "[reset]"
                reset_count += 1
                # Best-effort: treat the current counter value as the
                # delta for this interval.
                raw_delta = now["write_bytes"]

            delta = max(0, raw_delta)
            rate = delta / args.interval_seconds
            total_rate += rate

            rows.append({
                "pid": pid,
                "name": now["name"],
                "delta": delta,
                "rate": rate,
                "flag": flag,
            })

        rows.sort(key=lambda item: item["rate"], reverse=True)
        top_rows = rows[: args.top]

        current_paths = read_path_sizes(args.watch_path)

        # ----------------------------------------------------------
        # Output
        # ----------------------------------------------------------
        if args.json:
            print(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "interval_seconds": args.interval_seconds,
                        "iteration": iteration,
                        "total_rate_bytes_per_sec": total_rate,
                        "processes_seen": len(rows),
                        "resets_detected": reset_count,
                        "new_processes_skipped": new_count,
                        "rows": [
                            {
                                "pid": r["pid"],
                                "name": r["name"],
                                "delta_bytes": r["delta"],
                                "rate_bytes_per_sec": r["rate"],
                                "flag": r.get("flag", ""),
                            }
                            for r in top_rows
                        ],
                        "paths": [
                            {
                                "path": p,
                                "previous": previous_paths.get(p),
                                "current": current_paths.get(p),
                                "delta": (
                                    current_paths.get(p) - previous_paths.get(p)
                                    if previous_paths.get(p) is not None
                                    and current_paths.get(p) is not None
                                    else None
                                ),
                            }
                            for p in args.watch_path
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            tval, tunit = _human_rate(total_rate)
            extra = ""
            if reset_count:
                extra += f"  ⚠ {reset_count} reset(s)"
            print(f"\n{_BOLD}[{ts}]  interval={args.interval_seconds}s  "
                  f"top={args.top}  Σ={tval:.2f}{tunit}{extra}{_RESET}")

            render_table(top_rows, args.interval_seconds, use_colour, total_rate)

            if args.watch_path:
                render_path_table(previous_paths, current_paths, args.watch_path)

        # ----------------------------------------------------------
        # Advance
        # ----------------------------------------------------------
        previous = current
        previous_paths = current_paths

        if args.iterations > 0 and iteration >= args.iterations:
            break

    if shutdown:
        print("\nInterrupted.", file=sys.stderr)


if __name__ == "__main__":
    main()
