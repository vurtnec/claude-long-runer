# Vurtnec Loom

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Default backend: Codex](https://img.shields.io/badge/default-Codex%20%2F%20GPT--5.5-10a37f.svg)](https://github.com/openai/codex)
[![Claude backend optional](https://img.shields.io/badge/Claude%20backend-optional-blueviolet.svg)](https://docs.anthropic.com/en/docs/claude-code/sdk)

**Weave AI agents into schedules, team chat, and persistent workflows — backed by Claude, Codex, or OpenCode.**

<!-- TODO: Add a screenshot or GIF here -->
<!-- ![Demo](docs/images/demo.gif) -->

## Why?

Claude Code, OpenAI Codex, and OpenCode are powerful — but interactive. You sit there and watch them. Vurtnec Loom removes that constraint by weaving agents into the workflows around them:

- **Long-running tasks** — interactive coding agents do not give you this scheduler-style, persistent task loop out of the box. Loom executes complex tasks across iterations with automatic state persistence and resume.
- **Always-on scheduling** — Claude Code's `/loop` is limited and ephemeral. Loom provides a real cron daemon that runs 24/7, triggers tasks on schedule, and delivers results to your notification channels.
- **Team chat interface** — the upstream remote modes lack project switching and have poor voice recognition. Loom's Feishu Bot gives your whole team access with per-chat sessions, seamless project switching, and Feishu's excellent voice-to-text.
- **Multi-backend** — pick the agent best suited to the task: Claude, Codex, or OpenCode. Switch per-chat at runtime via `/backend`, with each backend keeping its own session history isolated.

## Quick Start (3 minutes)

### Prerequisites

- [Python 3.10+](https://www.python.org/downloads/)
- [Codex CLI](https://github.com/openai/codex) — the default backend. Install via `npm install -g @openai/codex` or `brew install codex`, then `codex login`.
- *(Optional)* [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — only needed if you explicitly switch to the Claude backend.
- *(Optional)* [OpenCode CLI](https://opencode.ai/) — only needed if you use `backend: opencode`. Install it, authenticate providers, and configure models/MCP in `opencode.json` or `.opencode`.

### 1. Install

```bash
git clone https://github.com/vurtnec/claude-long-runner.git
cd claude-long-runner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install the Codex Python SDK.
# Not yet on PyPI — install from source, pinned to a stable Codex CLI tag:
pip install git+https://github.com/openai/codex.git@rust-v0.125.0#subdirectory=sdk/python
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` — for the Feishu Bot, you only need these three:

```bash
FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=your_app_secret_here
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_HOOK_ID
```

> To get these credentials: go to [open.feishu.cn](https://open.feishu.cn) → Create App → Enable **Bot** capability → Add `im:message` permission → Subscribe to `im.message.receive_v1` with **WebSocket** mode → Publish the app → Add the bot to a group chat.

Then set up the scheduler config:

```bash
cp scheduler_config.example.yaml scheduler_config.yaml
```

Edit `scheduler_config.yaml` to add your project paths:

```yaml
feishu_bot:
  enabled: true
  projects:
    my-project: "/path/to/your/project"
  default_project: "my-project"
```

### 3. Run

```bash
# Start the Feishu Bot (standalone)
python -m scheduler.feishu_bot

# Or start the full daemon (scheduler + bot)
python -m scheduler.daemon
```

That's it! @mention the bot in your Feishu group chat and start talking to the configured backend.

---

## Long-Run Tasks

Run multi-iteration tasks with state persistence and resume. Good for batch processing, step-by-step feature builds, and code migrations.

```bash
python long_run_executor.py \
  --task tasks/repetitive_work \
  --params '{"file_pattern": "*.py"}' \
  --project-dir /path/to/project \
  --max-iterations 20
```

| Flag | Description |
|------|-------------|
| `--task` | Task directory (required) |
| `--params` | JSON params for the task |
| `--project-dir` | Working directory (default: `.`) |
| `--max-iterations` | Max iterations (default: 5) |
| `--backend` | Agent backend: `codex`, `claude`, or `opencode` (default: `codex`) |
| `--model` | Agent model (backend default if omitted; OpenCode uses `provider/model`) |
| `--resume` | Resume from last saved state |

**Built-in templates:**

- `tasks/repetitive_work/` — batch file processing (test gen, audits, migrations)
- `tasks/feature_story/` — step-by-step feature implementation from a `spec.yaml`

**Create your own:** `cp -r tasks/repetitive_work tasks/my_task`, then edit `task.json`, `init_prompt.md`, `iter_prompt.md`, `processor.py`.

---

## Scheduler

Cron daemon that runs tasks on a schedule and sends notifications.

```bash
python -m scheduler.daemon              # start daemon
python -m scheduler.daemon --once       # run one cycle and exit
python -m scheduler.daemon --run <name> # run a specific schedule now
```

### Create a Schedule

Add a YAML file in `schedules/`. Two types:

**Inline** — just a prompt:

```yaml
name: morning_briefing
enabled: true
trigger:
  type: cron
  cron: "30 7 * * 1-5"
  timezone: "Asia/Shanghai"
task:
  type: inline
  prompt: "Today is {{today}}. Summarize market highlights and tech news."
  backend: "codex"
  model: "gpt-5.5"
  max_turns: 3
notifications:
  on_success:
    - type: feishu
      title: "Briefing - {{today}}"
      body: "{{last_response}}"
```

**Standard** — references a `tasks/` directory:

```yaml
name: daily_analysis
enabled: true
trigger:
  type: cron
  cron: "0 8 * * *"
  timezone: "Asia/Shanghai"
task:
  name: data_analysis
  params: { report_type: "daily" }
  project_dir: "/path/to/project"
  max_iterations: 10
notifications:
  on_success:
    - type: feishu
      title: "Done - {{today}}"
      body: "{{last_response}}"
  on_failure:
    - type: feishu
      body: "Error: {{error}}"
```

**Notification channels:** `feishu` (tested), `wechat` (ServerChan / WxPusher), `dingtalk`, `email`, `webhook` — channels other than Feishu are experimental and untested.

See `schedules/_examples/` for more examples.

---

## Feishu Bot

Interactive bot for multi-turn agent conversations in Feishu group chats. Each chat maintains its own independent session with full tool access. **Claude, Codex, and OpenCode backends are supported** — switch per-chat at runtime.

```bash
python -m scheduler.feishu_bot          # standalone
python -m scheduler.daemon              # or with daemon (auto-starts if enabled)
```

### Bot Commands

| Command | Description |
|---------|-------------|
| `/project [alias]` | View / switch project |
| `/backend [claude\|codex\|opencode]` | View / switch agent backend |
| `/model [name]` | View / switch model (backend-aware) |
| `/mode [plan\|ask\|auto\|edits\|bypass]` | View / switch permission mode |
| `/effort [low\|medium\|high\|xhigh\|max]` | View / switch reasoning effort |
| `/resume [n]` | List sessions for current backend / resume one |
| `/rename <title>` | Rename current session |
| `/run <name>` | Run a schedule |
| `/new` | Reset conversation (archive current session) |
| `/stop` | Disconnect session |
| `/cancel` | Interrupt current request (keep session) |
| `/status` | Show whether the agent is currently working |
| _(any other `/cmd`)_ | Forwarded to the agent — use backend custom slash commands directly (e.g. `/init`, `/commit`) |
| _(any plain message)_ | Chat with the agent |

### Multi-backend Support

Each chat picks one backend at a time. Switching resets the session (similar to `/model`); each backend keeps its own session history.

| Aspect | Claude | Codex | OpenCode |
|--------|--------|-------|----------|
| Default model | cc-switch Opus mapping, or `claude-opus-4-8` | `gpt-5.5` | OpenCode config default |
| Available models | `opus`, `sonnet`, `haiku` | `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2` | Raw `provider/model` |
| Permission modes | Dynamic via `/mode` | Set at session start only | `bypass` maps to `--dangerously-skip-permissions`; otherwise OpenCode default |
| Custom slash commands | `~/.claude/commands/*.md` | Codex CLI's mechanism | OpenCode commands/config |
| Session resume | Per-chat history | Per-chat history | `opencode session list` / `--session` |

Per-project default backend can be configured in `scheduler_config.yaml`:

```yaml
feishu_bot:
  default_backend: codex          # global default: claude, codex, or opencode
  projects:
    my-claude-app:
      path: /path/to/claude-app
      backend: claude             # uses cc-switch Opus mapping, or claude-opus-4-8
      model: auto
    my-python-app:
      path: /path/to/app
      backend: codex              # this project defaults to Codex
      model: gpt-5.5
    my-web-app:
      path: /path/to/web
      backend: opencode
      model: anthropic/claude-sonnet-4-5
```

Priority: `/backend` command > project config > `default_backend`.

For OpenCode, omit `model` to use the model configured in `opencode.json`; set `OPENCODE_BIN` only if the `opencode` executable is not on PATH. OpenCode MCP servers are configured by OpenCode itself, not migrated from Codex or Claude settings.

### Permission Modes

| Mode | Behavior |
|------|----------|
| `plan` | Claude suggests changes only, no execution |
| `auto` | Auto-determine permissions per operation |
| `edits` | Auto-approve file edits within allowed directories |
| `ask` (default) | Per-operation approval required |

> Codex and OpenCode do not support dynamic mode switching in an active chat session. The chosen mode applies when the next session is created.

---

## Project Structure

```
claude-long-runner/
├── long_run_executor.py      # Main orchestrator: task loop, state management
├── client.py                 # Claude SDK client factory with MCP server integration
├── agent_protocol.py         # Backend-agnostic AgentClient protocol + AgentEvent + factory
├── claude_agent.py           # Claude backend (wraps Claude Agent SDK)
├── codex_agent.py            # Codex backend (wraps OpenAI Codex Python SDK)
├── opencode_agent.py         # OpenCode backend (wraps opencode run JSON events)
├── task_config.py            # Task configuration loader
├── state_manager.py          # JSON-based state persistence
├── success_checker.py        # Completion condition evaluator
├── security.py               # Command allowlisting and validation
│
├── scheduler/
│   ├── daemon.py             # Scheduler main loop and task dispatch
│   ├── feishu_bot.py         # Feishu bot (WebSocket, per-chat sessions, multi-backend)
│   ├── schedule_loader.py    # YAML schedule parsing
│   ├── trigger_engine.py     # Trigger evaluation (cron, file, http, composite)
│   ├── notifiers/            # Feishu (tested), WeChat, DingTalk, Email, Webhook
│   └── triggers/             # Cron, file, HTTP, composite trigger implementations
│
├── tasks/                    # Task templates
│   ├── repetitive_work/      # Batch processing template
│   └── feature_story/        # Step-by-step feature template
│
└── schedules/
    └── _examples/            # Example schedule definitions
```

### Adding a new backend

The agent layer is abstracted via [agent_protocol.py](agent_protocol.py). To add another backend (e.g. Gemini):

1. Implement the `AgentClient` protocol in a new `<name>_agent.py`
2. Map the SDK's events to `AgentEvent` types
3. Register it in `create_agent_client()`'s factory dispatch

The Feishu Bot, command routing, session storage, and `/resume` history all work without changes.

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT License](LICENSE)

## Support

Questions or issues? Please [open a GitHub issue](https://github.com/vurtnec/claude-long-runner/issues).
