# Font Migration - Initialization

## Project Context
- Project: FilmMeter iOS App
- Working directory: {project_dir}
- Total files to process: {total_files}
- Reference file (already converted): `FilmMeter/UI/Screens/ContentView.swift`

## Overview

You are migrating FilmMeter's SwiftUI views from system fonts to JetBrains Mono custom fonts. This creates a consistent, professional monospace aesthetic throughout the app.

## Font Family

The app uses **JetBrains Mono** with three weights:
- `JetBrainsMono-Regular` - Default weight
- `JetBrainsMono-Medium` - Semi-bold equivalent
- `JetBrainsMono-Bold` - Bold weight

## Your Task

This is the first iteration. The file list has been pre-loaded with {pending_count} files.

1. **Prepare the first batch**: Take the first {batch_size} files from `pending_files`
2. **Review each file**: Check if it contains font declarations that need migration
3. **For files WITH system fonts**: Migrate them following the patterns below
4. **For files WITHOUT system fonts**: Mark as skipped (no changes needed)
5. **Verify**: Build the project to ensure no compilation errors

## Font Replacement Patterns

### Pattern 1: Basic System Font
```swift
// Before
.font(.system(size: 16))

// After
.font(.custom("JetBrainsMono-Regular", size: 16))
```

### Pattern 2: System Font with Weight (Semibold/Medium)
```swift
// Before
.font(.system(size: 14, weight: .semibold))
.font(.system(size: 14, weight: .medium))

// After
.font(.custom("JetBrainsMono-Medium", size: 14))
```

### Pattern 3: System Font with Bold Weight
```swift
// Before
.font(.system(size: 18, weight: .bold))

// After
.font(.custom("JetBrainsMono-Bold", size: 18))
```

### Pattern 4: Monospaced Design (already monospace intent)
```swift
// Before
.font(.system(size: 12, design: .monospaced))

// After
.font(.custom("JetBrainsMono-Regular", size: 12))
```

### Pattern 5: Semantic Fonts - Must convert to specific sizes

| Semantic Font | Size | Weight | Replacement |
|---------------|------|--------|-------------|
| `.title` | 34 | Bold | `.custom("JetBrainsMono-Bold", size: 34)` |
| `.title2` | 28 | Bold | `.custom("JetBrainsMono-Bold", size: 28)` |
| `.title3` | 22 | Medium | `.custom("JetBrainsMono-Medium", size: 22)` |
| `.headline` | 17 | Medium | `.custom("JetBrainsMono-Medium", size: 17)` |
| `.subheadline` | 15 | Medium | `.custom("JetBrainsMono-Medium", size: 15)` |
| `.body` | 17 | Regular | `.custom("JetBrainsMono-Regular", size: 17)` |
| `.callout` | 16 | Regular | `.custom("JetBrainsMono-Regular", size: 16)` |
| `.caption` | 12 | Regular | `.custom("JetBrainsMono-Regular", size: 12)` |
| `.caption2` | 11 | Regular | `.custom("JetBrainsMono-Regular", size: 11)` |

```swift
// Before
.font(.title)
.font(.headline)
.font(.body)
.font(.caption)

// After
.font(.custom("JetBrainsMono-Bold", size: 34))
.font(.custom("JetBrainsMono-Medium", size: 17))
.font(.custom("JetBrainsMono-Regular", size: 17))
.font(.custom("JetBrainsMono-Regular", size: 12))
```

### Pattern 6: Remove Redundant fontWeight Modifiers

After converting to custom fonts, remove any `.fontWeight()` modifiers as the weight is now embedded in the font name:

```swift
// Before
.font(.system(size: 16))
.fontWeight(.bold)

// After (remove fontWeight entirely)
.font(.custom("JetBrainsMono-Bold", size: 16))
```

## Reference Example

Study `FilmMeter/UI/Screens/ContentView.swift` - it has already been correctly migrated and shows the proper patterns.

## Required Output

After processing the batch, output this JSON:

```json
{{
  "action": "batch_complete",
  "processed": ["file1.swift", "file2.swift", "file3.swift"],
  "succeeded": ["file1.swift", "file2.swift"],
  "failed": [],
  "skipped": ["file3.swift"],
  "fail_reasons": {{}},
  "skip_reasons": {{"file3.swift": "No font declarations found"}}
}}
```

## Important Rules

1. **Only process files in the current batch**
2. **Build the project** after modifications to verify no errors
3. **If build fails**: Fix the issue or mark as failed
4. **Files without font declarations**: Mark as skipped, not failed
5. **Always output the JSON block** at the end
6. **Reference ContentView.swift** for correct pattern examples
