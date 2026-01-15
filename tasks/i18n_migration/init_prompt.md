# i18n Migration - Initialization

## Project Context
- Project: Satellica Frontend
- Working directory: {project_dir}
- Translation files: `messages/en.json`, `messages/zh.json`
- Library: next-intl (already configured)
- Total files to process: {total_files}

## Reference Examples
The following files have been migrated and serve as templates:
- `components/nav-user.tsx` - Uses `useTranslations('auth')` for account settings text
- `components/language-switcher.tsx` - Language dropdown component

## Your Task

This is the first iteration. The file list has been pre-loaded with {pending_count} files.

1. **Prepare the first batch**: Take the first {batch_size} files from `pending_files`
2. **Review each file**: Check if it contains user-visible hardcoded text that needs i18n
3. **For files WITH hardcoded text**: Migrate them following the standards below
4. **For files WITHOUT hardcoded text**: Mark as skipped (no changes needed)
5. **Verify**: Run `pnpm build` and `pnpm lint` in the frontend directory

## Migration Standards

```tsx
// Before
<Button>Save Changes</Button>
<Input placeholder="Enter email" />

// After
'use client';
import {{ useTranslations }} from 'next-intl';

export function MyComponent() {{
  const t = useTranslations('namespace');
  return (
    <>
      <Button>{{t('saveChanges')}}</Button>
      <Input placeholder={{t('emailPlaceholder')}} />
    </>
  );
}}
```

## Translation Key Naming
- Use camelCase: `saveChanges`, `emailPlaceholder`
- Group by feature: `account.title`, `study.create`, `common.save`
- Keep keys concise but descriptive

## Required Output

After processing the batch, output this JSON:

```json
{{
  "action": "batch_complete",
  "processed": ["file1.tsx", "file2.tsx", "file3.tsx"],
  "succeeded": ["file1.tsx", "file2.tsx"],
  "failed": [],
  "skipped": ["file3.tsx"],
  "fail_reasons": {{}},
  "skip_reasons": {{"file3.tsx": "No user-visible text"}}
}}
```

## Important Rules
1. **Only process files in the current batch**
2. **Run `pnpm build` and `pnpm lint`** after modifications - files must pass both
3. **If build/lint fails**: Fix the issue or mark as failed
4. **Files without hardcoded text**: Mark as skipped, not failed
5. **Always output the JSON block** at the end
