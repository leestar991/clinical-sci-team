---
name: ppt-svg-generator
description: >
  Stage 2 of the PPT pipeline — reads design_spec.md + spec_lock.md from Stage 1, acquires images (Step 5), generates per-page SVG hand-written from spec_lock + <lib> template SVGs (Step 6 Executor), runs quality check, then generates speaker notes to notes/total.md. Covers ppt-master Steps 5–6. Use when downstream of ppt-outline-generator; triggers on '生成SVG', 'SVG幻灯片', 'Executor', 'generate SVG', 'SVG PPT'.
---

# PPT SVG Generator (Stage 2 — Step 5–6)

将 Stage 1（`ppt-outline-generator`）产出的 `design_spec.md` + `spec_lock.md` 转化为逐页 SVG + 演讲者备注。涵盖 ppt-master Step 5（配图获取）和 Step 6（Executor 逐页 SVG 生成 + 质检 + 备注）。

**职责边界**：本技能生成自包含的逐页 SVG（插图已内联）和演讲者备注。不产出最终 PPTX —— 那是 Stage 3（`ppt-svg-to-pptx`）的工作。

> [!CAUTION]
> ## 🚨 Global Execution Discipline (MANDATORY)

> 1. **SERIAL EXECUTION** — Step 5 → Step 6（含 Visual Construction → Quality Check → Logic Construction）必须串行
> 2. **SPEC_LOCK ONCE AT START** — 生成第一页前一次性读取 `spec_lock.md`，提取 per-page 参数映射写入上下文；后续每页从上下文取值，**禁止重读文件**
> 3. **SVG MUST BE HAND-WRITTEN** — 每页 SVG 单独手写设计，禁止脚本批量生成
> 4. **BATCH READ LAYOUTS/CHARTS** — 开始生成前一次性读完所有 `spec_lock.page_layouts` 和 `spec_lock.page_charts` 引用的模版 SVG
> 5. **QUALITY CHECK GATE** — `svg_quality_checker.py` 必须退出码 0 才能进入备注生成
> 6. **FILE PRESENT SINGLE PAGE** — 每页 SVG 生成完成后仅展示当前页文件名，**禁止输出累计文件清单**；进度由 TaskCreate/TaskUpdate 追踪
> 7. **USER EDITING PHASE** — 质检通过后进入专用编辑阶段，有明确的阶段 banner 和退出条件
> 8. **PPTX CONFIRMATION GATE** — 备注生成完成后必须询问用户是否继续 PPTX 导出，禁止自动进入 Stage 3
> 9. **TODO-DRIVEN PROGRESS** — 使用 TaskCreate/TaskUpdate 跟踪每页生成进度，替代累计文件列表展示
> 10. **MINIMAL TOOL RESULTS** — 禁止在单次 run 中 read_file 超过 2 个参考文件；优先使用 skill 加载时已提供的上下文和内联约束
> 11. **SPLIT OUTPUTS** — 演讲者备注按页生成，每页一条独立消息；禁止单条消息输出全部备注

> [!IMPORTANT]
> ## 🗂️ Path Convention — deer-flow Sandbox

> | Symbol | Absolute Path | Notes |
> |--------|--------------|-------|
> | `<project_path>` | `/mnt/user-data/workspace` | Pre-existing deer-flow session working directory |
> | `<skill_dir>` | `/mnt/skills/custom/ppt-svg-generator` | This skill's install location |
> | `<lib>` | `/mnt/skills/custom/ppt-template-design/templates` | 统一模版库（只读引用），由 `ppt-template-design` 提供 |
> | Input spec | `/mnt/user-data/workspace/design_spec.md` | Stage 1 产物 — 人类可读设计叙事 |
> | Input lock | `/mnt/user-data/workspace/spec_lock.md` | Stage 1 产物 — 机器可读执行合约 |
> | SVG output | `/mnt/user-data/workspace/svg_output/` | 逐页最终 SVG（`NN_页名.svg`） |
> | Speaker notes | `/mnt/user-data/workspace/notes/total.md` | 演讲者备注（合并稿） |
> | Images | `/mnt/user-data/workspace/images/` | 配图（AI 生成/网页搜索/用户提供/公式渲染） |

> ⚠️ **Bash relative-path rule (MANDATORY)**: 所有 bash 命令必须先 `cd /mnt/user-data/workspace`，再用相对路径（`sources/`, `images/`, `svg_output/`, `notes/`）。严禁硬编码 `/mnt/user-data/workspace/...`。仅 `<skill_dir>/scripts/...` 和 `<lib>/...` 保持绝对路径。

## Main Scripts

| Script | Purpose |
|--------|---------|
| `<skill_dir>/scripts/image_gen.py` | AI image generation (multi-provider, manifest-driven) |
| `<skill_dir>/scripts/image_search.py` | Web image search |
| `<skill_dir>/scripts/svg_quality_checker.py` | SVG quality check (hard gate, exit code 0) |
| `<skill_dir>/scripts/analyze_images.py` | Image analysis |

For complete tool documentation, see `<skill_dir>/scripts/README.md`.

---

## 输入

本技能从 Stage 1（`ppt-outline-generator`）接收：

- `design_spec.md` — 十一节完整设计规格，含 §VIII Image Resource List 和 §IX Outline（页 stem 集）
- `spec_lock.md` — 机器可读执行合约，含 `page_layouts`、`page_charts`、`images`、每页 `page_rhythm`

**页 stem 约定**：Stage 1 在 `spec_lock.md §IX Outline` 中为每页确定唯一的 stem（`NN_页名`，如 `01_封面` / `02_爱马仕家居`）。本技能产出的 SVG 文件名与之一致：`svg_output/NN_页名.svg`。

---

## Workflow

### Step 5: Image Acquisition Phase (Conditional)

🚧 **GATE**: Stage 1 complete; `design_spec.md` + `spec_lock.md` 存在；`images/` 目录已创建。

> **Trigger**: §VIII Image Resource List 中至少有一行 `Acquire Via: ai` 和/或 `Acquire Via: web`。全部为 `user`/`formula`/`placeholder` 则跳过本步。

**Always load the common framework**:

```
Read references/image-base.md
```

Then **lazy-load the path-specific reference** for each row that actually needs it:

| Acquire Via | Load reference (only if any such row exists) | Run |
|---|---|---|
| `ai` | `references/image-generator.md` | `python3 <skill_dir>/scripts/image_gen.py --manifest <project_path>/images/image_prompts.json` |
| `web` | `references/image-searcher.md` | `python3 <skill_dir>/scripts/image_search.py ...` |
| `user` / `placeholder` | (skip) | (skip) |

> ⚠️ **In-pipeline ai path MUST use manifest mode** — 即使只有 1 行 ai。先写 `images/image_prompts.json`，再跑 `image_gen.py --manifest`，再 `image_gen.py --render-md` 产出 `image_prompts.md` sidecar。

Workflow:

1. 从 design spec 提取所有 `Status: Pending` 且 `Acquire Via ∈ {ai, web}` 的行
2. 按 [image-base.md](references/image-base.md) §2 调度表生成 prompt（ai）和/或搜索（web）
3. 验证每行到达终止状态：`Generated`（ai 成功）、`Sourced`（web 成功）或 `Needs-Manual`

**失败处理**：retry once，仍失败则标记 `Needs-Manual`，告知用户，继续。

**✅ Checkpoint**:
```
## ✅ Image Acquisition Phase Complete
- [x] image_prompts.json created (when any ai rows processed)
- [x] image_prompts.md sidecar rendered (when any ai rows processed)
- [x] Each row: status is Generated / Sourced / Needs-Manual (no Pending remaining)
```

---

### Step 6: Executor Phase

🚧 **GATE**: Step 4 (Stage 1) complete; Step 5 (if triggered) complete.

#### 6.0 任务初始化 (Task Bootstrap)

开始生成前，使用 TaskCreate 创建结构化任务列表追踪进度：

```
TaskCreate: "生成设计参数确认" — pending
TaskCreate: "生成 SVG 第 1/N 页 — NN_页名" — pending（每页一个）
...
TaskCreate: "质检 — svg_quality_checker.py" — pending
TaskCreate: "生成演讲者备注 notes/total.md" — pending
```

然后将第一个任务设为 `in_progress`，开始执行。

**参考文件策略（精简模式）**：

> 本 skill 通过 Skill 工具加载时已包含 SKILL.md 全文上下文。
> 以下核心约束已内联于此，**无需通过 read_file 重新读取 executor-base.md / shared-standards.md**：

**内联关键约束**：
- 画布：`viewBox="0 0 1280 720"`（16:9）
- 禁用：`<style>`、CSS class、`filter`、`foreignObject`、`<g opacity>`、外部资源、JS
- 字体栈必须以 PPT 安全字体结尾（`Microsoft YaHei` / `Arial` / `Times New Roman`）
- 文本换行：`<tspan>` 手动换行，`dy = font-size × line-height`
- 图标：`<use data-icon="tabler-outline/icon-name" fill="#HEX" stroke-width="2">`（finalize_svg.py 自动内联）
- 插图：`<image href="../images/xxx.png" preserveAspectRatio="...">`（finalize_svg.py 自动裁剪 + 内联）
- 语义分组：每个内容块用顶层 `<g id="...">` 分组
- 路径规范：坐标精确、无重叠溢出
- 配色/字体/图标取值来源：`spec_lock.md`（一次性读取）

> 仅在遇到具体问题时才读取对应参考文件的**特定章节**（使用 offset/limit），禁止全量读取。

**Design Parameter Confirmation (Mandatory)**：生成第一页 SVG 前，一次性读取 `spec_lock.md`，输出关键设计参数（画布尺寸、色板、字体方案、正文基准字号）以及 per-page 映射表。

**Per-page spec 参数提取**（一次性完成，写入上下文，< 2KB）：

从 spec_lock.md 提取以下映射，后续每页生成时**从上下文查表，不重读文件**：
- `page_layouts` 映射：`{页stem → layout 模版名}`
- `page_charts` 映射：`{页stem → chart 类型 + 数据源}`
- `page_rhythm` 映射：`{页stem → anchor|dense|breathing}`
- 全局参数：colors、fonts、canvas、icon library

**Pre-generation Batch Read (Mandatory)**: 开始生成前，一次性读取所有 layout SVG 和 chart SVG 模版。每个文件读一次。

> ⚠️ **Main-agent only**: SVG 生成必须由当前主 agent 端到端完成——每页设计依赖完整上游上下文。禁止委托给子 agent。
> ⚠️ **Generation rhythm**: 逐页串行生成，一页接一页，同一连续上下文中。禁止分组批量（如每次 5 页）。

#### 6.1 Visual Construction Phase

**SVG 编辑器（可选提前启动）**：可在生成第一页前启动 `--live` 模式，在生成过程中实时预览：

```bash
cd /mnt/user-data/workspace && python3 <skill_dir>/scripts/svg_editor/server.py . --live &
```

> 这是可选的——用户也可在所有 SVG 完成后按需启动。若已有 `.live_preview.lock` 文件（断点续跑），跳过。

**逐页生成 + 后处理 + 进度更新**：

每页执行以下步骤：
1. 手写 SVG → `svg_output/NN_页名.svg`
2. 运行 `finalize_svg.py` 产出 `svg_final/NN_页名.svg`
3. **TaskUpdate**：将当前页任务标记为 `completed`，将下一页任务标记为 `in_progress`

**精简文件展示**（仅展示当前页，禁止累计清单）：

```
📄 SVG 第 {N}/{total} 页完成: svg_final/NN_页名.svg
```

> ⚠️ **不展示累计文件列表**。进度由 TodoList 面板追踪。

全部生成完成后，单行摘要并**停顿**，使用可交互选项让用户选择下一步：

```
📄 所有 SVG 页面已生成（共 {N} 页）。
```

**使用 AskUserQuestion 向用户展示可交互选项**：

1. 🖼️ **启动 SVG 预览编辑器** — 在浏览器中可视化预览和编辑 SVG（推荐）
2. ✅ **继续质检** — 跳过预览，直接进入质量检查和备注生成
3. 📝 **直接在对话中编辑** — 不启动编辑器，在聊天中描述修改

> ⚠️ 等待用户点击选项后再执行对应操作。若用户选择「启动编辑器」则执行启动命令并等待就绪。

**插图引用**：`needed: true` 的页在 SVG 中用占位符标识位置（`[插图]` + 风格 + prompt）；`finalize_svg.py` 替换为 data URI 内联。

#### 6.2 Quality Check Gate (Mandatory) — after all SVGs

```bash
cd /mnt/user-data/workspace && python3 <skill_dir>/scripts/svg_quality_checker.py .
```

- `error`（违禁 SVG 特性、viewBox 不匹配、spec_lock 漂移等）→ **必须修完再往下**
- `warning`（低分辨率图片、非 PPT 安全字体尾巴等）→ 能修就修，否则确认放行

> 质检必须在 `svg_output/` 上跑（不能在 `finalize_svg.py` 之后——finalize 会改写 SVG 掩盖违规）。

#### 6.2.1 用户编辑阶段 (User Editing Phase) ⛔ BLOCKING — after Quality Check passes

质检通过（退出码 0），进入专用用户编辑阶段。

---

🔬 **质检摘要**：`svg_quality_checker.py` 退出码 0，{N} 个 warning（如有则列出）

---

🖼️ **使用 AskUserQuestion 向用户展示可交互选项**：

1. 🖼️ **启动 SVG 编辑器**（推荐） — 在浏览器中可视化预览和逐页编辑 SVG
2. 📝 **在对话中编辑** — 无需启动编辑器，在聊天中描述修改需求
3. 📋 **应用注解** — 运行 `check_annotations.py` 应用标注系统中的修改指令
4. ✅ **跳过编辑，继续** — 不编辑，直接进入演讲者备注生成

> 等待用户点击选项。选项 1 需执行以下启动命令：

```bash
cd /mnt/user-data/workspace && python3 <skill_dir>/scripts/svg_editor/server.py . &
```

> 启动后告知用户编辑器地址 http://localhost:5050 并展示使用说明。

若用户选择选项 1 启动编辑器，启动后再次停顿，等待用户在编辑器中完成修改后点击 **Apply changes** 并在对话中回复「继续」/「编辑完成」/「done」。

---

📝 **编辑方式**（多通道并行）：

| 通道 | 操作方式 | 保存方式 |
|------|---------|---------|
| SVG 编辑器 | 浏览器 http://localhost:5050 → 选中元素 → 右侧面板修改 → 实时预览 | 点击 **Apply changes** 写入磁盘 |
| 对话描述 | 在聊天中直接描述修改需求 | Agent 直接编辑 `svg_final/*.svg`（同步回 `svg_output/`） |
| 标注系统 | 编辑器中选择元素 → 写修改指令 → Add annotation → Apply changes | 回到对话说「应用注解」→ Agent 运行 `check_annotations.py` |

---

⛔ **HARD STOP** — 当前阶段**暂停所有自动生成任务**。必须等待用户明确发出完成信号。

接受以下信号退出编辑阶段：
- `继续` / `done` / `编辑完成` / `完成编辑` / `proceed` / `go ahead` / `没问题` / `OK继续`
- `跳过编辑` / `skip editing` / `不需要编辑` / `直接继续`（跳过编辑，直接进入备注生成）

---

**当用户发出编辑完成信号时**：

1. 确认用户已在浏览器中点击 **Apply changes**（如有浏览器编辑），或确认所有对话修改已写入磁盘
2. 若用户未保存，提示：「您的修改尚未保存到磁盘，请先在浏览器中点击 Apply changes，或由我帮您应用对话中的修改。」
3. 确认保存后，提示用户确认进入演讲者备注生成：

> "您的SVG编辑已保存。接下来将生成演讲者备注（notes/total.md）。是否继续？"

4. 用户确认后，进入 **6.3 Logic Construction Phase**
5. ⚠️ **此处不询问 PPTX 导出**——那将在 6.3.1 中单独询问

---

**等待期间可处理的请求**：
- 用户在对话中描述的 SVG 修改（直接编辑 `svg_final/` 中的文件，同步回 `svg_output/`）
- `应用注解` / `apply my annotations`（运行 `check_annotations.py` → 逐一修改标注元素）
- 重跑质检（修改后 `svg_quality_checker.py` 应仍通过）
- 查看特定页面（告知用户编辑器 URL 和页码）

#### 6.3 Logic Construction Phase — after Visual Review Gate passes

逐页生成演讲者备注，每页作为独立消息输出（**禁止单条消息输出全部页面备注**）。

每页输出格式：

```
📝 备注 {N}/{total}：NN_页名
[过渡] ...（如适用，封面无过渡）
...口播叙述...
要点：① ... ② ... ③ ...
时长：X 分钟
```

全部页面生成完成后，拼接写入 `notes/total.md`：

```bash
# 逐段追加写入，或使用脚本拼接
cat notes/01_*.md notes/02_*.md ... > notes/total.md
```

**格式规则**：
- `notes/total.md`：每页一个 section，section 间用一行 `---` 分隔
- 首页（封面）无 `[过渡]`；正文/章节页以 `[过渡] …` 开头
- 每 section 含 `要点：① … ② … ③ …` 与 `时长：X 分钟`
- `# ` 标题文本即 SVG 文件名 stem，供 `total_md_split.py` 切分

**✅ Checkpoint**:
```
## ✅ Executor Phase Complete
- [x] All SVGs generated to svg_output/ + svg_final/（TodoList 追踪完成）
- [x] svg_quality_checker.py passed on svg_output/ (0 errors)
- [x] User Editing Phase complete (user confirmed 继续/done)
- [x] Speaker notes generated at notes/total.md（逐页独立输出完成）
- [x] Section count in total.md == SVG page count
- [x] Each section has 要点 + 时长 line
```

#### 6.3.1 PPTX 导出确认 (Export Confirmation) ⛔ BLOCKING

演讲者备注生成完成后，**禁止自动进入下游 Stage 3**。

必须先在聊天中询问用户：

> "演讲者备注已生成（notes/total.md，共 {N} 个 section）。
> 您的 SVG 编辑已保存。
> **是否开始整合输出 PPT（进入 svg-to-pptx 流程）？**"
>
> - 回复 `开始整合` / `继续生成PPT` / `proceed to PPTX` / `输出PPT` / `是` → 进入 Step 7 后处理与 PPTX 导出
> - 回复 `暂不` / `not yet` / `稍后` → 暂停，用户可随时要求继续
> - 回复 `需要继续编辑` / `edit more` / `还要改` → 回到 6.2.1 用户编辑阶段

⛔ **HARD STOP** — 必须等到用户明确确认。禁止在用户未确认的情况下调用 `total_md_split.py`、`finalize_svg.py` 或 `svg_to_pptx.py`。

---

## 返回文件列表

> ⚠️ 仅输出摘要，不列文件清单。文件进度由 TodoList 面板追踪。

生成完成摘要：{N} 页 SVG → svg_final/ + 演讲者备注 → notes/total.md

---

## Handoff to ppt-svg-to-pptx

本技能完成后，下游 `ppt-svg-to-pptx`（Stage 3）接手。⚠️ 进入 Stage 3 前必须通过 6.3.1 的 PPTX 导出确认门。

用户确认后执行：
1. `total_md_split.py` → 拆分为 `notes/NN_页名.md`
2. `svg_to_pptx.py` → `outputs/*.pptx`（默认嵌入演讲者备注，从 `svg_final/` 读取）

> `finalize_svg.py` 已在 Step 6.1 逐页执行完成，`svg_final/` 已就绪，无需重复运行。

---

## 异常处理

- **配图获取失败**：retry once → 仍失败标记 `Needs-Manual`，告知用户，继续生成其余页
- **质检 ERROR**：定位错误页 → 修正该页 SVG → 重跑质检直到退出码 0
- **文本过长**：自动分割为多卡片/多 `<tspan>`，告知用户
- **无具体数据**：生成示意占位数据，标注"示例数据"
- **超出 SVG 能力（动画/3D）**：说明限制，给出静态替代方案

---

## Reference Resources

| Resource | Path |
|----------|------|
| Executor base (common guidelines) | `references/executor-base.md` |
| Executor general style | `references/executor-general.md` |
| Shared technical constraints | `references/shared-standards.md` |
| SVG style spec | `references/svg-style-spec.md` |
| SVG code rules | `references/svg-code-rules.md` |
| Layout patterns | `references/layout-patterns.md` |
| Image base (common framework) | `references/image-base.md` |
| Image generator reference | `references/image-generator.md` |
| Image searcher reference | `references/image-searcher.md` |
| Image-text layout patterns | `references/image-layout-patterns.md` |
| Image layout sizing | `references/image-layout-spec.md` |
| SVG image embedding | `references/svg-image-embedding.md` |
| Canvas format specification | `references/canvas-formats.md` |
| Icon library | `<lib>/icons/README.md` |

## Standalone Workflows

| Workflow | Path | Purpose |
|----------|------|---------|
| `verify-charts` | `workflows/verify-charts.md` | Chart coordinate calibration — run after SVG generation if the deck contains data charts |
| `visual-review` | `workflows/visual-review.md` | Per-page rubric-based visual self-check (opt-in) |

---

## Notes

- **Bash path discipline (MANDATORY)**: 所有 bash 命令先 `cd /mnt/user-data/workspace`，再用相对路径。仅 `<skill_dir>/scripts/...` 和 `<lib>/...` 用绝对路径。
- 写 SVG 文件用文件写入工具，禁止 bash heredoc（`cat > file <<'EOF'`）；单条 bash 命令 < 10000 字符。
- **Troubleshooting**: on script issues, check `<skill_dir>/scripts/docs/troubleshooting.md`.
