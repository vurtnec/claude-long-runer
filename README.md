# Claude Long-Running Executor

A framework for executing long-running, iterative tasks with Claude Agent SDK.

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Run a task
python long_run_executor.py \
  --task tasks/repetitive_work \
  --params '{"project_dir": "./src", "file_pattern": "*.py"}' \
  --max-iterations 20
```

## Two Task Templates

### 1. Repetitive Work (`tasks/repetitive_work/`)

For batch processing tasks: unit test generation, code audits, migrations, etc.

**Features**:
- Processes files in batches
- State tracking (pending/completed/failed/skipped)
- JSON-based progress reporting
- Auto-discovery of files

**Usage**:
```bash
python long_run_executor.py \
  --task tasks/repetitive_work \
  --params '{"project_dir": "/path/to/project", "file_pattern": "*.tsx"}' \
  --max-iterations 50
```

**Customize**: Edit `tasks/repetitive_work/init_prompt.md` to define what to do with each file.

### 2. Feature Story (`tasks/feature_story/`)

For feature development with acceptance criteria.

**Features**:
- Feature list driven
- Two verification methods:
  - **Code**: Run tests, type check, lint
  - **Browser**: Use Playwright MCP tools
- Completion signal: `ALL_FEATURES_COMPLETED_AND_VERIFIED`

**Usage**:
```bash
python long_run_executor.py \
  --task tasks/feature_story \
  --params '{}' \
  --project-dir /path/to/project \
  --max-iterations 10
```

**Customize**: Edit `tasks/feature_story/init_prompt.md` to define your features and acceptance criteria.

## Command Options

| Option | Description |
|--------|-------------|
| `--task` | Task directory path |
| `--params` | JSON parameters |
| `--max-iterations` | Max iterations (default: 5) |
| `--project-dir` | Working directory |
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
- `processor.py` - State processing logic (optional)

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
└── tasks/
    ├── repetitive_work/  # Batch processing template
    └── feature_story/    # Feature development template
```

## Credits

Based on Anthropic's [autonomous-coding](https://github.com/anthropics/claude-quickstarts/tree/main/autonomous-coding) quickstart.
