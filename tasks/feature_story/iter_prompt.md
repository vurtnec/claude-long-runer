## Progress

### Completed Steps
{completed_steps_display}

---

## Current: Step {current_step} - {current_step_title}

### Tasks
{current_step_tasks_display}

### Acceptance Criteria
{current_step_acceptance_display}

---

Continue implementing the current step.

When done, run acceptance tests and output JSON:
```json
{{
  "action": "step_complete",
  "step": {current_step},
  "results": {{
    "tasks_completed": ["..."],
    "acceptance_passed": true
  }}
}}
```

## Acceptance Testing Rules

IMPORTANT - You MUST follow these rules:

- For browser acceptance tests, you MUST use the MCP browser tools DIRECTLY
  (e.g., mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot).
  Do NOT delegate browser testing to sub-agents via the Task tool — sub-agents
  do not have access to browser MCP tools.
- Do NOT use curl as a substitute for browser verification.
- If browser MCP tools are not available or fail, you MUST report step_failed.
- Only set "acceptance_passed": true if you have actually executed and passed
  ALL acceptance criteria.
