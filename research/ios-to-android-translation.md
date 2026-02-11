# iOS → Android 翻译方案调研：基于 Claude Long-Runner 的可行性分析

## 一、结论先行

**可行，但需要混合两种任务模板，并新增一个"分析阶段"。** 核心挑战不是翻译代码本身，而是如何将一个"语义密集、上下文相互依赖"的工程任务，拆解成 Long-Runner 能处理的"低耦合、可迭代"的子任务序列。

---

## 二、为什么直接翻译不可行

iOS 和 Android 之间的差异不仅是语言层面（Swift → Kotlin），更是框架范式的根本不同：

| 维度 | iOS | Android | 翻译难度 |
|------|-----|---------|----------|
| UI 框架 | UIKit / SwiftUI | Jetpack Compose / XML | **高** - 声明式 vs 命令式，生命周期完全不同 |
| 导航 | NavigationController / NavigationStack | Navigation Component / Fragment | **高** - 路由模型差异大 |
| 数据持久化 | CoreData / SwiftData | Room / DataStore | **中** - ORM 概念相似但 API 完全不同 |
| 网络层 | URLSession / Alamofire | Retrofit / OkHttp | **低** - 模式相似，可近乎直译 |
| 依赖注入 | 手动 / Swinject | Hilt / Koin | **中** |
| 异步模型 | async/await + Combine | Coroutines + Flow | **中** - 概念对应但语法不同 |
| 系统集成 | iOS SDK（推送、权限、相机等） | Android SDK | **高** - 平台特有 API |

**关键洞察：** 不能逐文件翻译，必须按"架构层"翻译。否则会出现循环依赖、类型不匹配等问题。

---

## 三、整体策略：三阶段混合迭代

```
┌─────────────────────────────────────────────────────────────────┐
│  阶段一：分析与规划 (Analysis)                                    │
│  使用: feature_story 模板                                        │
│  目标: 生成翻译 spec.yaml                                        │
│  迭代: 3-5 次                                                    │
├─────────────────────────────────────────────────────────────────┤
│  阶段二：架构层翻译 (Translation)                                 │
│  使用: feature_story 模板（多步骤）                                │
│  目标: 按架构层逐步翻译                                           │
│  迭代: 20-50 次（取决于 app 规模）                                 │
├─────────────────────────────────────────────────────────────────┤
│  阶段三：批量修正与测试 (Validation)                               │
│  使用: repetitive_work 模板                                      │
│  目标: 编译错误修复、测试补齐                                      │
│  迭代: 10-30 次                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 四、阶段一详细设计：分析与规划

### 目标
扫描 iOS 项目，生成结构化的翻译计划。

### 新建 task: `ios_analyzer`

**init_prompt.md 核心内容：**
```
你是一个 iOS → Android 翻译规划师。扫描项目 {project_dir} 并输出：

1. 项目架构分析
   - 使用的架构模式 (MVC/MVVM/VIPER/TCA)
   - 模块/Target 列表
   - 第三方依赖列表及 Android 对等库映射

2. 文件依赖图
   - 按模块分组的文件列表
   - 模块间的依赖关系

3. 翻译顺序建议
   - 从底层到上层：Models → Data → Domain → Presentation
   - 标注每个模块的翻译复杂度 (low/medium/high)

4. 生成 translation_spec.yaml
```

**processor.py 核心逻辑：**
- 解析 agent 输出的 JSON
- 自动生成 `translation_spec.yaml`（作为阶段二的输入）
- 记录 iOS 项目中的文件清单和依赖图

### 输出产物
```yaml
# translation_spec.yaml (自动生成)
project_name: "MyiOSApp → Android"
source_platform: ios
target_platform: android

dependency_mapping:
  Alamofire: "com.squareup.retrofit2:retrofit:2.9.0"
  Kingfisher: "io.coil-kt:coil-compose:2.4.0"
  SnapKit: "N/A (use Compose constraints)"

architecture_mapping:
  pattern: MVVM
  ios_structure: "View → ViewModel → Repository → API"
  android_structure: "Composable → ViewModel → Repository → API"

translation_order:
  - layer: "models"
    files: ["User.swift", "Product.swift", ...]
    complexity: low
    android_package: "com.app.data.model"

  - layer: "network"
    files: ["APIClient.swift", "Endpoints.swift", ...]
    complexity: medium
    android_package: "com.app.data.remote"

  - layer: "repository"
    files: ["UserRepository.swift", ...]
    complexity: medium
    android_package: "com.app.data.repository"

  - layer: "viewmodel"
    files: ["HomeViewModel.swift", ...]
    complexity: medium
    android_package: "com.app.presentation.viewmodel"

  - layer: "ui"
    files: ["HomeView.swift", "ProfileView.swift", ...]
    complexity: high
    android_package: "com.app.presentation.ui"
```

---

## 五、阶段二详细设计：架构层翻译

### 使用 `feature_story` 模板

每个架构层作为一个 step，在 spec.yaml 中定义。

**关键设计：每个 step 内部使用 batch 思维**

```yaml
implementation_steps:
  - step: 1
    title: "初始化 Android 项目骨架"
    tasks:
      - "创建 Kotlin + Jetpack Compose 项目"
      - "配置 build.gradle 依赖"
      - "建立包结构"
    acceptance:
      - type: code
        command: "./gradlew assembleDebug"
        expected: "BUILD SUCCESSFUL"

  - step: 2
    title: "翻译 Models 层"
    tasks:
      - "翻译 User.swift → User.kt"
      - "翻译 Product.swift → Product.kt"
      - "添加 Room Entity 注解"
    acceptance:
      - type: code
        command: "./gradlew compileDebugKotlin"

  - step: 3
    title: "翻译 Network 层"
    tasks:
      - "创建 Retrofit 接口定义"
      - "翻译 APIClient → RetrofitClient"
      - "翻译 Endpoints → API interface"
    acceptance:
      - type: code
        command: "./gradlew compileDebugKotlin"

  - step: 4
    title: "翻译 Repository 层"
    tasks:
      - "翻译 UserRepository"
      - "将 Combine Publisher 转为 Kotlin Flow"
    acceptance:
      - type: code
        command: "./gradlew compileDebugKotlin"

  - step: 5
    title: "翻译 ViewModel 层"
    tasks:
      - "翻译各 ViewModel"
      - "将 @Published 转为 StateFlow"
      - "将 Combine 管道转为 Flow 操作符"
    acceptance:
      - type: code
        command: "./gradlew compileDebugKotlin"

  - step: 6
    title: "翻译 UI 层"
    tasks:
      - "翻译 SwiftUI View → Composable"
      - "翻译导航结构"
      - "处理 iOS 特有的 UI 模式"
    acceptance:
      - type: code
        command: "./gradlew assembleDebug"
      - type: browser
        url: "http://localhost:8081"  # Android Emulator web view
```

### iter_prompt.md 中的关键上下文

每次迭代需要注入：
```
当前正在翻译的层: {current_step_title}
已完成的层: {completed_steps_display}
iOS 源文件列表: {current_ios_files}
对应的 Android 目标路径: {current_android_targets}
依赖映射表: {dependency_mapping}

重要规则：
1. 保持与 iOS 版本相同的业务逻辑
2. 使用 Android/Kotlin 惯用写法，不要写 "Swift 风格的 Kotlin"
3. 每次翻译后必须通过编译
```

---

## 六、阶段三详细设计：批量修正

### 使用 `repetitive_work` 模板

**场景 A：编译错误批量修复**
```json
{
  "task_name": "android_compile_fix",
  "batch_size": 5,
  "file_pattern": "*.kt"
}
```
- 运行 `./gradlew compileDebugKotlin` 收集错误
- 按文件分组错误
- 每 batch 修复 5 个文件的错误
- 直到编译通过

**场景 B：单元测试补齐**
```json
{
  "task_name": "android_test_migration",
  "batch_size": 3,
  "file_pattern": "*Test.swift"
}
- 发现 iOS 测试文件
- 每 batch 翻译 3 个测试文件
- 验证 `./gradlew test`

---

## 七、需要对框架做的扩展

### 1. 新增 `ios_analyzer` task 类型
- processor.py: 解析 iOS 项目结构，生成 translation_spec.yaml
- 需要支持读取 .xcodeproj / Package.swift

### 2. 增强 feature_story processor
- 支持 step 内部的文件列表批量处理
- 支持从上一阶段的 translation_spec.yaml 自动加载配置
- 支持"编译验证"作为 acceptance criteria 的一等公民

### 3. 新增 cross-reference 上下文机制
- 当前框架每次迭代是全新 context，但翻译需要参考 iOS 源码
- 方案：在 iter_prompt 中注入当前要翻译的 iOS 文件内容
- 需要注意 token 限制 — 大文件需要拆分

### 4. 安全策略更新 (security.py)
- 允许 `gradle`, `./gradlew` 命令
- 允许 Android SDK 相关工具
- 允许 `adb` 命令（如需模拟器验证）

---

## 八、预估规模与迭代次数

| App 规模 | iOS 文件数 | 预估迭代次数 | 预估阶段 |
|----------|-----------|-------------|---------|
| 小型 (10-30 文件) | ~20 | 15-25 次 | 可一阶段完成 |
| 中型 (30-100 文件) | ~60 | 40-80 次 | 需完整三阶段 |
| 大型 (100+ 文件) | 150+ | 100-200 次 | 需要分模块多轮 |

### Token 消耗估算
- 每次迭代约消耗 50K-150K tokens（输入 + 输出）
- 中型项目完整翻译：约 400 万 - 1200 万 tokens
- 成本取决于使用的模型（Sonnet vs Opus）

---

## 九、核心风险与缓解措施

### 风险 1：上下文丢失
**问题：** 每次迭代独立 context，agent 不记得之前写了什么
**缓解：**
- state 中维护"已翻译文件映射表"
- iter_prompt 中注入关键上下文摘要
- 利用文件系统本身作为"记忆"——agent 可以 read 已翻译的文件

### 风险 2：编译错误级联
**问题：** 翻译到中间层时，编译错误可能因缺少上层代码而无法修复
**缓解：**
- 自底向上翻译（models → network → repository → viewmodel → ui）
- 每层翻译完必须编译通过
- 必要时先创建 stub/interface 占位

### 风险 3：iOS 特有模式无法直译
**问题：** 某些 iOS 模式（如 UIKit Delegate、Storyboard、CoreData NSManagedObject）没有直接对应
**缓解：**
- 在分析阶段识别这些模式并标记
- 在 spec.yaml 中为这些文件提供翻译指南（不是逐行翻译，而是给出目标模式）
- 示例：`UITableViewDelegate → LazyColumn with Composable items`

### 风险 4：迭代次数失控
**问题：** 大型项目可能需要数百次迭代
**缓解：**
- 使用 `--resume` 支持中断恢复
- 按模块拆分为多个独立任务
- 设定每阶段的 max-iterations 上限

---

## 十、推荐执行流程

```bash
# 阶段一：分析 iOS 项目（3-5 次迭代）
python long_run_executor.py \
  --task ios_analyzer \
  --project-dir /path/to/ios-app \
  --max-iterations 5

# 阶段二：架构层翻译（20-50 次迭代）
python long_run_executor.py \
  --task feature_story \
  --project-dir /path/to/android-app \
  --params '{"ios_source_dir": "/path/to/ios-app", "spec_file": "translation_spec.yaml"}' \
  --max-iterations 50

# 阶段三：编译修复（10-30 次迭代）
python long_run_executor.py \
  --task repetitive_work \
  --project-dir /path/to/android-app \
  --params '{"file_pattern": "*.kt", "task_type": "compile_fix"}' \
  --max-iterations 30

# 如果中断，可恢复
python long_run_executor.py \
  --task feature_story \
  --project-dir /path/to/android-app \
  --resume
```

---

## 十一、总结

| 方面 | 评估 |
|------|------|
| **技术可行性** | ✅ 可行 — 框架的迭代机制 + 状态持久化天然适合这种长任务 |
| **框架适配度** | ⚠️ 需要扩展 — 需新增 ios_analyzer task，增强 context 注入 |
| **质量预期** | ⚠️ 中等 — 80% 代码可自动翻译，20% 需要人工调整（特别是 UI 和平台特有功能）|
| **最大挑战** | 上下文管理 — 如何在无限迭代中保持对全局架构的理解 |
| **建议起步** | 先用一个小型 iOS 项目（<20 文件）验证完整流程 |
