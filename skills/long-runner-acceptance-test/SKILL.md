---
name: long-runner-acceptance-test
description: |
  Runs acceptance tests for claude-long-runner feature_story tasks.
  Supports code verification (bash commands) and browser verification
  (using configurable MCP browser tools like Playwright, BrowserMCP, etc.).

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

## Overview

This skill runs acceptance tests defined in the current step's `acceptance` criteria. It supports two types of verification:

1. **Code verification** (`type: code`) - Run bash commands and check exit codes
2. **Browser verification** (`type: browser`) - Use MCP browser tools to interact with web pages

## Step 1: Read Configuration

First, read the task configuration to determine which browser tool to use:

1. Read `task.json` in the task directory to get `browser_tool` setting
2. Read the current step's acceptance criteria from the state file

### Browser Tool Mapping

| browser_tool | MCP Tool Prefix |
|--------------|-----------------|
| `playwright` (default) | `mcp__playwright__browser_` |
| `browsermcp` | `mcp__browsermcp__browser_` |
| `browser-tool` | `mcp__browser-tool__` |

## Step 2: Execute Code Tests

For each acceptance criterion with `type: code`:

```yaml
- type: code
  command: "npm test"
  expected: "ok"  # optional
```

Execute the command using Bash tool and check:
- Exit code is 0 (success)
- If `expected` is specified, output contains the expected text

Report result:
```json
{
  "type": "code",
  "command": "npm test",
  "passed": true,
  "output": "All tests passed"
}
```

## Step 3: Execute Browser Tests

For each acceptance criterion with `type: browser`:

```yaml
- type: browser
  url: "http://localhost:5173"
  steps:
    - action: "navigate"
    - action: "snapshot"
    - action: "click"
      ref: "button[type=submit]"
    - action: "type"
      ref: "input[name=email]"
      text: "test@example.com"
    - action: "verify"
      text: "Success"
```

### Action Mapping

Based on the `browser_tool` configuration, map actions to MCP tool calls:

| Action | playwright | browsermcp |
|--------|-----------|------------|
| navigate | `mcp__playwright__browser_navigate` | `mcp__browsermcp__browser_navigate` |
| snapshot | `mcp__playwright__browser_snapshot` | `mcp__browsermcp__browser_snapshot` |
| click | `mcp__playwright__browser_click` | `mcp__browsermcp__browser_click` |
| type | `mcp__playwright__browser_type` | `mcp__browsermcp__browser_type` |
| hover | `mcp__playwright__browser_hover` | `mcp__browsermcp__browser_hover` |
| select | `mcp__playwright__browser_select_option` | `mcp__browsermcp__browser_select_option` |
| wait | `mcp__playwright__browser_wait_for` | `mcp__browsermcp__browser_wait` |

### Verify Action

The `verify` action is special - it checks that the page snapshot contains the expected text:

1. Take a snapshot using the appropriate tool
2. Check if the snapshot contains the specified `text`
3. Report pass/fail based on presence of text

## Step 4: Report Results

After running all acceptance tests, output a JSON summary:

```json
{
  "acceptance_results": [
    {
      "type": "code",
      "command": "npm test",
      "passed": true,
      "output": "All tests passed"
    },
    {
      "type": "browser",
      "url": "http://localhost:5173",
      "passed": true,
      "steps_completed": 4,
      "notes": "All browser steps completed successfully"
    }
  ],
  "all_passed": true,
  "summary": "2/2 acceptance criteria passed"
}
```

## Error Handling

### Code Test Failures
- Report the command, exit code, and error output
- Continue with remaining tests

### Browser Test Failures
- Report which step failed and why
- Take a screenshot if possible for debugging
- Continue with remaining tests if the browser is still usable

### Common Issues

1. **Server not running**: If browser tests fail to connect, remind the user to start the dev server
2. **Element not found**: If click/type fails, take a snapshot to help debug selectors
3. **Timeout**: If actions take too long, report timeout and suggest increasing wait times

## Usage in Prompts

In `init_prompt.md` or `iter_prompt.md`, include:

```markdown
## Acceptance Testing

After implementing all tasks, run acceptance tests:

Use `/long-runner-acceptance-test` to verify your implementation.

The skill will:
1. Run code tests (commands)
2. Run browser tests (if configured)
3. Report pass/fail for each criterion
```
