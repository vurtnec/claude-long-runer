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
   - For `type: code` acceptance: run the specified command via Bash and check the result
   - For `type: browser` acceptance: use MCP browser tools directly (see rules below)
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

## Acceptance Testing Rules

IMPORTANT - You MUST follow these rules:

- For browser acceptance tests, you MUST use the MCP browser tools DIRECTLY
  (e.g., mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot).
  Do NOT delegate browser testing to sub-agents via the Task tool — sub-agents
  do not have access to browser MCP tools.
- Do NOT use curl as a substitute for browser verification. curl cannot execute
  JavaScript or detect CSS/build errors.
- If browser MCP tools are not available or fail, you MUST report step_failed.
  Do NOT claim acceptance_passed: true without actually running browser tests.
- Only set "acceptance_passed": true if you have actually executed and passed
  ALL acceptance criteria listed above.

---

Begin implementing Step {current_step} now.
