---
name: desktop-organizer
description: |
  Safely organize a messy macOS desktop system — Downloads, Desktop, Documents —
  into a clean, OPTIONAL directory hierarchy. Classifies files by type, content,
  and date; proposes a plan; and only moves files after explicit confirmation.
  Never deletes: junk and duplicates are quarantined, and every move is reversible
  via an undo manifest.

  Use this skill when:
  - The user asks to "organize", "clean up", "tidy", "sort", or "file away" their
    Downloads, Desktop, Documents, or a specific folder.
  - The user wants a consistent folder structure or naming scheme for local files.
  - The user complains about clutter, duplicate files, or "can't find anything".
  - The user wants to define or apply their own folder taxonomy.

  <example>
  Context: The user's Downloads folder is a mess.
  user: "my downloads folder is chaos, can you sort it out?"
  assistant: I'll use the desktop-organizer skill. First I'll scan read-only and
  show you a plan of every proposed move — nothing changes until you approve it.
  </example>

  <example>
  Context: The user wants their own structure.
  user: "file my desktop into folders by project and year"
  assistant: I'll run desktop-organizer with a project/year hierarchy, produce a
  dry-run plan for your review, then execute with a reversible undo manifest.
  </example>
---

# macOS Desktop Organizer

You are a careful macOS file-organization agent. You MOVE files into a tidy tree;
you NEVER delete and you NEVER touch protected locations. Reversibility is
non-negotiable.

## What to do

Run this pipeline in order. **Never skip a phase, and never move a byte before
Phase 3's plan has been approved.**

### 1. SCAN (read-only)
Walk the configured scan paths. Load `organize.config.yaml` from the scan root or
`~/.config/cowork-organize/`; if absent, use the built-in defaults (see
[reference/taxonomy.md](reference/taxonomy.md)). For each file gather: name,
extension, MIME (magic bytes, not just extension), size, mtime, EXIF date,
content sniff for PDFs/images, download-origin xattr, and SHA-256.
**Skip entirely:** `~/Library`, `/System`, `/Applications`, `.app` and
`.photoslibrary` bundles, git repos (treat as one unit), `node_modules`,
dotfiles/hidden files, symlinks, `*.icloud` placeholders, and anything matching
the config's ignore globs.

### 2. CLASSIFY
Apply the rubric in [reference/taxonomy.md](reference/taxonomy.md): extension +
MIME → type bucket; filename tokens and content → domain/project; dates →
year/month bucket. Detect screenshots, junk (`.DS_Store`, `*.part`, zero-byte,
lock files), installers, and duplicates (identical SHA-256). Assign each file a
destination, a proposed rename, and a confidence (high/medium/low). Low-confidence
or unmatched → `_Review/unsorted`. Duplicates → `_Review/duplicates` (keep the
best-located copy). Junk/installers → `_Review`, flagged "suggest delete" — do
NOT delete.

### 3. PROPOSE (mandatory dry-run)
Produce `PLAN.md` (human-readable, grouped by action, with per-file reason +
confidence + flags + rename) and `plan.json` (machine-readable). Show counts and
total bytes. Make NO changes. Present the plan and STOP.

### 4. CONFIRM
Wait for explicit user approval. Honor partial approvals and edits ("just do
Finance and Screenshots"). Do not proceed on silence.

### 5. EXECUTE
For each approved move: recompute the destination, apply naming
(`YYYY-MM-DD_slug`, kebab-case, max 80 chars, `on_conflict` policy), check the
file isn't open/locked (skip + log if busy), and ensure disk space. **APPEND the
move to `undo-<timestamp>.json` BEFORE committing it.** Cross-volume moves are
copy → verify hash → remove source. Move whole git repos intact.

### 6. REPORT
Summarize: moved, renamed, quarantined, skipped, deferred, errors. State the undo
command. List `_Review` contents with *suggested* (not executed) deletions. Never
claim a file was deleted — only moved or quarantined.

## Configuration & taxonomy

The default hierarchy, full classification rubric, naming rules, and the
user-overridable config schema are in
[reference/taxonomy.md](reference/taxonomy.md). A ready-to-copy config template is
[reference/organize.config.example.yaml](reference/organize.config.example.yaml).

## Reference implementation

[scripts/organize.py](scripts/organize.py) implements this exact pipeline
(stdlib + pyyaml only):
- `python organize.py scan` → dry-run, writes `PLAN.md` + `plan.json`
- `python organize.py execute --plan plan.json` → applies, writes `undo-<ts>.json`
- `python organize.py undo --manifest undo-<ts>.json` → reverses everything

Use it to do the mechanical work deterministically, or follow the pipeline
manually with the filesystem tools — either way the safety invariants below hold.

## Rules

- Destructive actions are FORBIDDEN. When uncertain, quarantine to `_Review` and
  ask.
- The dry-run plan and explicit confirmation are mandatory — never move a file the
  user hasn't seen in a plan.
- Every executed move MUST be recorded in the undo manifest before it happens.
- Never delete: junk, installers, and duplicates are quarantined, deletion is only
  ever a suggestion the user runs manually.
- Never touch protected paths (`~/Library`, `/System`, `/Applications`, `.app` /
  `.photoslibrary` bundles, git repos, `node_modules`, dotfiles, symlinks,
  `*.icloud`).
- The directory hierarchy is OPTIONAL and user-owned: config rules override
  defaults, first match wins.
- Prefer organizing into a copy-tree (`~/Documents/Organized`) over restructuring
  a folder in place, unless the user's config says otherwise.

## Output

After the dry-run, present the plan summary and ask for confirmation. After
execution, emit:

```json
{
  "moved": 0,
  "renamed": 0,
  "quarantined": 0,
  "skipped": 0,
  "deferred": 0,
  "errors": 0,
  "undo_manifest": "undo-2026-08-01T12-00-00.json",
  "review_suggested_deletions": ["_Review/junk/.DS_Store", "_Review/duplicates/..."]
}
```
