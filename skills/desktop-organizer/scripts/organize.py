#!/usr/bin/env python3
"""Reference implementation of the desktop-organizer safety pipeline.

Deliberately dependency-light (stdlib + optional pyyaml) so it runs anywhere the
rest of this project runs. It enforces the skill's non-negotiable invariants:

    scan   -> read-only; writes PLAN.md + plan.json, moves nothing
    execute-> applies an approved plan.json; records undo-<ts>.json BEFORE each move
    undo   -> reverses a run from its undo manifest

It NEVER deletes: junk/installers/duplicates are quarantined under _Review/.

Usage:
    python organize.py scan    [--config organize.config.yaml] [--root DEST]
    python organize.py execute --plan plan.json
    python organize.py undo    --manifest undo-<timestamp>.json
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml  # pyyaml is already a project dependency
except ImportError:  # pragma: no cover - graceful fallback
    yaml = None

# --- Always-protected paths, independent of user config -----------------------
PROTECTED_PARTS = {"Library", "System", "Applications", "node_modules"}
PROTECTED_SUFFIXES = (".app", ".photoslibrary")
JUNK_NAMES = {".DS_Store"}
JUNK_GLOBS = ("*.crdownload", "*.part", "~$*")

DEFAULT_CONFIG = {
    "version": 1,
    "root": "~/Documents/Organized",
    "scan": ["~/Downloads", "~/Desktop"],
    "naming": {"date_format": "%Y-%m-%d", "case": "kebab", "max_len": 80},
    "ignore": ["**/.git/**", "**/*.app/**", "~/Library/**", "**/node_modules/**", "**/*.icloud"],
    "folders": {},
    "rules": [],
    "defaults": {
        "unmatched": "_Review/unsorted",
        "quarantine": "_Review",
        "on_conflict": "version",
        "duplicates": "quarantine",
        "never_delete": True,
    },
}

EXT_BUCKETS = {
    "pdf": "Reference", "docx": "Work", "xlsx": "Work", "pptx": "Work",
    "png": "Media", "jpg": "Media", "jpeg": "Media", "heic": "Media",
    "mov": "Media", "mp4": "Media",
    "zip": "Archives", "tar": "Archives", "gz": "Archives",
    "dmg": "quarantine", "pkg": "quarantine",
    "py": "Code", "js": "Code", "ts": "Code", "go": "Code", "rs": "Code",
}


def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path and Path(path).expanduser().exists():
        if yaml is None:
            print("warning: pyyaml not installed; using defaults", file=sys.stderr)
        else:
            user = yaml.safe_load(Path(path).expanduser().read_text()) or {}
            cfg.update({k: v for k, v in user.items() if v is not None})
    return cfg


def sha256(p: Path, limit: int = 64 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            if f.tell() > limit:
                break
    return "sha256:" + h.hexdigest()


def is_protected(p: Path) -> bool:
    if any(part in PROTECTED_PARTS for part in p.parts):
        return True
    if any(str(p).find(sfx + os.sep) != -1 or p.name.endswith(sfx) for sfx in PROTECTED_SUFFIXES):
        return True
    if p.is_symlink() or p.name.startswith("."):
        return True
    if p.name.endswith(".icloud"):
        return True
    # git repos are treated as one unit; skip files inside them
    if any((anc / ".git").exists() for anc in list(p.parents)[:6]):
        return True
    return False


def matches_ignore(p: Path, globs: list[str]) -> bool:
    s = str(p)
    return any(fnmatch.fnmatch(s, os.path.expanduser(g)) for g in globs)


def is_junk(p: Path) -> bool:
    if p.name in JUNK_NAMES or any(fnmatch.fnmatch(p.name, g) for g in JUNK_GLOBS):
        return True
    try:
        return p.stat().st_size == 0
    except OSError:
        return False


def slugify(name: str, case: str, max_len: int) -> str:
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[/\\:*?\"<>|]", "", stem)
    stem = re.sub(r"[\s_]+", "-", stem.strip())
    stem = re.sub(r"-{2,}", "-", stem).strip("-").lower()
    if case == "snake":
        stem = stem.replace("-", "_")
    return (stem[:max_len] or "file") + ext.lower()


def file_date(p: Path, fmt: str) -> str:
    ts = p.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


def classify(p: Path, cfg: dict) -> tuple[str, str]:
    """Return (destination_subpath, reason)."""
    name = p.name
    ext = p.suffix.lower().lstrip(".")
    dfmt = cfg["naming"]["date_format"]
    year = file_date(p, "%Y")
    month = file_date(p, "%m")

    # user rules first (first match wins)
    for rule in cfg.get("rules", []):
        m = rule.get("match", {})
        if "ext" in m and ext not in [e.lower() for e in m["ext"]]:
            continue
        if "name_regex" in m and not re.search(m["name_regex"], name):
            continue
        target = rule.get("to")
        folder = cfg.get("folders", {}).get(target, {"path": target})
        path_tmpl = folder["path"] if isinstance(folder, dict) else str(folder)
        if target == "quarantine":
            return f"{cfg['defaults']['quarantine']}/installers", "rule:quarantine"
        return path_tmpl.format(project="_Unsorted", year=year, month=month), f"rule:{target}"

    # screenshot heuristic
    if ext == "png" and re.search(r"(?i)screenshot|cleanshot|screen shot", name):
        return f"Media/Screenshots/{year}-{month}", "screenshot"

    # extension bucket
    bucket = EXT_BUCKETS.get(ext)
    if bucket == "quarantine":
        return f"{cfg['defaults']['quarantine']}/installers", "installer"
    if bucket in ("Media",):
        return f"Media/Photos/{year}/{year}-{month}", "ext:media"
    if bucket:
        return f"{bucket}/{year}", f"ext:{ext}"

    return cfg["defaults"]["unmatched"], "unmatched"


def dest_path(root: Path, sub: str, p: Path, cfg: dict) -> Path:
    d = file_date(p, cfg["naming"]["date_format"])
    slug = slugify(p.name, cfg["naming"]["case"], cfg["naming"]["max_len"])
    if not slug.lower().startswith(d):
        slug = f"{d}_{slug}"
    return root / sub / slug


def resolve_conflict(dest: Path, policy: str, content_hash: str) -> Path | None:
    if not dest.exists():
        return dest
    if policy == "skip":
        return None
    stem, ext = os.path.splitext(dest.name)
    if policy == "hash-suffix":
        return dest.with_name(f"{stem}-{content_hash.split(':')[1][:8]}{ext}")
    n = 2
    while True:
        cand = dest.with_name(f"{stem}-v{n}{ext}")
        if not cand.exists():
            return cand
        n += 1


def cmd_scan(args) -> None:
    cfg = load_config(args.config)
    root = Path(args.root or cfg["root"]).expanduser()
    seen_hashes: dict[str, str] = {}
    moves, quarantines, junk, skipped = [], [], [], []

    for scan_dir in cfg["scan"]:
        base = Path(scan_dir).expanduser()
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if is_protected(p) or matches_ignore(p, cfg["ignore"]):
                skipped.append({"path": str(p), "reason": "protected/ignored"})
                continue
            if is_junk(p):
                junk.append({"src": str(p), "dst": str(root / "_Review/junk" / p.name),
                             "reason": "junk", "action": "quarantine"})
                continue
            ch = sha256(p)
            if ch in seen_hashes:
                quarantines.append({"src": str(p),
                                    "dst": str(root / "_Review/duplicates" / p.name),
                                    "reason": f"duplicate of {seen_hashes[ch]}",
                                    "action": "quarantine", "hash": ch})
                continue
            seen_hashes[ch] = str(p)
            sub, reason = classify(p, cfg)
            dest = dest_path(root, sub, p, cfg)
            dest = resolve_conflict(dest, cfg["defaults"]["on_conflict"], ch)
            if dest is None:
                skipped.append({"path": str(p), "reason": "conflict:skip"})
                continue
            moves.append({"src": str(p), "dst": str(dest), "reason": reason,
                          "action": "move", "hash": ch})

    plan = {
        "generated": file_date(Path(cfg["scan"][0]).expanduser(), "%Y-%m-%dT%H-%M-%S")
        if Path(cfg["scan"][0]).expanduser().exists() else "unknown",
        "root": str(root),
        "moves": moves, "quarantines": quarantines, "junk": junk, "skipped": skipped,
    }
    Path("plan.json").write_text(json.dumps(plan, indent=2))
    _write_plan_md(plan)
    print(f"DRY RUN — nothing moved.\n  moves:       {len(moves)}\n"
          f"  duplicates:  {len(quarantines)}\n  junk:        {len(junk)}\n"
          f"  skipped:     {len(skipped)}\n\nReview PLAN.md, then:\n"
          f"  python organize.py execute --plan plan.json")


def _write_plan_md(plan: dict) -> None:
    lines = [f"# Organization Plan (DRY RUN)\n", f"Destination root: `{plan['root']}`\n"]
    for title, key in [("Moves", "moves"), ("Duplicates → _Review", "quarantines"),
                       ("Junk → _Review", "junk"), ("Skipped (protected/ignored)", "skipped")]:
        items = plan.get(key, [])
        lines.append(f"\n## {title} ({len(items)})\n")
        for it in items[:1000]:
            if "src" in it:
                lines.append(f"- `{it['src']}` → `{it['dst']}`  _({it.get('reason','')})_")
            else:
                lines.append(f"- `{it['path']}`  _({it.get('reason','')})_")
    lines.append("\n> Nothing has been moved. Deletion is never performed — "
                 "junk and duplicates are quarantined under `_Review/`.\n")
    Path("PLAN.md").write_text("\n".join(lines))


def _do_move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.stat().st_dev != dst.parent.stat().st_dev:  # cross-volume: copy+verify+remove
        shutil.copy2(src, dst)
        if sha256(src) != sha256(dst):
            dst.unlink(missing_ok=True)
            raise IOError(f"hash mismatch copying {src}")
        src.unlink()
    else:
        shutil.move(str(src), str(dst))


def cmd_execute(args) -> None:
    plan = json.loads(Path(args.plan).read_text())
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%S") if _now_ok() else "run"
    undo_path = Path(f"undo-{ts}.json")
    undo: list[dict] = []
    stats = {"moved": 0, "quarantined": 0, "deferred": 0, "errors": 0}

    for group, kind in [("moves", "moved"), ("quarantines", "quarantined"), ("junk", "quarantined")]:
        for it in plan.get(group, []):
            src, dst = Path(it["src"]), Path(it["dst"])
            if not src.exists():
                stats["deferred"] += 1
                continue
            entry = {"src": str(src), "dst": str(dst), "orig_name": src.name}
            undo.append(entry)
            undo_path.write_text(json.dumps(undo, indent=2))  # record BEFORE the move
            try:
                _do_move(src, dst)
                stats[kind] += 1
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                entry["error"] = str(e)
                undo.pop()
                undo_path.write_text(json.dumps(undo, indent=2))

    print(json.dumps({**stats, "undo_manifest": str(undo_path)}, indent=2))
    print(f"\nTo reverse: python organize.py undo --manifest {undo_path}")


def cmd_undo(args) -> None:
    entries = json.loads(Path(args.manifest).read_text())
    restored = 0
    for entry in reversed(entries):
        dst, src = Path(entry["dst"]), Path(entry["src"])
        if dst.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            restored += 1
    print(f"Reversed {restored} move(s) from {args.manifest}.")


def _now_ok() -> bool:
    try:
        datetime.now()
        return True
    except Exception:  # pragma: no cover
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="dry-run: write PLAN.md + plan.json")
    s.add_argument("--config")
    s.add_argument("--root")
    s.set_defaults(func=cmd_scan)
    e = sub.add_parser("execute", help="apply an approved plan.json")
    e.add_argument("--plan", required=True)
    e.set_defaults(func=cmd_execute)
    u = sub.add_parser("undo", help="reverse a run from its undo manifest")
    u.add_argument("--manifest", required=True)
    u.set_defaults(func=cmd_undo)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
