#!/usr/bin/env python3
"""Monitor Linux per-process disk write rates via /proc/<pid>/io.

Shows top N processes by write throughput with in-place refresh (no scroll),
colour-coded rates, counter-reset detection, and graceful Ctrl+C shutdown.
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
# ANSI escape sequences
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


# ======================================================================
# Colour detection
# ======================================================================
def _detect_colour(no_color_flag: bool) -> bool:
    """Decide whether to emit ANSI colour codes.

    Respects NO_COLOR / FORCE_COLOR, then assumes any modern TTY has colour.
    """
    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    # Assume yes for any interactive terminal — virtually all modern
    # terminal emulators (including VS Code, JetBrains, iTerm2, Windows
    # Terminal, Gnome Terminal, Konsole, etc.) support ANSI colours.
    return True


def _colour_for_rate(rate_bytes_per_sec: float) -> str:
    """Return an ANSI colour for a write rate: red→yellow→green→none."""
    if rate_bytes_per_sec > 10 * 1024 * 1024:   # > 10 MB/s  🔴 heavy
        return _RED
    if rate_bytes_per_sec > 1 * 1024 * 1024:    # >  1 MB/s  🟡 moderate
        return _YELLOW
    if rate_bytes_per_sec >= 1024:              # ≥  1 KB/s  🟢 light
        return _GREEN
    return ""


# ======================================================================
# Formatting helpers
# ======================================================================
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
    return f"{sign}{n:,.2f} {_SIZE_UNITS[-1]}"


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


# ======================================================================
# Terminal display — alternate-screen refresh (like top / htop / vim)
# ======================================================================
class TerminalDisplay:
    """Buffered full-frame output using the alternate screen buffer.

    On first render the terminal switches to the *alternate screen*
    (``\\033[?1049h``), clearing the window.  Each subsequent render
    homes the cursor (``\\033[H``) and redraws — no line counting, no
    flicker, no scrolling.  On exit the original screen is restored
    (``\\033[?1049l``).

    When *use_refresh* is False (pipe, ``--no-refresh``, or ``--json``)
    the alternate screen is **not** used; each frame is printed as a
    standalone block — suitable for logging or processing.
    """

    _ALT_ENTER = "\033[?1049h"   # switch to alternate screen buffer
    _ALT_LEAVE = "\033[?1049l"   # restore original screen buffer

    def __init__(self, use_colour: bool, use_refresh: bool = True) -> None:
        self._use_colour = use_colour
        self._use_refresh = use_refresh
        self._active = False
        self._buf: List[str] = []

    # -- public API --------------------------------------------------------

    @property
    def use_colour(self) -> bool:
        return self._use_colour

    def add(self, text: str, *, colour: str = "", bold: bool = False, dim: bool = False) -> None:
        """Append a line to the frame buffer (may contain embedded newlines)."""
        prefix = ""
        if bold:
            prefix += _BOLD
        if dim:
            prefix += _DIM
        if colour and self._use_colour:
            prefix += colour
        suffix = _RESET if prefix else ""
        self._buf.append(f"{prefix}{text}{suffix}")

    def add_rule(self) -> None:
        """Append a horizontal rule spanning the terminal width."""
        self.add("─" * _term_width(), dim=True)

    def render(self) -> None:
        """Write the buffered frame to stdout.

        In refresh mode the cursor is homed to (1,1) before each frame so
        the display updates in-place.  Outside refresh mode each frame is
        printed as a standalone block.
        """
        frame = "\n".join(self._buf)
        self._buf.clear()

        out: List[str] = []

        if self._use_refresh:
            if not self._active:
                # Enter alternate screen + hide cursor (once)
                out.append(self._ALT_ENTER)
                out.append(_CURSOR_HIDE)
                self._active = True
            # Home cursor to top-left for each frame
            out.append("\033[H")

        out.append(frame)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def cleanup(self) -> None:
        """Leave alternate screen and show cursor."""
        if self._use_refresh and self._active:
            sys.stdout.write(_CURSOR_SHOW)
            sys.stdout.write(self._ALT_LEAVE)
            sys.stdout.flush()


# ======================================================================
# /proc data collection
# ======================================================================
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


# ======================================================================
# Frame builder — builds the entire display frame as a list of lines
# ======================================================================
def build_frame(
    disp: TerminalDisplay,
    top_rows: List[dict],
    total_rate: float,
    rows_total: int,
    reset_count: int,
    iteration: int,
    interval: float,
    top_n: int,
    watch_paths: List[str],
    previous_paths: Dict[str, Optional[int]],
    current_paths: Dict[str, Optional[int]],
) -> None:
    """Add all lines for one monitoring frame to *disp*."""

    # --- Title bar ---
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tval, tunit = _human_rate(total_rate)
    parts = [f"[{ts}]", f"interval={interval}s", f"top={top_n}",
             f"Σ={tval:.2f}{tunit}", f"#{iteration}"]
    if reset_count:
        parts.append(f"⚠ {reset_count} reset(s)")
    disp.add("  ".join(parts), bold=True, colour=_CYAN)

    # --- Table ---
    width = _term_width()
    disp.add_rule()
    disp.add(f"{'RATE':>9}  {'PID':>7}  PROCESS", bold=True)
    disp.add_rule()

    if not top_rows:
        disp.add("  (no process write activity in this interval)", dim=True)
    else:
        for row in top_rows:
            rate = row["rate"]
            val, unit = _human_rate(rate)
            col = _colour_for_rate(rate)
            name = row["name"]
            flag = row.get("flag", "")

            flag_str = ""
            if flag:
                flag_str = f" {_RED}{flag}{_RESET}" if disp.use_colour else f" {flag}"

            # Truncate name so line fits terminal
            prefix_len = 9 + 2 + 7 + 2
            max_name = max(6, width - prefix_len - len(flag) - 2)
            if len(name) > max_name:
                name = name[: max_name - 1] + "…"

            disp.add(
                f"{val:8.2f}{unit}  {row['pid']:7d}  {name}{flag_str}",
                colour=col,
            )

    # --- Footer with totals ---
    disp.add_rule()
    disp.add(
        f"{'Total':>9}  {'':>7}  {tval:.2f}{tunit} across {rows_total} processes",
        dim=True, colour=_colour_for_rate(total_rate),
    )

    # --- Watched paths ---
    if watch_paths:
        disp.add("")
        disp.add(f"{'DELTA':>12}  PATH", bold=True)
        for path in watch_paths:
            prev = previous_paths.get(path)
            curr = current_paths.get(path)
            if prev is not None and curr is not None:
                delta = curr - prev
                delta_str = _human_size(delta)
            else:
                delta_str = "n/a"
            disp.add(f"{delta_str:>12}  {path}")


# ======================================================================
# Argument parsing
# ======================================================================
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
    parser.add_argument(
        "--no-refresh", action="store_true",
        help="Disable in-place refresh (print each sample as a new block; useful for logging)",
    )
    return parser.parse_args(argv)


# ======================================================================
# Main
# ======================================================================
def main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)

    # --- Signal handling ---
    shutdown = False

    def _on_signal(signum: int, frame: object) -> None:
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    use_colour = _detect_colour(args.no_color)
    use_refresh = not args.json and not args.no_refresh and sys.stdout.isatty()

    # --- First sample (baseline) ---
    try:
        previous = read_process_sample()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error reading /proc: {exc}", file=sys.stderr)
        sys.exit(1)

    previous_paths = read_path_sizes(args.watch_path)
    iteration = 0

    # --- Display ---
    disp = TerminalDisplay(use_colour, use_refresh)

    # One-time hint to stderr (doesn't interfere with the display)
    if not args.json:
        print(f"Sampling every {args.interval_seconds}s, showing top {args.top}. "
              f"Press Ctrl+C to stop.", file=sys.stderr)

    # --- Main loop ---
    try:
        while not shutdown:
            time.sleep(args.interval_seconds)
            iteration += 1

            current = read_process_sample()
            rows: List[dict] = []
            total_rate = 0.0
            reset_count = 0

            for pid, now in current.items():
                before = previous.get(pid)
                if before is None:
                    continue

                raw_delta = now["write_bytes"] - before["write_bytes"]

                # Counter reset detection
                flag = ""
                if raw_delta < 0:
                    flag = "[reset]"
                    reset_count += 1
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

            # --- Output ---
            if args.json:
                print(json.dumps({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "interval_seconds": args.interval_seconds,
                    "iteration": iteration,
                    "total_rate_bytes_per_sec": total_rate,
                    "processes_seen": len(rows),
                    "resets_detected": reset_count,
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
                }, ensure_ascii=False))
            else:
                build_frame(
                    disp, top_rows, total_rate, len(rows), reset_count,
                    iteration, args.interval_seconds, args.top,
                    args.watch_path, previous_paths, current_paths,
                )
                disp.render()

            # --- Advance ---
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
