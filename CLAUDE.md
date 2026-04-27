# CLAUDE.md — Architecture Reference

This file is for AI agents working on this codebase. For human setup instructions, see [README.md](README.md).

## Project Overview

Vurtnec Loom is a Python framework built on the Claude Agent SDK (with Codex SDK as an alternate backend) for weaving AI agents into real workflows. Three main capabilities:

1. **Long-Run Tasks** — iterative task executor with state persistence
2. **Scheduler** — cron-based daemon with multi-channel notifications
3. **Feishu Bot** — interactive group chat bot with per-chat sessions, supporting both Claude and Codex backends

## Project Structure

```
claude-long-runner/
├── long_run_executor.py      # Main orchestrator: task loop, state mgmt, success checking
├── client.py                 # Claude SDK client factory with MCP server integration
├── task_config.py            # Task configuration loader from task.json
├── state_manager.py          # JSON-based state persistence and tracking
├── success_checker.py        # Success condition evaluation engine
├── security.py               # Command allowlisting and validation hooks
│
├── scheduler/
│   ├── __init__.py
│   ├── __main__.py           # Entry point for `python -m scheduler`
│   ├── daemon.py             # Main scheduler loop, trigger evaluation, task dispatch
│   ├── feishu_bot.py         # Feishu bot server (WebSocket, per-chat sessions)
│   ├── schedule_loader.py    # YAML schedule parsing and validation
│   ├── models.py             # Dataclass models: schedules, triggers, notifications
│   ├── trigger_engine.py     # Trigger evaluation logic (cron, file, http, composite)
│   ├── inline_executor.py    # Inline task execution (simple prompt-based)
│   ├── execution_log.py      # Execution history management
│   ├── notifiers/            # Notification channel implementations
│   │   ├── base.py           # Abstract base notifier
│   │   ├── feishu_notifier.py
│   │   ├── wechat_notifier.py
│   │   ├── dingtalk_notifier.py
│   │   ├── email_notifier.py
│   │   └── webhook_notifier.py
│   └── triggers/             # Trigger implementations
│       ├── base.py           # Abstract base trigger
│       ├── cron_trigger.py
│       ├── file_trigger.py
│       ├── http_trigger.py
│       └── composite_trigger.py
│
├── schedules/                # Schedule YAML definitions
│   └── _examples/            # Example schedules
│
├── tasks/                    # Task templates
│   ├── repetitive_work/      # Batch processing template
│   │   ├── task.json
│   │   ├── init_prompt.md
│   │   ├── iter_prompt.md
│   │   └── processor.py
│   └── feature_story/        # Step-by-step feature template
│       ├── task.json
│       ├── init_prompt.md
│       ├── iter_prompt.md
│       ├── processor.py
│       └── spec.yaml
│
├── skills/
│   └── long-runner-acceptance-test/
│       └── SKILL.md          # Acceptance test skill for feature_story
│
├── .env.example
├── scheduler_config.example.yaml
└── requirements.txt
```

## Core Components

### long_run_executor.py

Main orchestrator. Runs the task loop:
1. Load task config from `task.json`
2. Initialize or resume state from `state_manager`
3. Render `init_prompt.md` (first iteration) or `iter_prompt.md` (subsequent)
4. Send prompt to Claude via `client.py`
5. Run `processor.py` to parse response and update state
6. Check success conditions via `success_checker.py`
7. Repeat until success or max iterations

### client.py

Factory for Claude SDK clients. Handles:
- MCP server loading from `~/.claude.json` (same config as Claude Code CLI)
- Global vs project-level MCP server resolution
- Browser tool integration (Playwright, Puppeteer, BrowserMCP)

### task_config.py

Loads task configuration from a directory. Each task has:
- `task.json` — metadata, initial state, success conditions
- `init_prompt.md` — Jinja-style template for first iteration
- `iter_prompt.md` — template for subsequent iterations
- `processor.py` — Python module with `process(response, state)` function

### state_manager.py

JSON-based state persistence. Features:
- Atomic writes to prevent corruption
- State diffing for logging
- Auto-injection of `project_dir` into state

### success_checker.py

Evaluates completion conditions (AND logic — all must pass):

| Type | Params | Description |
|------|--------|-------------|
| `text_contains` | `text` | Response contains string |
| `text_not_contains` | `text` | Response lacks string |
| `state_equals` | `key`, `value` | State field equals value |
| `state_not_equals` | `key`, `value` | State field differs |
| `iteration_limit` | `max` | Stop after N iterations |
| `custom_function` | `function` | Custom Python callable |

### security.py

Allowlist-based command validation. Pre-tool-use hook that:
- Parses bash commands with `shlex`
- Checks first token against allowed set
- Tasks can extend via `allowed_commands` in `task.json`

Base allowed commands include: `ls`, `cat`, `grep`, `cp`, `mv`, `rm`, `mkdir`, `npm`, `pnpm`, `python`, `git`, `gh`, `xcodebuild`, `swift`, `pod`, `curl`, `find`, `ps`, `kill`, etc.

## Task Templates

### repetitive_work

State machine: `initializing` → `processing` → `completed`

State fields:
```json
{
  "phase": "initializing|processing|completed",
  "pending_files": [],
  "completed_files": [],
  "failed_files": [],
  "skipped_files": [],
  "current_batch": [],
  "batch_size": 3,
  "total_files": 0
}
```

Processor expects agent to respond with JSON:
```json
{
  "action": "batch_complete",
  "results": [
    {"file": "path", "status": "completed|failed|skipped", "message": "..."}
  ]
}
```

### feature_story

State machine: `initializing` → `implementing` → `completed`

State fields:
```json
{
  "phase": "initializing|implementing|completed",
  "current_step": 1,
  "total_steps": 0,
  "completed_steps": [],
  "failed_steps": [],
  "spec_file": "spec.yaml"
}
```

Processor expects agent to respond with JSON:
```json
{
  "action": "step_complete",
  "step": 1,
  "results": {
    "tasks_completed": ["task1", "task2"],
    "acceptance_passed": true
  }
}
```

`spec.yaml` structure:
```yaml
project_name: "My App"
overview: "..."
technology_stack:
  frontend: { framework: "React with Vite" }
  backend: { runtime: "Node.js with Express" }
implementation_steps:
  - step: 1
    title: "Setup Backend"
    tasks: ["Initialize Express", "Set up DB"]
    acceptance:
      - type: code
        command: "npm test"
      - type: browser
        url: "http://localhost:3000"
        verify: "Page loads"
success_criteria: ["All tests pass"]
```

## Scheduler Architecture

### daemon.py

Main loop:
1. Load all YAML files from `schedules/` via `schedule_loader.py`
2. Every `poll_interval_seconds`, evaluate triggers via `trigger_engine.py`
3. When triggered, dispatch task (standard → `long_run_executor`, inline → `inline_executor`)
4. On completion/failure, send notifications via `notifiers/`
5. Log to `scheduler_history.json`

### Trigger Types

| Type | Config | Description |
|------|--------|-------------|
| `cron` | `cron`, `timezone` | Standard cron expressions |
| `file_changed` | `paths`, `debounce_seconds` | File modification watch |
| `http_condition` | `url`, `headers`, `condition` | HTTP endpoint polling |
| `composite` | `operator` (and/or), `triggers` | Combine multiple triggers |

### Schedule YAML Schema

```yaml
name: string              # unique identifier
description: string
enabled: bool

trigger:
  type: cron|file_changed|http_condition|composite
  cron: "* * * * *"       # for cron type
  timezone: "Asia/Shanghai"

task:
  type: standard|inline
  # standard:
  name: string            # maps to tasks/{name}/
  params: {}
  project_dir: string
  model: string
  max_iterations: int
  # inline:
  prompt: string
  max_turns: int

timeout_minutes: int

notifications:
  on_success: [NotificationConfig]
  on_failure: [NotificationConfig]

retry:
  max_retries: int
  retry_delay_minutes: int
```

### Template Variables

Available in schedule YAML and notification templates:
- `{{today}}` — YYYY-MM-DD
- `{{now}}` — ISO timestamp
- `{{last_response}}` — agent's final response
- `{{error}}` — error message
- `{{duration}}` — execution time
- `{{iterations}}` — iteration count
- `{{env.VAR_NAME}}` — environment variable (in scheduler_config.yaml)

### Notification Channels

| Channel | Type Key | Required Env Vars | Status |
|---------|----------|-------------------|--------|
| Feishu (webhook) | `feishu` | `FEISHU_WEBHOOK_URL` | ✅ Tested |
| Feishu (app) | `feishu` | `FEISHU_APP_ID`, `FEISHU_APP_SECRET` | ✅ Tested |
| WeChat (ServerChan) | `wechat` (channel: serverchan) | `SERVERCHAN_KEY` | Experimental |
| WeChat (WxPusher) | `wechat` (channel: wxpusher) | `WXPUSHER_TOKEN`, `WXPUSHER_UID` | Experimental |
| DingTalk | `dingtalk` | `DINGTALK_WEBHOOK_URL` | Experimental |
| Email | `email` | `SMTP_USER`, `SMTP_PASSWORD` | Experimental |
| Generic Webhook | `webhook` | `url` in notification config | Experimental |

## Feishu Bot Architecture

### feishu_bot.py

WebSocket-based bot using `lark-oapi` SDK. Key design:

- **Per-chat sessions**: Each group chat maintains independent Claude session
- **Session persistence**: Recent sessions stored at `~/.claude-long-runner/feishu_sessions.json` (up to 10 per chat)
- **Session timeout**: Auto-disconnect after 50 hours of inactivity
- **Project switching**: `/project <alias>` changes working directory for the session

### Permission Modes

| Mode | Behavior |
|------|----------|
| `plan` | Agent suggests changes only, no execution |
| `auto` | Auto-approve file edits within allowed directories |
| `default` | Per-operation approval required |
| `bypass` | All permissions granted |

### Bot Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/project [alias]` | Show current / switch project |
| `/mode [plan\|auto\|default]` | Show / switch permission mode |
| `/resume [number]` | List recent sessions / resume by number |
| `/list` | List available schedules |
| `/run <name>` | Run a predefined schedule |
| `/new` | Reset conversation (start fresh) |
| `/stop` | Stop and disconnect current session |

## MCP Integration

MCP servers are loaded from `~/.claude.json` (shared with Claude Code CLI):

- **Global**: `mcpServers` at root level → available to all projects
- **Project-level**: `projects.<path>.mcpServers` → available when that project is active
- Project-level overrides global if same server name
- Browser tools configured per-task via `browser_tool` in `task.json`

Supported browser tools:
| `browser_tool` | MCP Prefix |
|----------------|------------|
| `playwright` (default) | `mcp__playwright__browser_*` |
| `puppeteer` | `mcp__puppeteer__puppeteer_*` |
| `browsermcp` | `mcp__browsermcp__browser_*` |
| `browser-tool` | `mcp__browser-tool__*` |

## Configuration Files

| File | Purpose |
|------|---------|
| `.env` | Environment credentials (from `.env.example`) |
| `scheduler_config.yaml` | Global config: daemon, notifications, defaults, bot (from `scheduler_config.example.yaml`) |
| `tasks/*/task.json` | Per-task: metadata, initial state, success conditions |
| `tasks/*/init_prompt.md` | First iteration prompt template |
| `tasks/*/iter_prompt.md` | Subsequent iteration prompt template |
| `tasks/*/processor.py` | State processor (`process(response, state)`) |
| `tasks/feature_story/spec.yaml` | Project specification for feature_story |
| `schedules/*.yaml` | Schedule definitions |
| `scheduler_history.json` | Execution history log |

## Dependencies

```
claude-agent-sdk>=0.1.47
pyyaml>=6.0
croniter>=2.0.0
lark-oapi>=1.4.0
python-dotenv>=1.0.0
```

## Key Design Decisions

- **No database**: All state is JSON files for simplicity and portability
- **Directory-based tasks**: Each task is a self-contained directory with config, prompts, and processor
- **Allowlist security**: Bash commands must be explicitly allowed; tasks can extend the base set
- **MCP reuse**: Leverages existing Claude Code CLI MCP config rather than maintaining separate config
- **WebSocket for bot**: Uses Feishu WebSocket mode (no public URL / webhook endpoint needed)
