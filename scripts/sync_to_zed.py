#!/usr/bin/env python3
"""
Sync Claude sessions in ~/.claude/projects/ into Zed's sidebar_threads table
so Feishu-bot / claude CLI sessions show up in Zed's agent panel sidebar.

Usage:
    Cmd+Q Zed
    python scripts/sync_to_zed.py [--project PATTERN ...] [--dry-run]
    open -a Zed

Examples:
    python scripts/sync_to_zed.py                         # all projects
    python scripts/sync_to_zed.py -p claude-long-runner   # one project
    python scripts/sync_to_zed.py -p filmmeter -p fanye   # multiple
    python scripts/sync_to_zed.py --dry-run               # preview, no write

Idempotent and incremental: only inserts session_ids not already in
sidebar_threads. Auto-backs up the Zed db, keeps last 5 backups.
"""
import argparse
import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

# ─── User config ────────────────────────────────────────────────────────────
# Project list is loaded from `scripts/sync_to_zed.config.json` (gitignored).
# Copy `sync_to_zed.config.example.json` to `sync_to_zed.config.json` and
# edit it. If the file is missing or its `projects` list is empty, ALL
# projects under ~/.claude/projects/ are synced.
SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG_PATH = SCRIPT_DIR / "sync_to_zed.config.json"
# ────────────────────────────────────────────────────────────────────────────

ZED_DB = Path.home() / "Library/Application Support/Zed/db/0-stable/db.sqlite"
ZED_DB_DIR = ZED_DB.parent
CLAUDE_PROJECTS = Path.home() / ".claude/projects"
BACKUP_BASE = Path.home()
KEEP_BACKUPS = 5
TITLE_MAX = 200
SCAN_LINES_PER_FILE = 200  # enough to find first user message + cwd in any practical session


def is_zed_running() -> bool:
    try:
        out = subprocess.run(
            ["lsof", str(ZED_DB)],
            capture_output=True, text=True, check=False,
        ).stdout
        return any(line.strip() for line in out.splitlines()[1:])
    except FileNotFoundError:
        out = subprocess.run(
            ["pgrep", "-x", "zed"],
            capture_output=True, text=True, check=False,
        ).stdout
        return bool(out.strip())


def backup_db() -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUP_BASE / f".zed_db_backup_{ts}"
    backup_dir.mkdir()
    for name in ("db.sqlite", "db.sqlite-wal", "db.sqlite-shm"):
        src = ZED_DB_DIR / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)
    backups = sorted(BACKUP_BASE.glob(".zed_db_backup_*"), reverse=True)
    for old in backups[KEEP_BACKUPS:]:
        shutil.rmtree(old, ignore_errors=True)
    return backup_dir


def parse_session(jsonl_path: Path) -> dict | None:
    """Extract session_id, cwd, title, mtime from a Claude jsonl file.

    Returns None if the file is empty / sidechain / lacks both cwd and any
    user message we can use as title.
    """
    session_id = jsonl_path.stem
    cwd: str | None = None
    title: str | None = None

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for i, raw in enumerate(f):
                if i >= SCAN_LINES_PER_FILE:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if rec.get("isSidechain") is True:
                    return None

                if cwd is None:
                    c = rec.get("cwd")
                    if isinstance(c, str) and c:
                        cwd = c

                if title is None:
                    if rec.get("type") == "user":
                        msg = rec.get("message") or {}
                        content = msg.get("content")
                        if isinstance(content, str) and content.strip():
                            title = content.strip()
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    txt = (part.get("text") or "").strip()
                                    if txt:
                                        title = txt
                                        break
                    elif rec.get("type") == "last-prompt":
                        lp = rec.get("lastPrompt")
                        if isinstance(lp, str) and lp.strip():
                            title = lp.strip()

                if cwd and title:
                    break
    except OSError:
        return None

    if not cwd or not title:
        return None

    title = " ".join(title.split())  # collapse whitespace/newlines
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX] + "…"

    return {
        "session_id": session_id,
        "cwd": cwd,
        "title": title,
        "mtime": jsonl_path.stat().st_mtime,
    }


def scan_all_sessions() -> list[dict]:
    if not CLAUDE_PROJECTS.exists():
        return []
    out: list[dict] = []
    for project_dir in CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            meta = parse_session(jsonl)
            if meta:
                out.append(meta)
    return out


def load_local_projects() -> list[str]:
    """Read project list from the local (gitignored) config file. Returns [] if absent or invalid."""
    if not LOCAL_CONFIG_PATH.exists():
        return []
    try:
        with open(LOCAL_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"warning: could not read {LOCAL_CONFIG_PATH.name}: {e}", file=sys.stderr)
        return []
    projects = data.get("projects", [])
    if not isinstance(projects, list) or not all(isinstance(p, str) for p in projects):
        print(f"warning: {LOCAL_CONFIG_PATH.name} 'projects' must be a list of strings", file=sys.stderr)
        return []
    return projects


def normalize_path(p: str) -> str:
    """Expand ~ and strip trailing slash. No resolve()—we don't require it to exist on disk."""
    return str(Path(p).expanduser()).rstrip("/")


def matches_patterns(cwd: str, patterns: list[str]) -> bool:
    """Match cwd against patterns. Two modes:
      - pattern looks like a path (contains / or starts with ~) → exact path match
      - otherwise → basename exact match (case-insensitive, for typing convenience on CLI)
    """
    if not patterns:
        return True
    cwd_norm = normalize_path(cwd)
    cwd_basename = Path(cwd_norm).name
    for pat in patterns:
        if "/" in pat or pat.startswith("~"):
            if normalize_path(pat) == cwd_norm:
                return True
        else:
            if pat.lower() == cwd_basename.lower():
                return True
    return False


def print_breakdown(sessions: list[dict], header: str) -> None:
    by_project: dict[str, int] = {}
    for s in sessions:
        by_project[s["cwd"]] = by_project.get(s["cwd"], 0) + 1
    print(f"\n{header}: {len(sessions)} sessions across {len(by_project)} projects")
    for cwd, n in sorted(by_project.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {cwd}")


def sync(projects: list[str] | None = None, dry_run: bool = False) -> int:
    if not ZED_DB.exists():
        print(f"refusing: Zed db not found at {ZED_DB}")
        return 1

    if not dry_run and is_zed_running():
        print("refusing: Zed is running. Cmd+Q Zed first.")
        return 1

    print(f"scanning {CLAUDE_PROJECTS}/ ...")
    all_sessions = scan_all_sessions()
    print_breakdown(all_sessions, "found")

    if projects:
        sessions = [s for s in all_sessions if matches_patterns(s["cwd"], projects)]
        print_breakdown(sessions, f"after filter {projects}")
        if not sessions:
            print("\nno sessions match the given --project filter(s).")
            return 0
    else:
        sessions = all_sessions

    con = sqlite3.connect(str(ZED_DB), timeout=5.0)
    con.row_factory = sqlite3.Row
    existing = {
        row["session_id"]
        for row in con.execute(
            "SELECT session_id FROM sidebar_threads WHERE session_id IS NOT NULL"
        )
    }
    to_insert = [s for s in sessions if s["session_id"] not in existing]
    skipped = len(sessions) - len(to_insert)
    print(f"\n  {skipped} already in sidebar_threads (will skip)")
    print(f"  {len(to_insert)} to insert")

    if not to_insert:
        con.close()
        return 0

    if dry_run:
        print("\n[dry-run] would insert:")
        for s in to_insert:
            print(f"  {s['session_id']}  cwd={s['cwd']}")
            print(f"    title={s['title'][:80]}")
        con.close()
        return 0

    backup = backup_db()
    print(f"\nbackup at {backup}")

    inserted = 0
    failed = 0
    for s in to_insert:
        thread_id = uuid.uuid4().bytes
        ts_iso = dt.datetime.fromtimestamp(s["mtime"], dt.timezone.utc).isoformat(
            timespec="microseconds"
        )
        try:
            con.execute(
                """
                INSERT INTO sidebar_threads (
                    thread_id, session_id, agent_id, title,
                    updated_at, created_at,
                    folder_paths, folder_paths_order,
                    archived,
                    main_worktree_paths, main_worktree_paths_order,
                    remote_connection, interacted_at
                ) VALUES (?, ?, 'claude-acp', ?, ?, ?, ?, '0', 0, ?, '0', NULL, ?)
                """,
                (
                    thread_id,
                    s["session_id"],
                    s["title"],
                    ts_iso,
                    ts_iso,
                    s["cwd"],
                    s["cwd"],
                    ts_iso,
                ),
            )
            inserted += 1
        except sqlite3.Error as e:
            print(f"  insert failed for {s['session_id']}: {e}")
            failed += 1

    con.commit()
    con.close()

    print(f"\n✓ inserted {inserted} (failed {failed}, skipped {skipped} existing)")
    print(f"  rollback if needed: cp -p {backup}/db.sqlite* '{ZED_DB_DIR}/'")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Sync ~/.claude/projects/ sessions into Zed's sidebar_threads.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Matching: a pattern containing '/' or starting with '~' is an "
            "exact path match; otherwise it's an exact basename match "
            "(case-insensitive). Multiple --project args are OR-combined."
        ),
    )
    p.add_argument(
        "--project", "-p",
        action="append",
        metavar="PATH_OR_NAME",
        help="Only sync the given project. Accepts a full path "
             "(e.g. ~/development/.../foo) or a bare directory name "
             "(e.g. foo). Repeatable. Overrides DEFAULT_PROJECTS.",
    )
    p.add_argument(
        "--all", "-a",
        action="store_true",
        help="Sync ALL projects, ignoring DEFAULT_PROJECTS.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without writing (Zed-running check skipped).",
    )
    args = p.parse_args()

    if args.all and args.project:
        p.error("--all and --project are mutually exclusive")

    if args.all:
        projects = None
    elif args.project:
        projects = args.project
    else:
        projects = load_local_projects() or None

    return sync(projects=projects, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
