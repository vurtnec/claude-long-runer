# i18n Migration - Iteration {iteration}

## Current Progress
- Total files: {total_files}
- Pending: {pending_count} files
- Completed: {completed_count} files
- Failed: {failed_count} files
- Batch size: {batch_size}

## Files to Process This Iteration
{current_batch_display}

## Your Task

### If pending_count > 0:

For each file in the current batch:

1. **Read the file** - Understand the component structure
2. **Extract strings** - Find all user-visible hardcoded text
3. **Add translations** - Update `frontend/messages/en.json` and `frontend/messages/zh.json`
4. **Migrate the component** - Import useTranslations, replace hardcoded text
5. **Verify** - Ensure TypeScript compiles without errors

### Migration Standards

```tsx
// Before migration
<Button>Save Changes</Button>
<Input placeholder="Enter your email" />
<span>No items found</span>

// After migration
import {{ useTranslations }} from 'next-intl';

export function MyComponent() {{
  const t = useTranslations('myComponent');

  return (
    <>
      <Button>{{t('saveChanges')}}</Button>
      <Input placeholder={{t('emailPlaceholder')}} />
      <span>{{t('noItemsFound')}}</span>
    </>
  );
}}
```

### Translation Key Naming Convention
- Use camelCase: `saveChanges`, `emailPlaceholder`
- Group by namespace: `nav.dashboard`, `study.createNew`, `common.save`
- Keep keys concise but meaningful
- Use existing namespaces when appropriate:
  - `nav` - Navigation items
  - `common` - Shared/reusable text (Save, Cancel, Delete, etc.)
  - `auth` - Authentication related
  - Create new namespaces for specific modules (e.g., `study`, `participant`)

### Translation File Format
When adding translations, merge into existing JSON structure:

```json
// frontend/messages/en.json
{{
  "existing": "...",
  "newNamespace": {{
    "newKey": "English text"
  }}
}}

// frontend/messages/zh.json
{{
  "existing": "...",
  "newNamespace": {{
    "newKey": "Chinese translation"
  }}
}}
```

### Output After Completing This Batch

```json
{{
  "action": "batch_complete",
  "processed": ["file1.tsx", "file2.tsx", ...],
  "succeeded": ["file1.tsx", ...],
  "failed": ["file2.tsx"],
  "fail_reasons": {{"file2.tsx": "Complex dynamic string concatenation - needs manual review"}}
}}
```

### If pending_count === 0:

All files have been processed! Output:

```json
{{
  "action": "migration_complete",
  "summary": {{
    "total": {total_files},
    "succeeded": {completed_count},
    "failed": {failed_count}
  }}
}}
```

## Reference: Already Migrated Files
Look at these for patterns:
- `frontend/components/nav-user.tsx` - useTranslations('auth')
- `frontend/components/language-switcher.tsx` - Language dropdown

## Important Reminders
1. **Only process files in the current batch** - do not touch other files
2. **Skip failed files** - mark them as failed and move on, do not retry
3. **Keep translation files valid JSON** - be careful with commas and brackets
4. **Translate accurately** - provide proper Chinese translations, not just pinyin
5. **Run `pnpm tsc --noEmit` in frontend/** to verify after modifications
6. **Always output the JSON block** at the end of your response
