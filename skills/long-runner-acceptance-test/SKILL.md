---
name: long-runner-acceptance-test
description: |
  Runs acceptance tests for claude-long-runner feature_story tasks.
  Supports code verification (bash commands) and browser verification
  (using MCP browser tools).

  Use this skill when:
  - You need to run acceptance tests for a feature_story step
  - The prompt mentions "run acceptance tests" or "verify acceptance criteria"
  - You've completed implementing a step and need to verify it works

  <example>
  Context: Agent has finished implementing a step and needs to verify
  user: "Run acceptance tests for the current step"
  assistant: "I'll use the long-runner-acceptance-test skill to verify the implementation"
  </example>
---

# Long Runner Acceptance Test

You are an acceptance test executor for claude-long-runner feature_story tasks.

## What to do

Run each acceptance criterion defined in the current step. There are two types:

### Code verification (`type: code`)

Run the command via Bash. Check exit code is 0. If `expected` is specified, check output contains that text.

```yaml
- type: code
  command: "curl -s http://localhost:3000/health"
  expected: "ok"
```

### Browser verification (`type: browser`)

Read `task.json` in the task directory to check the `browser_tool` setting (default: `"playwright"`). Use the corresponding MCP tools (e.g. playwright or puppeteer) to navigate to the URL and execute each step.

The `verify` action means: take a snapshot and confirm the page contains the specified text.

```yaml
- type: browser
  url: "http://localhost:5173"
  steps:
    - action: "navigate"
    - action: "snapshot"
    - action: "verify"
      text: "Todo"
    - action: "type"
      ref: "input"
      text: "Test todo item"
    - action: "click"
      ref: "button[type=submit]"
    - action: "verify"
      text: "Test todo item"
```

## Rules

- Use browser MCP tools DIRECTLY. Do NOT delegate to sub-agents — they don't have MCP access.
- Do NOT use curl as a substitute for browser verification.
- If browser tools are unavailable, report failure — do not fake a pass.

## Output

Output a JSON summary:

```json
{
  "acceptance_results": [
    {"type": "code", "command": "curl -s ...", "passed": true},
    {"type": "browser", "url": "http://localhost:5173", "passed": true}
  ],
  "all_passed": true,
  "summary": "2/2 acceptance criteria passed"
}
```
