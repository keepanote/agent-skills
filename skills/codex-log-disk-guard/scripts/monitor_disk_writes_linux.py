#!/usr/bin/env python3
"""Monitor Linux per-process disk write rates via /proc/<pid>/io.

Shows top N processes by write throughput — current rate *and* session-
accumulated totals.  Supports optional ANSI colour and alternate-screen
refresh (opt-in via --color / --refresh).
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


# ======================================================================
# ANSI escape sequences (only emitted when colour is enabled)
# ======================================================================
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_GREEN = "\033[92m"
_CYAN = "\033[96m"

_CURSOR_HIDE = "\033[?25l"
_CURSOR_SHOW = "\033[?25h"
_ALT_ENTER = "\033[?1049h"
_ALT_LEAVE = "\033[?1049l"


# ======================================================================
# Helpers
# ======================================================================
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def _human_size(n: float) -> str:
    sign = ""
    if n < 0:
        sign = "-"
        n = -n
    for unit in _SIZE_UNITS:
        if n < 1024 or unit == _SIZE_UNITS[-1]:
            return f"{sign}{n:,.2f} {unit}"
        n /= 1024
    return f"{sign}{n:,.2f} {_SIZE_UNITS[-1]}"


def _human_rate(bytes_per_sec: float) -> Tuple[float, str]:
    if bytes_per_sec < 1024:
        return bytes_per_sec, " B/s"
    if bytes_per_sec < 1024 ** 2:
        return bytes_per_sec / 1024, "KB/s"
    if bytes_per_sec < 1024 ** 3:
        return bytes_per_sec / (1024 ** 2), "MB/s"
    return bytes_per_sec / (1024 ** 3), "GB/s"


def _term_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 120


# ======================================================================
# Colour & refresh decisions
# ======================================================================
def _want_colour(args: argparse.Namespace) -> bool:
    """Resolve whether to emit ANSI colours."""
    if os.environ.get("NO_COLOR"):
        return False
    if args.color:
        return True
    if os.environ.get("FORCE_COLOR"):
        return True
    # Auto-detect: only when explicitly asked or TERM advertises colour
    if not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    if "256color" in term or "color" in term:
        return True
    return False


def _want_refresh(args: argparse.Namespace) -> bool:
    """Resolve whether to use alternate-screen refresh."""
    if args.json or args.no_refresh:
        return False
    if args.refresh:
        return True
    # Auto-detect: refresh only makes sense with a colour-capable TTY
    if not sys.stdout.isatty():
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    term = os.environ.get("TERM", "")
    if "256color" in term or "color" in term:
        return True
    return False


# ======================================================================
# Terminal display
# ======================================================================
class TerminalDisplay:
    """Frame-buffered output with optional alternate-screen refresh."""

    def __init__(self, use_colour: bool, use_refresh: bool) -> None:
        self._colour = use_colour
        self._refresh = use_refresh
        self._active = False
        self._buf: List[str] = []

    @property
    def colour(self) -> bool:
        return self._colour

    def add(self, text: str, *, colour: str = "", bold: bool = False, dim: bool = False) -> None:
        prefix = ""
        if self._colour:
            if bold:
                prefix += _BOLD
            if dim:
                prefix += _DIM
            if colour:
                prefix += colour
        suffix = _RESET if prefix else ""
        self._buf.append(f"{prefix}{text}{suffix}")

    def rule(self) -> None:
        if self._colour:
            self.add("─" * _term_width(), dim=True)
        else:
            self._buf.append("─" * _term_width())

    def flush(self) -> None:
        """Write the frame; in refresh mode, homes cursor each time."""
        frame = "\n".join(self._buf)
        self._buf.clear()

        out: List[str] = []
        if self._refresh:
            if not self._active:
                out.append(_ALT_ENTER)
                out.append(_CURSOR_HIDE)
                self._active = True
            out.append("\033[H")   # home
        out.append(frame)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def cleanup(self) -> None:
        if self._refresh and self._active:
            sys.stdout.write(_CURSOR_SHOW)
            sys.stdout.write(_ALT_LEAVE)
            sys.stdout.flush()


# ======================================================================
# /proc data
# ======================================================================
def _check_proc() -> pathlib.Path:
    root = pathlib.Path("/proc")
    if not root.is_dir():
        raise SystemExit("Error: /proc filesystem not available on this system.")
    return root


def read_process_sample() -> Dict[int, dict]:
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
    out: Dict[str, Optional[int]] = {}
    for p_str in paths:
        p = pathlib.Path(p_str)
        out[p_str] = p.stat().st_size if p.exists() else None
    return out


# ======================================================================
# Frame builder
# ======================================================================
def _colour_for_rate(rate: float) -> str:
    if rate > 10 * 1024 * 1024:
        return _RED
    if rate > 1 * 1024 * 1024:
        return _YELLOW
    if rate >= 1024:
        return _GREEN
    return ""


def build_frame(
    disp: TerminalDisplay,
    top_rows: List[dict],
    total_rate: float,
    total_accum: int,
    rows_total: int,
    reset_count: int,
    iteration: int,
    interval: float,
    top_n: int,
    watch_paths: List[str],
    prev_paths: Dict[str, Optional[int]],
    curr_paths: Dict[str, Optional[int]],
) -> None:
    """Add all lines for one monitoring frame."""

    # --- Title ---
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tval, tunit = _human_rate(total_rate)
    parts = [
        f"[{ts}]", f"interval={interval}s", f"top={top_n}",
        f"Σ rate={tval:.2f}{tunit}", f"Σ acc={_human_size(total_accum)}", f"#{iteration}",
    ]
    if reset_count:
        parts.append(f"⚠ {reset_count} reset(s)")
    disp.add("  ".join(parts), bold=True, colour=_CYAN)

    # --- Table ---
    width = _term_width()
    disp.rule()
    disp.add(f"{'RATE':>9}  {'ACCUM':>10}  {'PID':>7}  PROCESS", bold=True)
    disp.rule()

    if not top_rows:
        disp.add("  (no process write activity in this interval)", dim=True)
    else:
        for row in top_rows:
            rate = row["rate"]
            accum = row["accum"]
            rval, runit = _human_rate(rate)
            col = _colour_for_rate(rate)
            name = row["name"]
            flag = row.get("flag", "")

            flag_str = ""
            if flag:
                flag_str = f" {_RED}{flag}{_RESET}" if disp.colour else f" {flag}"

            # Truncate name
            prefix_len = 9 + 2 + 10 + 2 + 7 + 2
            max_name = max(6, width - prefix_len - len(flag) - 2)
            if len(name) > max_name:
                name = name[: max_name - 1] + "…"

            disp.add(
                f"{rval:8.2f}{runit}  {_human_size(accum):>10}  {row['pid']:7d}  {name}{flag_str}",
                colour=col,
            )

    # --- Footer ---
    disp.rule()
    disp.add(
        f"{'Total':>9}  {_human_size(total_accum):>10}  {'':>7}  {rows_total} processes",
        dim=True, colour=_colour_for_rate(total_rate),
    )

    # --- Watched paths ---
    if watch_paths:
        disp.add("")
        disp.add(f"{'DELTA':>12}  PATH", bold=True)
        for path in watch_paths:
            prev = prev_paths.get(path)
            curr = curr_paths.get(path)
            if prev is not None and curr is not None:
                delta = curr - prev
                delta_str = _human_size(delta)
            else:
                delta_str = "n/a"
            disp.add(f"{delta_str:>12}  {path}")


# ======================================================================
# Args
# ======================================================================
def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Monitor Linux per-process disk write rates from /proc/<pid>/io."
    )
    p.add_argument("--interval-seconds", type=float, default=2.0)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--iterations", type=int, default=0,
                   help="0 = run until interrupted")
    p.add_argument("--watch-path", action="append", default=[])
    p.add_argument("--json", action="store_true")
    p.add_argument("--color", action="store_true",
                   help="Enable ANSI colour output (or set FORCE_COLOR=1)")
    p.add_argument("--refresh", action="store_true",
                   help="Enable alternate-screen in-place refresh")
    p.add_argument("--no-refresh", action="store_true",
                   help="Disable refresh (scroll output; useful for logging)")
    return p.parse_args(argv)


# ======================================================================
# Main
# ======================================================================
def main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)

    shutdown = False
    def _on_signal(signum: int, frame: object) -> None:
        nonlocal shutdown
        shutdown = True
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    use_colour = _want_colour(args)
    use_refresh = _want_refresh(args)

    # --- Baseline sample ---
    try:
        previous = read_process_sample()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error reading /proc: {exc}", file=sys.stderr)
        sys.exit(1)

    previous_paths = read_path_sizes(args.watch_path)
    iteration = 0

    # Session-accumulated write bytes per PID
    session_accum: Dict[int, int] = {}

    disp = TerminalDisplay(use_colour, use_refresh)

    if not args.json:
        kind = "colour+refresh" if use_refresh else ("colour" if use_colour else "plain text")
        print(f"Sampling every {args.interval_seconds}s, top {args.top}  "
              f"[{kind}]  Ctrl+C to stop.", file=sys.stderr)

    try:
        while not shutdown:
            time.sleep(args.interval_seconds)
            iteration += 1

            current = read_process_sample()
            rows: List[dict] = []
            total_rate = 0.0
            total_accum = 0
            reset_count = 0

            for pid, now in current.items():
                before = previous.get(pid)
                if before is None:
                    continue

                raw_delta = now["write_bytes"] - before["write_bytes"]
                flag = ""
                if raw_delta < 0:
                    # Counter reset — process restarted
                    flag = "[reset]"
                    reset_count += 1
                    raw_delta = now["write_bytes"]
                    session_accum[pid] = 0

                delta = max(0, raw_delta)
                rate = delta / args.interval_seconds

                # Update session accumulator
                session_accum[pid] = session_accum.get(pid, 0) + delta
                accum = session_accum[pid]

                total_rate += rate
                total_accum += accum

                rows.append({
                    "pid": pid, "name": now["name"],
                    "delta": delta, "rate": rate, "accum": accum, "flag": flag,
                })

            rows.sort(key=lambda item: item["rate"], reverse=True)
            top_rows = rows[: args.top]
            current_paths = read_path_sizes(args.watch_path)

            # --- Output ---
            if args.json:
                print(json.dumps({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "interval_seconds": args.interval_seconds,
                    "iteration": iteration,
                    "total_rate_bytes_per_sec": total_rate,
                    "total_accum_bytes": total_accum,
                    "processes_seen": len(rows),
                    "resets_detected": reset_count,
                    "rows": [
                        {
                            "pid": r["pid"], "name": r["name"],
                            "delta_bytes": r["delta"],
                            "rate_bytes_per_sec": r["rate"],
                            "accum_bytes": r["accum"],
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
                }, ensure_ascii=False))
            else:
                build_frame(
                    disp, top_rows, total_rate, total_accum, len(rows),
                    reset_count, iteration, args.interval_seconds, args.top,
                    args.watch_path, previous_paths, current_paths,
                )
                disp.flush()

            previous = current
            previous_paths = current_paths

            if args.iterations > 0 and iteration >= args.iterations:
                break

    finally:
        disp.cleanup()
        if shutdown:
            print("Interrupted.", file=sys.stderr)


if __name__ == "__main__":
    main()
