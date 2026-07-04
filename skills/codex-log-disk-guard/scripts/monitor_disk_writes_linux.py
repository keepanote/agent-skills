#!/usr/bin/env python3
import argparse
import json
import pathlib
import time


def read_process_sample():
    root = pathlib.Path("/proc")
    rows = {}
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        io_file = entry / "io"
        comm_file = entry / "comm"
        try:
            name = comm_file.read_text(encoding="utf-8", errors="replace").strip()
            write_bytes = 0
            for line in io_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("write_bytes:"):
                    write_bytes = int(line.split(":", 1)[1].strip())
                    break
            rows[pid] = {"pid": pid, "name": name, "write_bytes": write_bytes}
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
    return rows


def read_path_sizes(paths):
    out = {}
    for path in paths:
        p = pathlib.Path(path)
        out[path] = p.stat().st_size if p.exists() else None
    return out


def render_table(rows):
    print(f"{'MBWritten':>10} {'PID':>8} Process")
    for row in rows:
        mb = row["delta"] / (1024 * 1024)
        print(f"{mb:10.3f} {row['pid']:8d} {row['name']}")


def render_path_table(before, after, paths):
    print("\nWatched paths")
    print(f"{'Delta':>12} Path")
    for path in paths:
        prev = before.get(path)
        curr = after.get(path)
        delta = curr - prev if prev is not None and curr is not None else None
        delta_text = str(delta) if delta is not None else "n/a"
        print(f"{delta_text:>12} {path}")


def main():
    parser = argparse.ArgumentParser(description="Monitor Linux per-process disk writes.")
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--iterations", type=int, default=0)
    parser.add_argument("--watch-path", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    previous = read_process_sample()
    previous_paths = read_path_sizes(args.watch_path)
    iteration = 0

    while True:
        time.sleep(args.interval_seconds)
        iteration += 1
        current = read_process_sample()
        rows = []
        for pid, now in current.items():
            before = previous.get(pid)
            if not before:
                continue
            delta = max(0, now["write_bytes"] - before["write_bytes"])
            rows.append(
                {
                    "pid": pid,
                    "name": now["name"],
                    "delta": delta,
                }
            )
        rows.sort(key=lambda item: item["delta"], reverse=True)
        top_rows = rows[: args.top]

        current_paths = read_path_sizes(args.watch_path)
        if args.json:
            print(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "rows": top_rows,
                        "paths": [
                            {
                                "path": path,
                                "previous": previous_paths.get(path),
                                "current": current_paths.get(path),
                                "delta": (
                                    current_paths.get(path) - previous_paths.get(path)
                                    if previous_paths.get(path) is not None
                                    and current_paths.get(path) is not None
                                    else None
                                ),
                            }
                            for path in args.watch_path
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Top {args.top} Linux process write deltas")
            render_table(top_rows)
            if args.watch_path:
                render_path_table(previous_paths, current_paths, args.watch_path)

        previous = current
        previous_paths = current_paths
        if args.iterations > 0 and iteration >= args.iterations:
            break


if __name__ == "__main__":
    main()
