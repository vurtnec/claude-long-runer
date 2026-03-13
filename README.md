# Claude Long-Runner

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Built with Claude Agent SDK](https://img.shields.io/badge/Built%20with-Claude%20Agent%20SDK-blueviolet.svg)](https://docs.anthropic.com/en/docs/claude-code/sdk)

**Run Claude tasks autonomously, on a schedule, or from your group chat.**

<!-- TODO: Add a screenshot or GIF here -->
<!-- ![Demo](docs/images/demo.gif) -->

## Why?

Claude Code is powerful — but it's interactive. You sit there and watch it. Claude Long-Runner removes that constraint:

- **Long-running tasks** — Claude Code has no built-in support for persistent, multi-step tasks that survive interruptions. Long-Runner executes complex tasks across iterations with automatic state persistence and resume.
- **Always-on scheduling** — Claude Code's `/loop` command is limited and ephemeral. Long-Runner provides a real cron daemon that runs 24/7, triggers tasks on schedule, and delivers results to your notification channels.
- **Team chat interface** — Claude Code's remote mode lacks project switching and has poor voice recognition. Long-Runner's Feishu Bot gives your whole team access to Claude with per-chat sessions, seamless project switching, and Feishu's excellent voice-to-text.

## Quick Start (3 minutes)

### Prerequisites

- [Python 3.10+](https://www.python.org/downloads/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (provides the Claude Agent SDK)

### 1. Install

```bash
git clone https://github.com/vurtnec/claude-long-runner.git
cd claude-long-runner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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

That's it! @mention the bot in your Feishu group chat and start talking to Claude.

---

## Long Runner Task

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
| `--model` | Claude model (default: claude-sonnet-4-5-20250929) |
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
  model: "claude-sonnet-4-5-20250929"
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

Interactive bot for multi-turn Claude conversations in Feishu group chats. Each chat maintains its own independent session with full tool access.

```bash
python -m scheduler.feishu_bot          # standalone
python -m scheduler.daemon              # or with daemon (auto-starts if enabled)
```

### Bot Commands

| Command | Description |
|---------|-------------|
| `/project [alias]` | View / switch project |
| `/mode [plan\|auto\|default]` | View / switch permission mode |
| `/resume [n]` | List sessions / resume one |
| `/run <name>` | Run a schedule |
| `/new` | Reset conversation |
| `/stop` | Disconnect session |
| _(any message)_ | Chat with Claude |

### Permission Modes

| Mode | Behavior |
|------|----------|
| `plan` | Claude suggests changes only, no execution |
| `auto` | Auto-approve file edits within allowed directories |
| `default` | Per-operation approval required |

---

## Project Structure

```
claude-long-runner/
├── long_run_executor.py      # Main orchestrator: task loop, state management
├── client.py                 # SDK client factory with MCP server integration
├── task_config.py            # Task configuration loader
├── state_manager.py          # JSON-based state persistence
├── success_checker.py        # Completion condition evaluator
├── security.py               # Command allowlisting and validation
│
├── scheduler/
│   ├── daemon.py             # Scheduler main loop and task dispatch
│   ├── feishu_bot.py         # Feishu bot (WebSocket, per-chat sessions)
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

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT License](LICENSE)

## Support

Questions or issues? Please [open a GitHub issue](https://github.com/vurtnec/claude-long-runner/issues).
