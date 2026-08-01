# Unified Search — Architecture Reference

Read this when *building or extending* the index (adding a source, designing
storage, changing ranking) — not for a routine query.

## 1. Source-connector abstraction

Every source implements one capability-negotiated contract. The engine knows
nothing source-specific beyond it. Connectors register via a `connector.yaml`
manifest (`id`, auth type, required scopes, capability flags).

```typescript
interface SourceConnector {
  id: string;                    // "outlook", "slack", "sharepoint", "local-fs"
  capabilities: {
    delta: boolean;              // supports incremental cursor
    contentFetch: boolean;
    acl: boolean;
  };

  authenticate(ctx: AuthContext): Promise<ConnState>;   // scoped token/handle

  // Enumerate items changed since cursor (full scan if cursor null)
  list(cursor: string | null, opts: { pageSize: number })
      : AsyncIterator<{ items: ItemRef[]; nextCursor: string; done: boolean }>;

  fetchMetadata(ref: ItemRef): Promise<RawMetadata>;
  fetchContent(ref: ItemRef): Promise<{ mime: string; bytes?: Uint8Array; text?: string }>;
  fetchACL(ref: ItemRef): Promise<ACL>;    // optional if !capabilities.acl
}

type ItemRef = { sourceId: string; nativeId: string; etag: string;
                 changeType: "upsert" | "delete" };
```

Cursors are opaque per-connector strings: Outlook/Graph `deltaLink`, Gmail
`historyId`, Drive `pageToken`, Slack `oldest` ts, local-fs FSEvents id / mtime
watermark. When a capability is missing, degrade: no `delta` → poll-on-query with
short TTL; no `acl` → treat as private-to-user.

## 2. Normalized record — `loom.doc.v1`

Everything maps to one record; source-specific fields survive in `raw`. Formal
schema: `doc-schema.json`.

Key fields: `id` = `sha256(source_id + native_id)`; `content_hash` =
`sha256(normalized_text)` (dedup key); `location.url` = deep link for citation;
`acl.principals` = who may see it. See the schema file for the complete shape and
an example record.

## 3. Indexing strategy

**Stored locally:**
- Normalized records → SQLite (`documents` table + FTS5 for BM25).
- Extracted plain text → content-addressed blob store (never store original
  binaries unless the source is already local).
- Embeddings → local vector index (sqlite-vec / LanceDB), one vector per
  ~512-token chunk.

**Incremental sync.** Each `(connector, account)` keeps a `sync_state` row
`{cursor, last_full_scan, watermark}`. A scheduler polls per source (webhook/push
where available — Gmail push, Graph subscriptions). `list()` yields
upserts/deletes; only changed etags trigger `fetchContent`. Tombstones propagate
deletes to all three stores.

**Dedup across sources.** Two levels:
1. *Exact* — `content_hash` collapses the same content (file in Drive + local +
   email attachment) into one canonical record with `aliases[]` / `also_in`.
2. *Near* — MinHash/SimHash buckets catch forwarded emails and doc revisions;
   keep newest as canonical, link the rest.

**Large binaries.** Never embed raw. Pipeline: type-sniff → extract text (Tika /
pdfplumber / OCR for images) → chunk → discard bytes. Files over a size cap are
metadata-only with on-demand extraction at query time.

**Hybrid retrieval (recommended).** Run both and fuse:
- BM25/FTS5 for exact term, name, ID matching (fast, cheap).
- Dense embeddings for semantic recall.
- **Reciprocal Rank Fusion**: `score = Σ 1/(k + rank)`, k≈60; optional
  cross-encoder rerank on top 50. Embed lazily — index metadata + BM25
  immediately, backfill embeddings asynchronously.

## 4. Query interface

```json
// Request
{
  "q": "budget cut discussion with Jane",
  "filters": {
    "source": ["outlook", "slack", "sharepoint"],
    "type": ["email", "chat_message"],
    "date": { "gte": "2026-07-01", "lte": "2026-08-01" },
    "person": ["jane@x.com"]
  },
  "mode": "hybrid",
  "limit": 20
}
```

Structured filters constrain the candidate set (SQL) first; hybrid ranking then
orders it. Response returns deduped canonical docs, each with a `citation.url`
deep link and `citation.also_in` for cross-source copies, plus `facets` for
drill-down (see SKILL.md "Output").

## 5. Privacy / security

- **Local-first:** index, blobs, vectors on-device, encrypted at rest via a
  keychain-derived key. Prefer a local embedding model so content never leaves
  the machine.
- **Respect ACLs:** store effective `acl.principals`; every query filters to what
  the connected identity can see; re-validate shared items on access.
- **Secrets:** OAuth/refresh tokens in the OS keychain, never in SQLite or logs;
  connectors receive short-lived scoped handles.
- **Never indexed:** password vaults, `.env`, `id_rsa`, `*.pem`, user
  exclude-globs, muted/private channels, un-opted-in sources. A redaction pass
  strips detected secrets from extracted text before embedding.
- **User controls:** per-source enable, exclusion rules, "forget this item",
  full local purge.

## 6. Phased rollout

- **Phase 0 — MVP:** wrap the tools already present in this environment (see
  `connectors.md`) as read-only connectors. Normalized schema + SQLite/FTS5 (BM25
  only) + unified query + citations; sync = poll-on-query with short TTL. Zero
  local-storage risk; proves the abstraction end to end.
- **Phase 1:** persistent local SQLite/blob/vector store, incremental cursors,
  hybrid RRF ranking with local embeddings, cross-source dedup.
- **Phase 2:** macOS FSEvents connector (text extraction, OCR); native Gmail +
  Google Drive delta connectors for real push sync.
- **Phase 3 — Extensibility GA:** publish the `SourceConnector` SDK +
  `connector.yaml` manifest so users add sources (Notion, Jira, …) as drop-in
  plugins; connector registry with capability negotiation and per-connector scope
  prompts.
