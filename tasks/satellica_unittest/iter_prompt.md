# Satellica 单元测试生成 - 迭代 {iteration}

## Progress

| Metric | Count |
|--------|-------|
| Total files | {total_files} |
| Pending | {pending_count} |
| Completed | {completed_count} |
| Skipped | {skipped_count} |
| Failed | {failed_count} |

## Current Batch ({batch_size} files)
{current_batch_display}

---

## Your Task

**你需要在这一轮内完成当前批次的所有工作，包括运行测试和修复问题。**

### Step 1: 为当前批次生成测试

1. 读取 Current Batch 中的每个源文件
2. 分析导出的函数/组件/Hook
3. 生成对应的测试文件（Happy Path，1-2 个用例）
4. 使用 Write 工具创建测试文件

### Step 2: 运行测试验证

使用 Bash 工具运行测试（**只测试当前批次新创建的文件**）：

```bash
cd {project_dir} && pnpm test:run <test-file-1> <test-file-2> <test-file-3>
```

例如：
```bash
cd {project_dir} && pnpm test:run components/app-logo.test.tsx components/app-sidebar.test.tsx
```

⚠️ **重要**：`pnpm test:run` 是非 watch 模式，会运行完毕后自动退出。

### Step 3: 修复失败的测试（如果有）

如果测试失败：
1. 分析错误信息（常见问题：Provider 缺失、mock 不完整、props 缺失）
2. 读取失败的测试文件
3. 使用 Edit 工具修复
4. **再次运行测试验证**
5. 重复直到所有测试通过

### Step 4: 输出 JSON 结果

**只有当所有测试通过后**，才输出 JSON：

```json
{{
  "action": "batch_complete",
  "results": [
    {{
      "source_file": "path/to/file.ts",
      "test_file": "path/to/file.test.ts",
      "status": "created",
      "test_count": 2
    }},
    {{
      "source_file": "path/to/another.tsx",
      "test_file": "path/to/another.test.tsx",
      "status": "skipped",
      "reason": "No testable exports"
    }}
  ]
}}
```

### If pending_count === 0:

所有文件已处理！输出完成信号：

```json
{{
  "action": "generation_complete",
  "summary": {{
    "total_files": {total_files},
    "completed": {completed_count},
    "skipped": {skipped_count},
    "failed": {failed_count}
  }}
}}
```

---

## ⚠️ 重要规则

1. **只处理当前批次的文件**: 不要处理 Current Batch 以外的文件
2. **必须运行测试验证**: 使用 `pnpm test:run <files>` 验证，不要跳过
3. **测试必须通过才能输出 JSON**: 如果测试失败，先修复再重试
4. **Happy Path 优先**: 不要写 edge case 测试，每个函数/组件 1-2 个用例就够
5. **使用项目 test-utils**:
   - 组件: `import {{ render, screen }} from '@/test-utils';`
   - Hook: `import {{ renderHook }} from '@testing-library/react';`
6. **测试文件命名**: `xxx.ts` → `xxx.test.ts`

---

## 测试代码模式

### 工具函数
```typescript
import {{ describe, it, expect }} from 'vitest';
import {{ functionName }} from './filename';

describe('filename', () => {{
  it('should do something', () => {{
    const result = functionName(input);
    expect(result).toBe(expected);
  }});
}});
```

### React Hook
```typescript
import {{ describe, it, expect }} from 'vitest';
import {{ renderHook }} from '@testing-library/react';
import {{ useHookName }} from './use-hook-name';

describe('useHookName', () => {{
  it('should return expected value', () => {{
    const {{ result }} = renderHook(() => useHookName());
    expect(result.current).toBeDefined();
  }});
}});
```

### React 组件
```typescript
import {{ describe, it, expect }} from 'vitest';
import {{ render, screen }} from '@/test-utils';
import {{ ComponentName }} from './component-name';

describe('ComponentName', () => {{
  it('should render correctly', () => {{
    render(<ComponentName />);
    expect(screen.getByRole('...')).toBeInTheDocument();
  }});
}});
```

---

## 跳过条件

以下情况可以 skip:
- 文件已有对应的 `.test.ts` / `.test.tsx`
- 文件只导出类型（纯 TypeScript types/interfaces）
- 文件是配置或常量，无函数逻辑
- 文件是 re-export（只有 `export * from` 或 `export {{ }} from`）

---

## 开始工作

请读取 Current Batch 中的文件，生成测试，然后输出 JSON 结果。
