# {project_name}

{overview}

## Technology Stack
{technology_stack_display}

---

## Implementation Steps Overview
{implementation_steps_display}

---

## Current Step: Step {current_step} - {current_step_title}

### Tasks
{current_step_tasks_display}

### Acceptance Criteria
{current_step_acceptance_display}

---

## Instructions

1. Implement all tasks for the current step
2. Run acceptance tests:
   - **Code verification**: Run the specified command
   - **Browser verification**: Use Playwright MCP tools:
     - `mcp__playwright__browser_navigate` - Open URL
     - `mcp__playwright__browser_snapshot` - Get page snapshot
     - `mcp__playwright__browser_click` - Click elements
     - `mcp__playwright__browser_type` - Type text
3. When all tasks are done and acceptance passes, output JSON:

```json
{{
  "action": "step_complete",
  "step": {current_step},
  "results": {{
    "tasks_completed": ["task1", "task2", "..."],
    "acceptance_passed": true,
    "notes": "Optional notes about implementation"
  }}
}}
```

If acceptance fails, output:
```json
{{
  "action": "step_failed",
  "step": {current_step},
  "reason": "Description of what failed"
}}
```

---

Begin implementing Step {current_step} now.
