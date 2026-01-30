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

When done, output JSON:
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
