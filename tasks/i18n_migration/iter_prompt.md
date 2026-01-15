# i18n Migration - Iteration {iteration}

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

1. **Read the file** - Check if it has user-visible hardcoded text
2. **If NO hardcoded text** - Mark as skipped
3. **If HAS hardcoded text**:
   - Extract all user-visible strings
   - Add translations to `messages/en.json` and `messages/zh.json`
   - Import `useTranslations` and replace hardcoded text
4. **Verify** - Run `pnpm build` and `pnpm lint` in frontend/
5. **Fix any errors** - If build/lint fails, fix it or mark as failed

### Migration Pattern

```tsx
// Before migration
export function MyComponent() {{
  return (
    <div>
      <h1>Welcome to Dashboard</h1>
      <Button>Save Changes</Button>
      <Input placeholder="Enter your email" />
    </div>
  );
}}

// After migration
'use client';
import {{ useTranslations }} from 'next-intl';

export function MyComponent() {{
  const t = useTranslations('dashboard');
  return (
    <div>
      <h1>{{t('welcome')}}</h1>
      <Button>{{t('saveChanges')}}</Button>
      <Input placeholder={{t('emailPlaceholder')}} />
    </div>
  );
}}
```

### Translation Files Format

```json
// messages/en.json - ADD to existing content
{{
  "dashboard": {{
    "welcome": "Welcome to Dashboard",
    "saveChanges": "Save Changes",
    "emailPlaceholder": "Enter your email"
  }}
}}

// messages/zh.json - ADD to existing content
{{
  "dashboard": {{
    "welcome": "欢迎来到仪表盘",
    "saveChanges": "保存更改",
    "emailPlaceholder": "请输入邮箱"
  }}
}}
```

### Namespace Guidelines
- `common` - Shared text (Save, Cancel, Delete, Loading, etc.)
- `auth` - Authentication (Sign In, Sign Out, etc.)
- `account` - Account settings
- `study` - Study-related
- `interview` - Interview module
- `recruitment` - Recruitment module
- `admin` - Admin pages

### Required Output

```json
{{
  "action": "batch_complete",
  "processed": ["file1.tsx", "file2.tsx", "file3.tsx"],
  "succeeded": ["file1.tsx"],
  "failed": ["file2.tsx"],
  "skipped": ["file3.tsx"],
  "fail_reasons": {{"file2.tsx": "Build error: Cannot find module..."}},
  "skip_reasons": {{"file3.tsx": "No user-visible text"}}
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
2. **Run verification after EACH batch**:
   ```bash
   cd frontend && pnpm build && pnpm lint
   ```
3. **Build/lint MUST pass** - If they fail, either fix or mark as failed
4. **No hardcoded text = skipped** - Not failed
5. **Preserve existing translations** - Merge, don't overwrite
6. **Output JSON at the end** - Required for progress tracking

## Reference Files
Already migrated (use as examples):
- `components/nav-user.tsx` - Uses `useTranslations('auth')`
