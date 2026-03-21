# Claude Agent SDK Research Findings

**Research Date:** 2026-03-21
**Researcher:** Claude Agent (Opus 4.6)

---

## 1. Latest Versions

### Python SDK (`claude-agent-sdk`)
- **Latest Version:** 0.1.50 (released March 20, 2026)
- **PyPI:** https://pypi.org/project/claude-agent-sdk/
- **GitHub:** https://github.com/anthropics/claude-agent-sdk-python
- **Python Requirement:** 3.10+
- **Bundled Claude CLI:** v2.1.81

Recent Python versions:
| Version | Date |
|---------|------|
| 0.1.50 | Mar 20, 2026 |
| 0.1.49 | Mar 17, 2026 |
| 0.1.48 | Mar 7, 2026 |
| 0.1.47 | Mar 6, 2026 |
| 0.1.46 | Mar 5, 2026 |
| 0.1.45 | Mar 3, 2026 |
| 0.1.44 | Feb 26, 2026 |

### TypeScript SDK (`@anthropic-ai/claude-agent-sdk`)
- **Latest Version:** 0.2.81
- **npm:** https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk
- **GitHub:** https://github.com/anthropics/claude-agent-sdk-typescript
- **Maintains parity with Claude Code** (SDK 0.2.X = Claude Code 2.1.X)

### Claude Code CLI (`@anthropic-ai/claude-code`)
- **Latest Version:** 2.1.81 (bundled in SDK)

### Naming History
- Originally named "Claude Code SDK" (launched June 2025)
- Renamed to "Claude Agent SDK" in September 2025
- Old `claude-code-sdk` PyPI package is deprecated
- Old `@anthropic-ai/claude-code-sdk` npm package is deprecated

---

## 2. Plan Mode

### How Plan Mode Works
- **Activation:** Set `permission_mode="plan"` (Python) or `permissionMode: "plan"` (TypeScript)
- **Behavior:** Prevents tool execution entirely. Claude can analyze code and create plans but cannot make changes.
- **ExitPlanMode Tool:** When Claude finishes planning, it calls `ExitPlanMode` to signal completion and request user approval.
  - Does NOT take plan content as a parameter - reads plan from a file written during planning
  - The plan must be complete and unambiguous before calling this tool
  - Claude should use `AskUserQuestion` first to resolve any outstanding questions, NOT use `AskUserQuestion` to ask "is this plan okay?"
- **AskUserQuestion in Plan Mode:** Clarifying questions are especially common in plan mode, where Claude explores the codebase and asks questions before proposing a plan.

### ExitPlanMode Tool Details
- **Input:** `{"plan": str}` (the plan text for user approval)
- **Output:** `{"message": str, "approved": bool | None}`
- As of TypeScript SDK v0.2.76, added `planFilePath` field to tool input

### Known Issues with Plan Mode (from GitHub Issues)
1. ExitPlanMode sometimes returns empty input field (Issue #12288)
2. ExitPlanMode can hang with MCP servers active (Issue #19623)
3. PermissionRequest Allow doesn't always exit plan mode (Issue #15755)
4. Plan mode can block execution in don't-ask mode (Issue #30463)
5. Plan mode has had issues with tool-level enforcement for write operations (Issue #19874)

### Switching Permission Modes Dynamically
```python
q = query(prompt="...", options=ClaudeAgentOptions(permission_mode="plan"))
await q.set_permission_mode("acceptEdits")  # Switch mid-session
```

---

## 3. User Interaction / Approval API

### canUseTool Callback

The primary mechanism for user interaction. Fires in two cases:
1. **Tool needs approval:** Claude wants to use a tool not auto-approved by permission rules
2. **Claude asks a question:** Claude calls the `AskUserQuestion` tool

#### Python Signature
```python
async def can_use_tool(
    tool_name: str,
    input_data: dict,
    context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny
```

#### Response Types
```python
# Allow execution
PermissionResultAllow(
    updated_input=input_data,           # can modify the input
    updated_permissions=None             # can update permission rules
)

# Deny execution
PermissionResultDeny(
    message="reason",                    # Claude sees this message
    interrupt=False                      # if True, interrupts execution
)
```

#### Python-Specific Requirement
In Python, `can_use_tool` requires streaming mode AND a `PreToolUse` hook that returns `{"continue_": True}` to keep the stream open. Without this hook, the stream closes before the permission callback can be invoked.

```python
async def dummy_hook(input_data, tool_use_id, context):
    return {"continue_": True}

options = ClaudeAgentOptions(
    can_use_tool=can_use_tool,
    hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[dummy_hook])]},
)
```

### AskUserQuestion Tool

Claude generates clarifying questions with multiple-choice options.

#### Question Format
```json
{
  "questions": [
    {
      "question": "How should I format the output?",
      "header": "Format",
      "options": [
        { "label": "Summary", "description": "Brief overview" },
        { "label": "Detailed", "description": "Full explanation" }
      ],
      "multiSelect": false
    }
  ]
}
```

#### Response Format
```json
{
  "questions": [...],  // pass through original questions
  "answers": {
    "How should I format the output?": "Summary"
  }
}
```

#### Limits
- 1-4 questions per call
- 2-4 options per question
- Not available in subagents

#### Preview Format (TypeScript only, v0.2.69+)
```typescript
toolConfig: {
    askUserQuestion: { previewFormat: "html" | "markdown" }
}
```

### PermissionRequest Hook (v0.1.29+)
New hook event for external notification when Claude is waiting for approval:
```python
class PermissionRequestHookInput(BaseHookInput):
    hook_event_name: Literal["PermissionRequest"]
    tool_name: str
    tool_input: dict[str, Any]
    permission_suggestions: NotRequired[list[Any]]
```

---

## 4. Permission Modes

| Mode | Description |
|------|-------------|
| `default` | Standard - unmatched tools trigger `canUseTool` |
| `dontAsk` (TS only) | Deny instead of prompting |
| `acceptEdits` | Auto-accept file edits |
| `bypassPermissions` | Bypass all checks (use with caution) |
| `plan` | Planning only - no tool execution |

### Permission Evaluation Order
1. Hooks (can allow, deny, or continue)
2. Deny rules (`disallowed_tools`)
3. Permission mode
4. Allow rules (`allowed_tools`)
5. `canUseTool` callback

---

## 5. Hook System

### Available Hook Events
| Event | Description |
|-------|-------------|
| `PreToolUse` | Before tool execution |
| `PostToolUse` | After successful execution |
| `PostToolUseFailure` | When tool fails |
| `UserPromptSubmit` | When user submits prompt |
| `Stop` | When stopping execution |
| `SubagentStop` | When subagent stops |
| `PreCompact` | Before message compaction |
| `Notification` | Notification events |
| `SubagentStart` | When subagent starts |
| `PermissionRequest` | Permission decision needed |
| `TeammateIdle` (TS) | When teammate is idle |
| `TaskCompleted` (TS) | When task completes |
| `ConfigChange` (TS) | Configuration file changes |

### Hook Types
- **Programmatic hooks:** Callback functions passed to `query()`
- **Filesystem hooks:** Shell commands in `settings.json` (loaded via `settingSources`)
- **HTTP hooks:** POST JSON to a URL (TS, recent addition)

---

## 6. Recent Notable Features

### Python SDK (v0.1.46-0.1.50)
- Session management: `list_sessions()`, `get_session_messages()`, `get_session_info()`
- MCP runtime management: `add_mcp_server()`, `remove_mcp_server()`
- Background task messages: `TaskStarted`, `TaskProgress`, `TaskNotification`
- Agent enhancements: `skills`, `memory`, `mcpServers` fields on `AgentDefinition`
- Session operations: `tag_session()`, `rename_session()`
- Rate limit events: typed `RateLimitEvent` message

### TypeScript SDK (v0.2.69-0.2.81)
- AskUserQuestion preview format (HTML/Markdown)
- `promptSuggestion()` method
- `agentProgressSummaries` for subagents
- `forkSession()` for conversation branching
- ExitPlanMode `planFilePath` field
- V2 Interface preview (simpler multi-turn API)
- Structured outputs support

---

## 7. Message Types

```python
Message = UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent
```

### Key Message Types
- **AssistantMessage:** Claude's responses with `content: list[ContentBlock]`
- **SystemMessage:** System events with `subtype` and `data`
- **ResultMessage:** Final result with `result`, `session_id`, `total_cost_usd`, `usage`, `stop_reason`
- **StreamEvent:** Raw API stream events
- **TaskStartedMessage:** Background task started
- **TaskProgressMessage:** Background task progress
- **TaskNotificationMessage:** Background task completed/failed/stopped

### Content Block Types
- `TextBlock`: text content
- `ThinkingBlock`: extended thinking with signature
- `ToolUseBlock`: tool invocation with id, name, input
- `ToolResultBlock`: tool execution result

---

## Search Queries Used
1. `claude-agent-sdk pypi python package 2025 2026`
2. `@anthropic-ai/claude-code-sdk npm package latest version`
3. `anthropic claude-code SDK GitHub releases changelog 2025 2026`
4. `claude agent SDK plan mode user interaction documentation`
5. `"claude agent sdk" "ExitPlanMode" OR "plan mode" site:github.com`
6. `claude agent sdk "AskUserQuestion" "canUseTool" documentation`
7. `claude agent sdk typescript changelog recent features 2026`

## Tools Used
- WebSearch (7 queries)
- WebFetch (8 page fetches)

## Sources
- [PyPI - claude-agent-sdk](https://pypi.org/project/claude-agent-sdk/)
- [npm - @anthropic-ai/claude-agent-sdk](https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk)
- [GitHub - claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)
- [GitHub - claude-agent-sdk-typescript](https://github.com/anthropics/claude-agent-sdk-typescript)
- [Official Docs - Agent SDK Overview](https://platform.claude.com/docs/en/agent-sdk/overview)
- [Official Docs - Python SDK Reference](https://platform.claude.com/docs/en/agent-sdk/python)
- [Official Docs - Handle Approvals and User Input](https://platform.claude.com/docs/en/agent-sdk/user-input)
- [Official Docs - Configure Permissions](https://platform.claude.com/docs/en/agent-sdk/permissions)
- [Official Docs - Claude Code Features in SDK](https://platform.claude.com/docs/en/agent-sdk/claude-code-features)
- [GitHub - Python SDK Changelog](https://github.com/anthropics/claude-agent-sdk-python/blob/main/CHANGELOG.md)
- [GitHub - TypeScript SDK Changelog](https://github.com/anthropics/claude-agent-sdk-typescript/blob/main/CHANGELOG.md)
- [GitHub - ExitPlanMode System Prompt](https://github.com/Piebald-AI/claude-code-system-prompts/blob/main/system-prompts/tool-description-exitplanmode.md)
- [GitHub Issue #12288 - ExitPlanMode empty input](https://github.com/anthropics/claude-code/issues/12288)
- [GitHub Issue #19623 - ExitPlanMode AbortError](https://github.com/anthropics/claude-code/issues/19623)
- [GitHub Issue #15755 - PermissionRequest Allow issue](https://github.com/anthropics/claude-code/issues/15755)
