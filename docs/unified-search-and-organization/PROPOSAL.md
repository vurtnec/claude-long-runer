# Unified Search, Indexing & Desktop Organization — Proposal

> A design for weaving a **single searchable memory** across a user's email, local
> files, Teams, Slack, cloud docs and calendar — plus a **safe macOS desktop
> organizer** — delivered as two Claude Cowork skills that run against the
> connectors this environment already exposes.

**Status:** Proposal / design. Two runnable skills ship alongside this document
(`skills/unified-search/`, `skills/desktop-organizer/`). Nothing in this proposal
touches user data until the user explicitly runs a skill.

---

## 1. Goals

1. **One search box over everything.** Ask a natural-language question and get
   ranked, deduplicated, *cited* results drawn from every source the user has
   connected — email, chat, files, docs, calendar.
2. **User-extensible sources.** Adding a new source (Notion, Jira, a local
   folder) is dropping in a connector, not rewriting the engine.
3. **A trustworthy desktop organizer.** Claude can tidy a messy Mac — Downloads,
   Desktop, Documents — into a clean, *optional* directory hierarchy, and it can
   never lose or silently delete a file.
4. **Local-first & privacy-respecting.** The index lives on the user's machine;
   source ACLs are honored; secrets are never indexed.

## 2. Why two skills, not one

Search/retrieval and file *mutation* have opposite risk profiles. Retrieval is
read-only and benefits from broad reach; organization moves bytes on disk and
demands a hard confirmation gate. Splitting them keeps the dangerous surface
small and independently auditable, and lets a user adopt search without ever
granting write access — and vice versa.

| Skill | Direction | Risk | Trigger examples |
|-------|-----------|------|------------------|
| `unified-search` | read-only | low | "find the budget thread with Jane", "what did we decide about the launch date" |
| `desktop-organizer` | writes files | gated | "organize my Downloads", "clean up my Desktop" |

## 3. What this environment already gives us

Phase 0 is buildable **today** — the connectors below are already present, so the
search skill can wrap them as read-only sources with zero new auth:

| Source | Tools already available | Normalized type |
|--------|-------------------------|-----------------|
| Outlook / Exchange email | `outlook_email_search` | `email` |
| Microsoft Teams chat | `chat_message_search`, `teams_list_chats` | `chat_message` |
| SharePoint / OneDrive | `sharepoint_search`, `sharepoint_folder_search` | `file` / `doc` |
| Outlook calendar | `outlook_calendar_search` | `calendar_event` |
| Slack + other chat (Macro) | `ContentSearch`, `ReadChannelMessages`, `ReadChat` | `chat_message` |
| Google Calendar | `list_events`, `search_events` | `calendar_event` |
| Local files (Cowork sandbox) | `Glob`, `Grep`, `Read` | `file` |

Cowork adds Gmail, Google Drive and Slack as first-class OAuth connectors, which
Phase 1+ can promote to native delta-sync connectors.

---

## 4. Unified search architecture

Three pillars (full spec in [`../../skills/unified-search/reference/architecture.md`](../../skills/unified-search/reference/architecture.md)):

### 4.1 A thin connector contract
Every source implements the same capability-negotiated interface — `list(cursor)`,
`fetchMetadata`, `fetchContent`, `fetchACL` — and declares what it can do
(`delta`, `contentFetch`, `acl`). The engine degrades gracefully when a
capability is missing (e.g. poll-on-query when there's no delta cursor).
Connectors register via a `connector.yaml` manifest, so users add sources without
touching the core.

### 4.2 One normalized record — `loom.doc.v1`
Everything — an email, a Slack message, a PDF, a calendar invite — maps to a
single JSON record with a stable `id`, `source`, `type`, `title`, `participants`,
`timestamps`, a deep-link `location` for citation, a `snippet`, a `content_hash`
for dedup, `tags`, and `acl`. Source-specific fields survive in `raw`. Schema:
[`../../skills/unified-search/reference/doc-schema.json`](../../skills/unified-search/reference/doc-schema.json).

### 4.3 Hybrid retrieval with live citations
- **BM25/FTS5** for precise term, name and ID matching ("invoice #4471").
- **Dense embeddings** (prefer a *local* model) for semantic recall ("that
  thread about the budget cut").
- **Reciprocal Rank Fusion** (`score = Σ 1/(k+rank)`, k≈60) merges the two;
  optional cross-encoder rerank on the top 50.
- Results are **deduplicated** across sources by `content_hash` (the same file in
  Drive + local + as an email attachment collapses to one record with an
  `also_in` list) and returned as **pointers with deep links** — the assistant
  re-fetches live content before quoting, so it never serves stale data.

### 4.4 Storage & sync
SQLite (records + FTS5) + a content-addressed blob store for extracted text + a
local vector index (sqlite-vec / LanceDB). Each `(connector, account)` keeps a
`sync_state` cursor; only changed etags trigger content fetch; tombstones
propagate deletes. Large binaries are text-extracted (Tika / pdfplumber / OCR),
never embedded raw.

### 4.5 Privacy
Local-first and encrypted at rest; OAuth tokens in the OS keychain, never in
SQLite or logs; every query filters to what the connected identity may see;
`.env`, `id_rsa`, `*.pem`, muted channels and user exclude-globs are **never
indexed**, and a redaction pass strips detected secrets before embedding.

### 4.6 Rollout
- **Phase 0 — MVP:** wrap the existing M365 / Slack / Calendar tools as read-only
  connectors; normalized schema; FTS5/BM25; unified query + citations;
  poll-on-query. Zero local-storage risk.
- **Phase 1:** persistent local index, incremental cursors, hybrid RRF ranking,
  cross-source dedup.
- **Phase 2:** native macOS FSEvents connector (text extraction/OCR) + native
  Gmail/Drive delta sync.
- **Phase 3:** publish the `SourceConnector` SDK + `connector.yaml` so users add
  their own sources.

---

## 5. Desktop organization system

The organizer is a **scan → classify → propose → confirm → execute → report**
pipeline where every phase before "execute" is read-only and "execute" is gated
on explicit user approval. Full spec:
[`../../skills/desktop-organizer/reference/taxonomy.md`](../../skills/desktop-organizer/reference/taxonomy.md).

### 5.1 Optional directory hierarchy
A sensible default tree (`~/Documents/Organized/` → Domain → Project → Year) that
the user can **fully override** by dropping an `organize.config.yaml` in the scan
root. Downloads and Desktop are staging areas drained into the tree; anything
ambiguous lands in `_Review/` with a reason. Example config:
[`../../skills/desktop-organizer/reference/organize.config.example.yaml`](../../skills/desktop-organizer/reference/organize.config.example.yaml).

### 5.2 Classification
Destination is decided by combining signals, highest-confidence first:
extension + MIME (magic bytes, not just the extension) → type bucket; filename
tokens and content sniff → domain/project; EXIF/mtime/name dates → year-month
bucket; download-origin xattr → routing hint. Screenshots, junk (`.DS_Store`,
`*.part`, zero-byte, lock files), installers and duplicates (SHA-256) are each
handled explicitly.

### 5.3 Non-negotiable safety invariants
1. **Mandatory dry-run.** A human-readable `PLAN.md` + machine `plan.json` is
   produced first; no bytes move during scan or plan.
2. **Explicit confirmation** before any move; partial approvals honored.
3. **Undo manifest.** Every move is appended to `undo-<timestamp>.json` *before*
   it commits; one command reverses everything.
4. **Never delete.** Installers/junk/duplicates are *quarantined* to `_Review/`
   (or Trash if configured); deletion is only ever a suggestion the user runs.
5. **Protected paths skipped:** `~/Library`, `/System`, `/Applications`, `.app`
   and `.photoslibrary` bundles, git repos (moved whole), `node_modules`,
   dotfiles, symlinks, `*.icloud` placeholders.
6. **Locked/open files** are detected and deferred; cross-volume moves are
   copy → verify hash → remove.

A runnable reference implementation of this pipeline (dry-run planner + executor
+ undo) ships at
[`../../skills/desktop-organizer/scripts/organize.py`](../../skills/desktop-organizer/scripts/organize.py).

---

## 6. How it maps onto this codebase

- Both skills follow the house `skills/<name>/SKILL.md` convention (YAML
  frontmatter: `name` + block-scalar `description` with trigger bullets and an
  `<example>`), matching `skills/long-runner-acceptance-test/`.
- Bundled scripts follow the `tasks/*/processor.py` precedent (a self-contained
  Python module using only stdlib + `pyyaml`, which is already a dependency).
- Connectors reuse `client.py`'s MCP loading model (`mcp__<server>__*` tool
  names, global + project resolution) — no new configuration mechanism.

## 7. Confirmed decisions

These were the three defaults recommended for the user's call; all three are
**confirmed** and are what the shipped skills and `organize.py` implement:

1. **Embeddings — local-first.** Ship with a local embedding model so content
   never leaves the machine; a remote embedder stays available as an opt-in for
   quality-sensitive workloads. (`unified-search/reference/architecture.md` §3, §5.)
2. **Organizer destination — copy tree.** Organize into `~/Documents/Organized`
   rather than restructuring `~/Documents` in place — the safest option and the
   default `organize.config.yaml` `root`. In-place remains available by changing
   `root` and `scan`. (`desktop-organizer/reference/taxonomy.md` §1.)
3. **Sources — Phase-0 set first.** Start with the connectors already present in
   this environment (Outlook email, Teams, SharePoint/OneDrive, Slack via Macro,
   Google/Outlook calendar, local files, GitHub), then promote Cowork-native
   Gmail/Drive/Slack to delta-sync connectors in Phase 1+.
   (`unified-search/reference/connectors.md`.)
