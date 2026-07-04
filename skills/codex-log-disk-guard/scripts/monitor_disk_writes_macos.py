#!/usr/bin/env python3
"""Monitor macOS disk write activity via iostat and psutil.

Shows aggregate disk write throughput from iostat alongside top processes
by CPU (per-process disk write bytes are not available on macOS). Supports
optional ANSI colour and alternate-screen refresh.
"""

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

try:
    import psutil
except ImportError:
    print("Error: psutil is required. Install with: pip install psutil", file=sys.stderr)
    sys.exit(1)


_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_GREEN = "\033[92m"
_CYAN = "\033[96m"
_BLUE = "\033[94m"

_CURSOR_HIDE = "\033[?25l"
_CURSOR_SHOW = "\033[?25h"
_ALT_ENTER = "\033[?1049h"
_ALT_LEAVE = "\033[?1049l"

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def _human_size(n: float) -> str:
    sign = ""
    if n < 0:
        sign = "-"
        n = -n
    for unit in _SIZE_UNITS:
        if n < 1024 or unit == _SIZE_UNITS[-1]:
            return f"{sign}{n:.2f} {unit}"
        n /= 1024
    return f"{sign}{n:.2f} {_SIZE_UNITS[-1]}"


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


def _want_colour(args: argparse.Namespace) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if args.color:
        return True
    if os.environ.get("FORCE_COLOR"):
        return True
    if not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    if "256color" in term or "color" in term:
        return True
    return False


def _want_refresh(args: argparse.Namespace) -> bool:
    if args.json or args.no_refresh:
        return False
    if args.refresh:
        return True
    if not sys.stdout.isatty():
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    term = os.environ.get("TERM", "")
    if "256color" in term or "color" in term:
        return True
    return False


class TerminalDisplay:
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
        frame = "\n".join(self._buf)
        self._buf.clear()
        out: List[str] = []
        if self._refresh:
            if not self._active:
                out.append(_ALT_ENTER)
                out.append(_CURSOR_HIDE)
                self._active = True
            out.append("\033[H")
        else:
            out.append("\n")
        out.append(frame)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def cleanup(self) -> None:
        if self._refresh and self._active:
            sys.stdout.write(_CURSOR_SHOW)
            sys.stdout.write(_ALT_LEAVE)
            sys.stdout.flush()


class IostatReader:
    """Reads disk write throughput from iostat in background."""

    def __init__(self, interval: float):
        self._interval = interval
        self._proc: Optional[subprocess.Popen] = None
        self._disk_mb_per_s = 0.0
        self._disk_xfrs_per_s = 0.0
        self._disk_name = ""
        self._started = False

    def start(self) -> None:
        cmd = ["iostat", "-Id", str(int(self._interval))]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        # skip header line, fields line, and cumulative (since boot) line
        self._proc.stdout.readline()
        self._proc.stdout.readline()
        self._proc.stdout.readline()
        self._started = True

    def read_current(self) -> Tuple[float, float, str]:
        """Returns (MB/s, xfers/s, disk_name). Blocks until next sample line."""
        if not self._started:
            self.start()
            return (0.0, 0.0, "")
        line = self._proc.stdout.readline()
        if not line:
            return (0.0, 0.0, "")
        parts = line.strip().split()
        if len(parts) >= 3:
            try:
                kbt = float(parts[0])
                xfrs = float(parts[1])
                mb = float(parts[2])
                self._disk_mb_per_s = mb
                self._disk_xfrs_per_s = xfrs
            except (ValueError, IndexError):
                pass
        return (self._disk_mb_per_s, self._disk_xfrs_per_s, "")

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


def read_process_sample() -> List[dict]:
    rows = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            rows.append({
                "pid": proc.pid,
                "name": info["name"] or "",
                "cpu": info["cpu_percent"] or 0.0,
                "rss": info["memory_info"].rss if info["memory_info"] else 0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def read_path_sizes(paths: List[str]) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for p_str in paths:
        p = pathlib.Path(p_str)
        out[p_str] = p.stat().st_size if p.exists() else None
    return out


def _cpu_colour(cpu: float) -> str:
    if cpu > 50:
        return _RED
    if cpu > 20:
        return _YELLOW
    if cpu > 5:
        return _GREEN
    return ""


def build_frame(
    disp: TerminalDisplay,
    procs: List[dict],
    disk_mb_s: float,
    disk_xfrs_s: float,
    disk_bytes_s: float,
    iteration: int,
    interval: float,
    top_n: int,
    watch_paths: List[str],
    prev_paths: Dict[str, Optional[int]],
    curr_paths: Dict[str, Optional[int]],
) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        f"[{ts}]", f"interval={interval}s", f"top={top_n}",
        f"disk write: {disk_mb_s:.2f} MB/s ({disk_xfrs_s:.0f} xfrs/s)", f"#{iteration}",
    ]
    disp.add("  ".join(parts), bold=True, colour=_CYAN)

    disp.rule()
    disp.add(f"{'CPU%':>6}  {'RSS':>10}  {'PID':>7}  PROCESS", bold=True)
    disp.rule()

    if not procs:
        disp.add("  (no processes)", dim=True)
    else:
        width = _term_width()
        for row in procs:
            col = _cpu_colour(row["cpu"])
            name = row["name"]
            prefix_len = 6 + 2 + 10 + 2 + 7 + 2
            max_name = max(6, width - prefix_len - 2)
            if len(name) > max_name:
                name = name[: max_name - 1] + "\u2026"
            disp.add(
                f"{row['cpu']:5.1f}%  {_human_size(row['rss']):>10}  {row['pid']:7d}  {name}",
                colour=col,
            )

    disp.rule()
    total_rss = sum(p["rss"] for p in procs)
    disp.add(
        f"{'':>6}  {_human_size(total_rss):>10}  {'':>7}  {len(procs)} processes",
        dim=True,
    )

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

    disp.add("")
    disp.add(
        "Tip: per-process disk write bytes not available on macOS. "
        "Use sudo fs_usage -w -f filesystem for per-process filesystem activity.",
        dim=True, colour=_BLUE,
    )


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Monitor macOS disk write throughput via iostat and top processes via psutil."
    )
    p.add_argument("--interval-seconds", type=float, default=2.0)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--iterations", type=int, default=0,
                   help="0 = run until interrupted")
    p.add_argument("--watch-path", action="append", default=[])
    p.add_argument("--json", action="store_true")
    p.add_argument("--color", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--no-refresh", action="store_true")
    return p.parse_args(argv)


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
    disp = TerminalDisplay(use_colour, use_refresh)

    if not args.json:
        kind = "colour+refresh" if use_refresh else ("colour" if use_colour else "plain text")
        print(f"Sampling every {args.interval_seconds}s, top {args.top}  "
              f"[{kind}]  Ctrl+C to stop.", file=sys.stderr)

    # Start iostat background reader
    iostat = IostatReader(args.interval_seconds + 0.5)
    iostat.start()
    # Prime psutil with an initial call so first cpu_percent is non-zero
    psutil.cpu_percent(interval=0.1)
    previous_paths = read_path_sizes(args.watch_path)
    iteration = 0

    try:
        while not shutdown:
            # Read iostat sample (blocks ~interval seconds)
            disk_mb_s, disk_xfrs_s, disk_name = iostat.read_current()
            iteration += 1

            disk_bytes_s = disk_mb_s * 1024 * 1024

            # Read processes
            procs = read_process_sample()
            procs.sort(key=lambda p: p["cpu"], reverse=True)
            top_procs = procs[: args.top]

            current_paths = read_path_sizes(args.watch_path)

            # Output
            if args.json:
                print(json.dumps({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "interval_seconds": args.interval_seconds,
                    "iteration": iteration,
                    "disk": {
                        "mb_per_sec": disk_mb_s,
                        "bytes_per_sec": disk_bytes_s,
                        "xfers_per_sec": disk_xfrs_s,
                    },
                    "processes_seen": len(procs),
                    "rows": [
                        {"pid": p["pid"], "name": p["name"], "cpu": p["cpu"], "rss": p["rss"]}
                        for p in top_procs
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
                    disp, top_procs, disk_mb_s, disk_xfrs_s, disk_bytes_s,
                    iteration, args.interval_seconds, args.top,
                    args.watch_path, previous_paths, current_paths,
                )
                disp.flush()

            previous_paths = current_paths

            if args.iterations > 0 and iteration >= args.iterations:
                break

    finally:
        iostat.stop()
        disp.cleanup()
        if shutdown:
            print("Interrupted.", file=sys.stderr)


if __name__ == "__main__":
    main()
