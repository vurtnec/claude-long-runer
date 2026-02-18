# Claude Long-Running Executor

A framework for executing long-running, iterative tasks with Claude Agent SDK.

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Run a task
python long_run_executor.py \
  --task tasks/repetitive_work \
  --params '{"file_pattern": "*.py"}' \
  --project-dir ./src \
  --max-iterations 20
```

## Two Task Templates

### 1. Repetitive Work (`tasks/repetitive_work/`)

For batch processing tasks: unit test generation, code audits, migrations, etc.

**Features**:
- Processes files in batches
- State tracking (pending/completed/failed/skipped)
- JSON-based progress reporting
- Auto-discovery of files via `find` command

**Usage**:
```bash
python long_run_executor.py \
  --task tasks/repetitive_work \
  --params '{"file_pattern": "*.tsx"}' \
  --project-dir /path/to/project \
  --max-iterations 50
```

**Customize**: Edit `init_prompt.md` to define what to do with each file.

### 2. Feature Story (`tasks/feature_story/`)

For step-by-step feature implementation based on a project specification.

**Features**:
- `spec.yaml` defines project: tech stack, implementation steps, acceptance criteria
- Step-by-step implementation with progress tracking
- Two verification methods:
  - **Code**: Run commands (npm test, pytest, etc.)
  - **Browser**: Use configurable MCP browser tools (Playwright, BrowserMCP, etc.)
- Processor manages step transitions
- Optional `/long-runner-acceptance-test` skill for automated verification

**Usage**:
```bash
# 1. Replace spec.yaml with your project specification
# 2. Run the task
python long_run_executor.py \
  --task tasks/feature_story \
  --project-dir /path/to/project \
  --max-iterations 30
```

**spec.yaml structure**:
```yaml
project_name: "My App"
overview: "Description..."

technology_stack:
  frontend:
    framework: "React with Vite"
  backend:
    runtime: "Node.js with Express"

implementation_steps:
  - step: 1
    title: "Setup Backend"
    tasks:
      - "Initialize Express server"
      - "Set up database"
    acceptance:
      - type: code
        command: "npm test"
      - type: browser
        url: "http://localhost:3000"
        verify: "Page loads"

success_criteria:
  - "All tests pass"
  - "UI works correctly"
```

## Command Options

| Option | Description |
|--------|-------------|
| `--task` | Task directory path |
| `--params` | JSON parameters (task-specific options) |
| `--project-dir` | Working directory (auto-injected to state) |
| `--max-iterations` | Max iterations (default: 5) |
| `--model` | Claude model (default: claude-sonnet-4-5-20250929) |
| `--resume` | Resume from saved state |

## Creating Custom Tasks

Copy a template and modify:

```bash
cp -r tasks/repetitive_work tasks/my_task
```

Edit these files:
- `task.json` - Configuration and success conditions
- `init_prompt.md` - Initial instructions
- `iter_prompt.md` - Iteration instructions
- `processor.py` - State processing logic
- `spec.yaml` - Project specification (feature_story only)

## Task Configuration

### task.json

```json
{
  "name": "my_task",
  "description": "What this task does",
  "state_file": "my_task_state.json",
  "initial_state": {},
  "success_conditions": [
    {"type": "state_equals", "key": "phase", "value": "completed"}
  ],
  "delay_seconds": 2,
  "state_processor": "processor.py"
}
```

### Success Conditions

| Type | Description |
|------|-------------|
| `text_contains` | Response contains text |
| `text_not_contains` | Response doesn't contain text |
| `state_equals` | State field equals value |
| `state_not_equals` | State field doesn't equal value |
| `iteration_limit` | Stop after N iterations |

## Processor Pattern

Both templates use a `processor.py` to manage state:

```python
def process(response: str, state: Any) -> None:
    # 1. First run: initialize (discover files or parse spec)
    # 2. Parse agent JSON response
    # 3. Update state (move to next batch/step)
    # 4. Check completion
```

**repetitive_work**: Manages file batches (pending → completed/failed/skipped)
**feature_story**: Manages implementation steps (step 1 → step 2 → ... → completed)

## Installing the Skill (Optional)

To use the `/long-runner-acceptance-test` skill for automated verification:

```bash
# Copy skill to Claude Code skills directory
cp -r skills/long-runner-acceptance-test ~/.claude/skills/
```

Configure your browser MCP tool in `task.json`:
```json
{
  "browser_tool": "playwright"  // or "browsermcp", "browser-tool"
}
```

Supported browser tools:
| browser_tool | MCP Tool Prefix |
|--------------|-----------------|
| `playwright` (default) | `mcp__playwright__browser_*` |
| `puppeteer` | `mcp__puppeteer__puppeteer_*` |
| `browsermcp` | `mcp__browsermcp__browser_*` |
| `browser-tool` | `mcp__browser-tool__*` |

## Architecture

```
claude-long-runner/
├── long_run_executor.py  # Main orchestrator
├── agent.py              # Session executor
├── client.py             # Claude SDK wrapper
├── task_config.py        # Config loader
├── state_manager.py      # State persistence
├── success_checker.py    # Condition checker
├── security.py           # Command validation
├── skills/               # Claude Code skills
│   └── long-runner-acceptance-test/
│       └── SKILL.md      # Acceptance test skill
└── tasks/
    ├── repetitive_work/  # Batch processing template
    │   ├── task.json
    │   ├── init_prompt.md
    │   ├── iter_prompt.md
    │   └── processor.py
    └── feature_story/    # Feature development template
        ├── task.json
        ├── init_prompt.md
        ├── iter_prompt.md
        ├── processor.py
        └── spec.yaml     # Project specification
```

## Scheduler

A cron-based daemon that triggers tasks on schedule and sends notifications.

### Running the Daemon

```bash
# Start the scheduler (runs in foreground)
python -m scheduler.daemon

# Run a single poll cycle and exit (for testing)
python -m scheduler.daemon --once

# Immediately run a specific schedule
python -m scheduler.daemon --run daily_stock_news
```

### Schedule Definition

Create YAML files in `schedules/` directory:

```yaml
name: my_schedule
description: "What this schedule does"
enabled: true

trigger:
  type: cron
  cron: "0 8 * * *"           # Standard cron expression
  timezone: "Asia/Shanghai"

task:
  type: inline                 # "inline" (prompt) or "standard" (task dir)
  prompt: |
    Your prompt here. Supports {{today}} and {{now}} templates.
  model: "claude-opus-4-6"
  max_turns: 10
  project_dir: "/path/to/project"

notifications:
  on_success:
    - type: feishu             # feishu, wechat, dingtalk, email, webhook
      title: "Task Done - {{today}}"
      body: "{{last_response}}"
  on_failure:
    - type: feishu
      title: "Task Failed"
      body: "Error: {{error}}"
```

### Notification Channels

| Channel | Config Keys |
|---------|-------------|
| Feishu (webhook) | `FEISHU_WEBHOOK_URL` |
| WeChat (ServerChan) | `SERVERCHAN_KEY` |
| WeChat (WxPusher) | `WXPUSHER_TOKEN`, `WXPUSHER_UID` |
| DingTalk | `DINGTALK_WEBHOOK_URL` |
| Email | `SMTP_USER`, `SMTP_PASSWORD` |

## Feishu Bot

An interactive Feishu bot that provides multi-turn Claude conversations in group chats.

### Features

- Per-chat persistent sessions with full conversation context
- Multi-project support with `/project` switching
- Permission mode control (`/mode plan|auto|default`)
- Session history with `/resume` to restore previous conversations
- Direct execution of predefined schedules via `/run`

### Setup

1. Create an enterprise app at [open.feishu.cn](https://open.feishu.cn)
2. Enable bot capability, add `im:message` permission
3. Subscribe to `im.message.receive_v1` event with **WebSocket** mode
4. Publish a version and add the bot to a group chat
5. Set `FEISHU_APP_ID` and `FEISHU_APP_SECRET` environment variables

### Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/project [alias]` | Show current project / switch project |
| `/mode [plan\|auto\|default]` | Show or switch permission mode |
| `/resume [number]` | List recent sessions / resume by number |
| `/list` | List available schedules |
| `/run <name>` | Run a predefined schedule |
| `/new` | Reset conversation (start fresh) |
| `/stop` | Stop and disconnect current session |

Or just send a message to chat with Claude directly.

### Running Standalone

```bash
python -m scheduler.feishu_bot
```

The bot also starts automatically alongside the daemon when `feishu_bot.enabled: true` in config.

## Configuration

### scheduler_config.yaml

```yaml
daemon:
  poll_interval_seconds: 30
  schedules_dir: "schedules"

notifications:
  feishu:
    webhook_url: "{{env.FEISHU_WEBHOOK_URL}}"
    app_id: "{{env.FEISHU_APP_ID}}"
    app_secret: "{{env.FEISHU_APP_SECRET}}"
  wechat:
    serverchan_key: "{{env.SERVERCHAN_KEY}}"
  # ... see scheduler_config.yaml for all options

defaults:
  model: "claude-opus-4-6"
  max_iterations: 10
  timeout_minutes: 120

feishu_bot:
  enabled: true
  model: "claude-opus-4-6"
  max_turns: 5
  projects:
    my_project: "/path/to/project"
  default_project: "my_project"
```

### Environment Variables

All credentials use `{{env.VAR_NAME}}` templates in the config file. Set them via:

```bash
# Copy the example file and fill in your values
cp .env.example .env
# Edit .env, then load it
source .env
```

See [.env.example](.env.example) for all supported variables.

## Architecture

```
claude-long-runner/
├── long_run_executor.py  # Main orchestrator
├── agent.py              # Session executor
├── client.py             # Claude SDK wrapper
├── task_config.py        # Config loader
├── state_manager.py      # State persistence
├── success_checker.py    # Condition checker
├── security.py           # Command validation
├── scheduler_config.yaml # Global configuration
├── scheduler/            # Scheduler & bot
│   ├── daemon.py         # Cron daemon
│   ├── feishu_bot.py     # Feishu bot server
│   ├── inline_executor.py
│   ├── schedule_loader.py
│   ├── trigger_engine.py
│   ├── models.py
│   └── notifiers/        # Notification channels
├── schedules/            # Schedule YAML files
├── skills/               # Claude Code skills
│   └── long-runner-acceptance-test/
└── tasks/
    ├── repetitive_work/  # Batch processing template
    └── feature_story/    # Feature development template
```

## Credits

Based on Anthropic's [autonomous-coding](https://github.com/anthropics/claude-quickstarts/tree/main/autonomous-coding) quickstart.
