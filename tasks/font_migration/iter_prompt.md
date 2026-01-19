# Font Migration - Iteration {iteration}

## Progress Summary
| Metric | Count |
|--------|-------|
| Total files | {total_files} |
| Pending | {pending_count} |
| Completed | {completed_count} |
| Skipped | {skipped_count} |
| Failed | {failed_count} |

## Current Batch ({batch_size} files)
{current_batch_display}

## Your Task

### If pending_count > 0:

For each file in the current batch:

1. **Read the file** - Check if it has system font declarations
2. **If NO font declarations** - Mark as skipped
3. **If HAS font declarations**:
   - Apply the font replacement patterns
   - Remove redundant `.fontWeight()` modifiers
4. **Build the project** - Verify no compilation errors
5. **Fix any errors** - If build fails, fix it or mark as failed

### Font Replacement Quick Reference

| Original | Replacement |
|----------|-------------|
| `.system(size: N)` | `.custom("JetBrainsMono-Regular", size: N)` |
| `.system(size: N, weight: .medium)` | `.custom("JetBrainsMono-Medium", size: N)` |
| `.system(size: N, weight: .semibold)` | `.custom("JetBrainsMono-Medium", size: N)` |
| `.system(size: N, weight: .bold)` | `.custom("JetBrainsMono-Bold", size: N)` |
| `.system(size: N, design: .monospaced)` | `.custom("JetBrainsMono-Regular", size: N)` |
| `.title` | `.custom("JetBrainsMono-Bold", size: 34)` |
| `.title2` | `.custom("JetBrainsMono-Bold", size: 28)` |
| `.title3` | `.custom("JetBrainsMono-Medium", size: 22)` |
| `.headline` | `.custom("JetBrainsMono-Medium", size: 17)` |
| `.subheadline` | `.custom("JetBrainsMono-Medium", size: 15)` |
| `.body` | `.custom("JetBrainsMono-Regular", size: 17)` |
| `.callout` | `.custom("JetBrainsMono-Regular", size: 16)` |
| `.caption` | `.custom("JetBrainsMono-Regular", size: 12)` |
| `.caption2` | `.custom("JetBrainsMono-Regular", size: 11)` |

### Important: Remove fontWeight Modifiers

After conversion, delete any `.fontWeight()` modifiers - the weight is now in the font name:

```swift
// WRONG - fontWeight is redundant
.font(.custom("JetBrainsMono-Bold", size: 16))
.fontWeight(.bold)  // DELETE THIS

// CORRECT
.font(.custom("JetBrainsMono-Bold", size: 16))
```

### Required Output

```json
{{
  "action": "batch_complete",
  "processed": ["file1.swift", "file2.swift", "file3.swift"],
  "succeeded": ["file1.swift"],
  "failed": ["file2.swift"],
  "skipped": ["file3.swift"],
  "fail_reasons": {{"file2.swift": "Build error: Cannot find type..."}},
  "skip_reasons": {{"file3.swift": "No font declarations found"}}
}}
```

### If pending_count === 0:

All files processed! Output:

```json
{{
  "action": "migration_complete",
  "summary": {{
    "total": {total_files},
    "succeeded": {completed_count},
    "skipped": {skipped_count},
    "failed": {failed_count}
  }}
}}
```

## Critical Rules

1. **ONLY process files in current_batch** - Do not touch other files
2. **Build after EACH batch** to verify changes compile
3. **Build MUST pass** - If it fails, either fix or mark as failed
4. **No font declarations = skipped** - Not failed
5. **Output JSON at the end** - Required for progress tracking

## Reference File
Already migrated (use as reference): `FilmMeter/UI/Screens/ContentView.swift`
