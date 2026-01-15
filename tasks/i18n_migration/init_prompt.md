# i18n Migration - Initialization

## Your Task
Initialize the automated i18n migration for the Satellica frontend project.

## Background
- Project path: {project_dir}
- Translation files: `frontend/messages/en.json`, `frontend/messages/zh.json`
- Using next-intl library with client-side locale management
- i18n infrastructure is already set up

## Reference Examples
The following files have been migrated to i18n and serve as your reference templates:
- `frontend/components/nav-user.tsx` - User navigation dropdown with translations
- `frontend/components/language-switcher.tsx` - Language switcher component

## This Iteration's Task: Scan Files to Process

1. Scan the following directories for .tsx files containing hardcoded Chinese or English user-visible strings:
   - `frontend/components/` (excluding `frontend/components/ui/`)
   - `frontend/modules/`
   - `frontend/app/`

2. Exclude the following:
   - `frontend/components/ui/` (shadcn UI components - do not modify)
   - Files already using `useTranslations` from next-intl
   - Pure utility/config files without user-visible text
   - Type definition files (.d.ts)

3. For each file, look for:
   - Text content in JSX elements: `<Button>Save</Button>`
   - Placeholder text: `placeholder="Enter name"`
   - Title/aria-label attributes: `title="Settings"`
   - Alert/toast messages
   - Error messages shown to users

4. Output format (JSON block):
```json
{{
  "action": "set_pending_files",
  "pending_files": ["components/nav-main.tsx", "modules/study/form.tsx", ...],
  "total_files": 150
}}
```

**Important**: Only output the file list. Do not start migrating files in this iteration.
**Note**: Use relative paths from the frontend directory (e.g., "components/nav-main.tsx" not full absolute paths).
