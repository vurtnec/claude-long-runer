# {name} - Batch Processing Task

## Objective
[Describe what this task will do to each file]

## Processing Rules
1. Process {batch_size} files per batch
2. For each file, perform [specific operation]
3. Output results in JSON format

## Output Format
After processing each batch, output a JSON block:

```json
{
  "action": "batch_complete",
  "results": [
    {"file": "path/to/file1", "status": "completed", "message": "Successfully processed"},
    {"file": "path/to/file2", "status": "failed", "message": "Error: reason"},
    {"file": "path/to/file3", "status": "skipped", "message": "Already up to date"}
  ]
}
```

### Status Values
- `completed`: File was successfully processed
- `failed`: Processing failed (will be retried or marked for review)
- `skipped`: File doesn't need processing (already done, not applicable, etc.)

## Current State
- Total files: {total_files}
- Pending: {pending_count}
- Completed: {completed_count}

## First Batch
Process these files now:
{current_batch_display}
