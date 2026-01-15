# Task Template

Use this template to create new long-running tasks for the Claude executor framework.

## Creating a New Task

1. **Copy this template directory**:
   ```bash
   cp -r tasks/template tasks/your_task_name
   cd tasks/your_task_name
   ```

2. **Edit `task.json`**:
   - Set `name` to your task identifier
   - Define `initial_state` fields your task needs
   - Configure `success_conditions` (when the task is done)
   - Adjust `delay_seconds` between iterations

3. **Write `init_prompt.md`**:
   - This runs only once at task start
   - Set up context and initial instructions
   - Use `{variable_name}` for parameter substitution

4. **Write `iter_prompt.md`**:
   - This runs on each iteration
   - Check previous state: `{field_name}`
   - Verify progress and guide next steps

## Available Success Condition Types

| Type | Parameters | Example |
|------|------------|---------|
| `text_contains` | `text` | Check if response contains specific text |
| `text_not_contains` | `text` | Check if response doesn't contain text |
| `state_equals` | `key`, `value` | Check if state field equals value |
| `state_not_equals` | `key`, `value` | Check if state field doesn't equal value |
| `iteration_limit` | `max` | Stop after N iterations |

## Prompt Variable Substitution

Variables in prompts are replaced using Python's `.format()`:

```markdown
# In init_prompt.md or iter_prompt.md
Your task: Process item #{item_id}
Current status: {status}
Iteration: {iteration}
```

Available variables:
- **From task params**: Any key passed via `--params '{...}'`
- **From state**: All fields in the current state
- **Built-in**: `{iteration}` - current iteration number

## Example Task

See `tasks/pr_review/` for a complete working example.

## Running Your Task

```bash
python long_run_executor.py \
  --task your_task_name \
  --params '{"param1": "value1", "param2": 123}' \
  --max-iterations 5 \
  --project-dir /path/to/project
```
