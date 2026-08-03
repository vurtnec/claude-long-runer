# Connector Map — source → tools in this environment

How each normalized source maps to concrete tools. Tool names follow the
`mcp__<server>__<tool>` convention `client.py` already uses. When a source has no
connected tool, report it as `sources_unavailable` rather than failing the query.

## Available today (Phase 0 — read-only, no new auth)

| Source | Normalized `type` | Search tool(s) | Read/context tool(s) |
|--------|-------------------|----------------|----------------------|
| Outlook / Exchange email | `email` | `mcp__Microsoft_365__outlook_email_search` | `mcp__Microsoft_365__read_resource` |
| Microsoft Teams | `chat_message` | `mcp__Microsoft_365__chat_message_search` | `mcp__Microsoft_365__teams_list_chats`, `read_resource` |
| SharePoint / OneDrive | `file` / `doc` | `mcp__Microsoft_365__sharepoint_search`, `sharepoint_folder_search` | `read_resource` |
| Outlook calendar | `calendar_event` | `mcp__Microsoft_365__outlook_calendar_search` | `read_resource` |
| Slack / other chat (Macro) | `chat_message` | `mcp__Macro__ContentSearch`, `mcp__Macro__NameSearch` | `mcp__Macro__ReadChannelMessages`, `ReadChannelThread`, `ReadChat`, `ReadContent` |
| Google Calendar | `calendar_event` | `mcp__Google_Calendar__search_events` | `mcp__Google_Calendar__list_events`, `get_event` |
| Local files (sandbox) | `file` | `Grep`, `Glob` | `Read` |
| GitHub (code/issues/PRs) | `doc` / `file` | `mcp__github__search_code`, `search_issues`, `search_pull_requests` | `mcp__github__get_file_contents`, `pull_request_read` |

## Cowork-native connectors (promote in Phase 1+)

Claude Cowork exposes these as first-class OAuth connectors; wrap them the same
way and, where the API offers a delta cursor, upgrade from poll-on-query to real
incremental sync.

| Source | Read | Write | Notes |
|--------|------|-------|-------|
| Gmail | full | drafts only | Google Workspace connector |
| Google Drive | full | full | delta via `pageToken` |
| Google Calendar | full | full | |
| Slack | channels/threads | post / draft | |
| Microsoft 365 (Outlook) | full | read-only by default | admin can enable write |
| Microsoft 365 (Teams) | full | read-only | always read-only |
| SharePoint / OneDrive | full | admin-gated | |

## Adding a new source

1. Write a `connector.yaml` (`id`, auth type, scopes, capability flags:
   `delta` / `contentFetch` / `acl`).
2. Implement the `SourceConnector` contract (see `architecture.md` §1): `list`,
   `fetchMetadata`, `fetchContent`, `fetchACL`.
3. Provide a `normalize()` that maps the source's raw items into `loom.doc.v1`
   (`doc-schema.json`), including a real `location.url` deep link and a
   `content_hash`.
4. Register it; the engine picks it up via capability negotiation — no core
   changes.

## Query-time fan-out rules

- Only call tools for sources that pass the request's `filters.source`.
- Run source queries in parallel where the tools allow.
- Cap per-source results before merging (e.g. top 25/source) to keep fusion cheap.
- Always capture a deep link at fetch time — it is required for citation.
