#!/usr/bin/env python3
import argparse
import json
import pathlib
import sqlite3
import sys
import time


TRIGGER_NAME = "codex_block_logs_insert"


def default_db_path() -> pathlib.Path:
    home = pathlib.Path.home()
    candidates = [
        home / ".codex" / "logs_2.sqlite",
        home / ".codex" / "logs.sqlite",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def connect(path: pathlib.Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con = sqlite3.connect(str(path), timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    return con


def wal_path(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(f"{path}-wal")


def file_size(path: pathlib.Path) -> int:
    return path.stat().st_size if path.exists() else 0


def inspect_db(path: pathlib.Path) -> dict:
    con = connect(path, readonly=True)
    cur = con.cursor()
    result = {
        "db_path": str(path),
        "db_size": file_size(path),
        "wal_size": file_size(wal_path(path)),
        "page_size": cur.execute("PRAGMA page_size").fetchone()[0],
        "page_count": cur.execute("PRAGMA page_count").fetchone()[0],
        "freelist_count": cur.execute("PRAGMA freelist_count").fetchone()[0],
        "journal_mode": cur.execute("PRAGMA journal_mode").fetchone()[0],
        "max_id": cur.execute("SELECT MAX(id) FROM logs").fetchone()[0],
        "count": cur.execute("SELECT COUNT(*) FROM logs").fetchone()[0],
        "sum_estimated_bytes": cur.execute(
            "SELECT COALESCE(SUM(estimated_bytes), 0) FROM logs"
        ).fetchone()[0],
        "levels": cur.execute(
            "SELECT level, COUNT(*), COALESCE(SUM(estimated_bytes),0) "
            "FROM logs GROUP BY level ORDER BY 2 DESC"
        ).fetchall(),
        "top_targets": cur.execute(
            "SELECT level, target, COUNT(*) c, COALESCE(SUM(estimated_bytes),0) b "
            "FROM logs GROUP BY level, target ORDER BY b DESC LIMIT 15"
        ).fetchall(),
        "triggers": cur.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='logs' ORDER BY name"
        ).fetchall(),
    }
    con.close()
    return result


def backup_db(path: pathlib.Path) -> pathlib.Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    src = connect(path)
    dst = sqlite3.connect(str(backup), timeout=60)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return backup


def ensure_trigger(path: pathlib.Path):
    con = connect(path)
    cur = con.cursor()
    existing = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (TRIGGER_NAME,),
    ).fetchone()
    created = False
    if not existing:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            f"""
            CREATE TRIGGER {TRIGGER_NAME}
            BEFORE INSERT ON logs
            BEGIN
              SELECT RAISE(IGNORE);
            END;
            """
        )
        con.commit()
        created = True
    checkpoint = cur.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    con.close()
    return created, checkpoint


def sample_stability(path: pathlib.Path, seconds: int, interval: float = 2.0):
    samples = []
    end = time.time() + seconds
    while True:
        con = connect(path, readonly=True)
        cur = con.cursor()
        sample = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "max_id": cur.execute("SELECT MAX(id) FROM logs").fetchone()[0],
            "count": cur.execute("SELECT COUNT(*) FROM logs").fetchone()[0],
            "wal_size": file_size(wal_path(path)),
        }
        con.close()
        samples.append(sample)
        if time.time() >= end:
            break
        time.sleep(interval)
    return samples


def do_guard(path: pathlib.Path, sample_seconds: int) -> dict:
    backup = backup_db(path)
    created, checkpoint = ensure_trigger(path)
    samples = sample_stability(path, sample_seconds)
    return {
        "backup": str(backup),
        "trigger_created": created,
        "checkpoint": checkpoint,
        "samples": samples,
    }


def do_vacuum(path: pathlib.Path) -> dict:
    before = file_size(path)
    con = connect(path)
    con.execute("VACUUM")
    con.close()
    after = file_size(path)
    return {"before": before, "after": after}


def do_unblock(path: pathlib.Path) -> dict:
    con = connect(path)
    con.execute(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME}")
    con.commit()
    checkpoint = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    con.close()
    return {"checkpoint": checkpoint}


def main() -> int:
    parser = argparse.ArgumentParser(description="Guard and compact Codex SQLite log databases.")
    parser.add_argument(
        "command",
        choices=["inspect", "guard", "vacuum", "unblock"],
        help="Action to run",
    )
    parser.add_argument("--db", type=pathlib.Path, default=default_db_path())
    parser.add_argument("--sample-seconds", type=int, default=8)
    args = parser.parse_args()

    if not args.db.exists():
        print(json.dumps({"error": f"database not found: {args.db}"}))
        return 1

    if args.command == "inspect":
        print(json.dumps(inspect_db(args.db), ensure_ascii=False, indent=2))
        return 0
    if args.command == "guard":
        print(json.dumps(do_guard(args.db, args.sample_seconds), ensure_ascii=False, indent=2))
        return 0
    if args.command == "vacuum":
        print(json.dumps(do_vacuum(args.db), ensure_ascii=False, indent=2))
        return 0
    if args.command == "unblock":
        print(json.dumps(do_unblock(args.db), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
