# Desktop Organizer — Taxonomy, Rubric & Naming

## 1. Default hierarchy (a sensible default, fully overridable)

The default root is `~/Documents/Organized/` — the organizer never restructures
`~/Documents` in place unless the config says so. Pattern: **Domain → Project /
Category → Year → files**. Downloads and Desktop are *staging areas* drained into
this tree.

```
~/Documents/Organized/
├── Work/
│   ├── acme-corp/2026/2026-07-14_q3-budget.xlsx
│   └── _Unsorted/                      # domain known, project unknown
├── Personal/
│   ├── finance/2026/2026-06-01_bank-statement.pdf
│   ├── health/2026/
│   └── travel/2026/
├── Media/
│   ├── Photos/2026/2026-07/
│   ├── Screenshots/2026-07/
│   └── Video/
├── Code/
│   └── <repo-name>/                    # left intact if it's a git repo
├── Reference/                          # ebooks, manuals, papers
├── Archives/                           # zips/tars
├── Installers/                         # .dmg/.pkg → quarantine, suggest delete
└── _Review/                            # ambiguous / duplicates / junk — NEVER auto-deleted
    ├── unsorted/
    ├── duplicates/
    └── junk/
```

Anything unclassifiable lands in `_Review/unsorted/` with a reason note.

## 2. Classification rubric

Decide the destination by combining signals, highest-confidence first:

| Signal | Use |
|--------|-----|
| **Extension + MIME** | Primary type bucket. `.pdf`→docs, `.png/.jpg/.heic`→images, `.mov/.mp4`→video, `.zip/.tar/.gz`→archives, `.dmg/.pkg/.app`→installers, `.py/.js/.ts`→code, `.docx/.xlsx/.pptx`→documents. Read magic bytes — do not trust the extension. |
| **Content sniff** | PDF text scan → "Invoice/Statement/Boarding pass" → finance/travel. Light image inspection → screenshot vs. photo. |
| **Filename tokens** | Regex for `invoice`, `receipt`, `resume`, `screenshot`, project codes, client names → domain/project. |
| **Dates** | EXIF DateTaken (photos) > filesystem mtime > date parsed from name. Drives the `/YYYY/` and `YYYY-MM-DD` buckets. |
| **Source (optional)** | Download origin (`kMDItemWhereFroms` xattr), AirDrop/Slack tags → routing hints. |

**Screenshots:** name matches `Screenshot* / CleanShot* / Screen Shot*`, or
dir=Desktop + PNG + no EXIF → `Media/Screenshots/YYYY-MM/`.

**Desktop clutter:** loose files >7 days old on the Desktop are candidates;
app aliases / `.app` / in-use folders are skipped.

**Junk:** `.DS_Store`, `*.crdownload`, `*.part`, zero-byte files, `~$*` lock
files → `_Review/junk/` (never deleted).

**Duplicates:** SHA-256 of contents. Exact match → keep the copy in the best
location, move the others to `_Review/duplicates/` with a pointer to the kept
copy. Near-dupes (same name, differing size) are flagged, never auto-removed.

## 3. Naming conventions

- **Date prefix when meaningful:** `YYYY-MM-DD_<slug>.<ext>` (e.g.
  `2026-07-14_q3-budget.xlsx`). Photos use their EXIF date.
- **Slug:** lowercase; spaces/underscores → `-`; strip diacritics and unsafe
  chars `/\:*?"<>|`; collapse repeats; trim to `max_len`; keep the (lowercased)
  original extension.
- **Never destroy the original name** — it is stored in the undo manifest so every
  rename is reversible.
- **Collisions (`on_conflict`):** `version` → append `-v2`, `-v3`; `hash-suffix` →
  append the first 8 of the content hash; `skip` → leave in place and log. The
  collision check is case-insensitive (APFS is case-insensitive by default).

## 4. Protected / skipped paths (always)

`~/Library`, `/System`, `/Applications`, `.app` bundles, `.photoslibrary`, git
repositories (moved whole, never split), `node_modules`, dotfiles/hidden files,
symlinks, and `*.icloud` placeholders (not yet downloaded). Locked/open files are
detected (`lsof`/flock) and deferred with a note.

## 5. Safety invariants (restated — non-negotiable)

1. Read-only scan; mandatory dry-run plan (`PLAN.md` + `plan.json`) before any
   move.
2. Explicit confirmation required; partial approval supported.
3. Every move appended to `undo-<timestamp>.json` *before* it commits; an `undo`
   run replays it in reverse.
4. Never delete — quarantine to `_Review/` (or macOS Trash if configured);
   deletion is only ever a suggestion.
5. Cross-volume moves = copy → verify hash → remove source. Verify free disk
   space first.
