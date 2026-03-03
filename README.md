# Claude Long-Runner

A framework for running autonomous, multi-turn Claude tasks with state persistence, cron scheduling, and Feishu bot integration.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy and fill in your credentials (only fill what you need):

```bash
cp .env.example .env
```

```bash
# Feishu — notifications + bot
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_HOOK_ID
FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=your_app_secret_here

# WeChat — ServerChan or WxPusher (pick one)
SERVERCHAN_KEY=your_serverchan_key
WXPUSHER_TOKEN=your_wxpusher_token
WXPUSHER_UID=your_wxpusher_uid

# DingTalk
DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN

# Email
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=your_app_password
```

Then load: `source .env`

---

## 1. Long Runner Task

Run multi-iteration tasks with state persistence and resume. Good for batch processing, step-by-step feature builds, code migrations.

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

## 2. Scheduler

Cron daemon that runs tasks on a schedule and sends notifications.

### Setup

```bash
cp scheduler_config.example.yaml scheduler_config.yaml
# edit scheduler_config.yaml
```

### Run

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

**Notification channels:** `feishu`, `wechat` (ServerChan / WxPusher), `dingtalk`, `email`, `webhook`.

See `schedules/_examples/` for more.

---

## 3. Feishu Bot

Interactive bot for multi-turn Claude conversations in Feishu group chats.

### Setup

1. Create an app at [open.feishu.cn](https://open.feishu.cn)
2. Enable **Bot** capability, add `im:message` permission
3. Subscribe to `im.message.receive_v1`, use **WebSocket** mode
4. Publish and add the bot to a group chat
5. Set `FEISHU_APP_ID` and `FEISHU_APP_SECRET` in `.env`
6. Configure in `scheduler_config.yaml`:

```yaml
feishu_bot:
  enabled: true
  model: "claude-opus-4-6"
  max_turns: 10
  projects:
    my-project: "/path/to/your/project"
  default_project: "my-project"
```

### Run

```bash
python -m scheduler.feishu_bot          # standalone
python -m scheduler.daemon              # or with daemon (auto-starts if enabled)
```

### Commands

| Command | Description |
|---------|-------------|
| `/project [alias]` | View / switch project |
| `/mode [plan\|auto\|default]` | View / switch permission mode |
| `/resume [n]` | List sessions / resume one |
| `/run <name>` | Run a schedule |
| `/new` | Reset conversation |
| `/stop` | Disconnect session |
| _(any message)_ | Chat with Claude |

---
