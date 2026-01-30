# Feature Story: [Feature Name]

## Overview
[Brief description of the feature being implemented]

## Feature List

### Feature 1: [Name]
**Description**: [What this feature does]
**Acceptance Criteria**:
- [ ] Criterion 1
- [ ] Criterion 2

### Feature 2: [Name]
**Description**: [What this feature does]
**Acceptance Criteria**:
- [ ] Criterion 1
- [ ] Criterion 2

### Feature 3: [Name]
**Description**: [What this feature does]
**Acceptance Criteria**:
- [ ] Criterion 1
- [ ] Criterion 2

---

## Verification Methods

### Method A: Code Verification
Run these commands to verify:
```bash
# Run tests
npm test
# or
pytest

# Type check
npm run typecheck
# or
mypy .

# Lint
npm run lint
# or
ruff check .
```

### Method B: Browser Verification
Use Playwright MCP tools:
1. `mcp__playwright__browser_navigate` - Open the page
2. `mcp__playwright__browser_snapshot` - Get accessibility snapshot
3. `mcp__playwright__browser_click` - Interact with elements
4. Verify expected behavior

---

## Completion Signal
When ALL features are implemented and verified, output:
```
ALL_FEATURES_COMPLETED_AND_VERIFIED
```

---

## Start Implementation
Begin with Feature 1. Implement it fully, then verify using the appropriate method before moving to the next feature.
