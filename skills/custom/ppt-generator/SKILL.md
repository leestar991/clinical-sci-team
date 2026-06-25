---
name: ppt-generator
description: >
  End-to-end PPT generation pipeline orchestrator — serializes Stage 1 (ppt-outline-generator →
  design_spec.md + spec_lock.md), Stage 2 (ppt-svg-generator → svg_output/*.svg + notes/total.md),
  and Stage 3 (ppt-svg-to-pptx → svg_final/ + outputs/*.pptx with speaker notes).
  All templates sourced from ppt-template-design (<lib>). Use when user wants to
  create a complete PowerPoint presentation from documents or text input.
  Triggers on: '生成PPT', '做PPT', '制作演示文稿', 'create presentation', 'make PPT', 'generate pptx'.
---

# PPT Generator（总编排器）

端到端 PPT 生成流水线编排器。串联三阶段子技能 + 统一模版提供方，支持断点续跑/按页重试。

**架构**：

```
ppt-generator (总编排)
  ├─ Stage 1: ppt-outline-generator  → design_spec.md + spec_lock.md (页 stem 集)
  ├─ Stage 2: ppt-svg-generator      → svg_output/*.svg + svg_final/*.svg + notes/total.md
  │   ├─ 6.1 Visual Construction（📄 单行摘要 + TaskUpdate 驱动进度）
  │   ├─ 6.2 Quality Check Gate（质检 svg_output/）
  │   ├─ 6.2.1 用户编辑阶段（⛔ BLOCKING — AskUserQuestion 可交互选项）
  │   ├─ 6.3 Logic Construction（演讲者备注）
  │   └─ 6.3.1 PPTX 导出确认门（⛔ NEW BLOCKING）
  └─ Stage 3: ppt-svg-to-pptx        → outputs/*.pptx (含演讲者备注)
```

所有模版资产由 `ppt-template-design` 统一提供（`<lib>` = `/mnt/skills/custom/ppt-template-design/templates`），下游三阶段只读引用。

## 依赖

- **Skills**: `ppt-outline-generator`（Stage 1）、`ppt-svg-generator`（Stage 2）、`ppt-svg-to-pptx`（Stage 3）
- **模版提供方**: `ppt-template-design`（统一模版库 `<lib>`，只读引用）
- **文档转换**: `markitdown[all]`（需 extras 才支持 pdf/docx/pptx/xlsx）

## 路径约束（强制）

> [!IMPORTANT]
> ## 🗂️ Path Convention — deer-flow Sandbox

> | Symbol | Absolute Path | Notes |
> |--------|--------------|-------|
> | `<project_path>` | `/mnt/user-data/workspace` | deer-flow session working directory |
> | `<lib>` | `/mnt/skills/custom/ppt-template-design/templates` | 统一模版库 |
> | `<uploads>` | `/mnt/user-data/uploads` | 上传源文件 |
> | Output | `/mnt/user-data/workspace/outputs/` | 最终 .pptx 交付 |

> ⚠️ **Bash relative-path rule (MANDATORY)**: 所有 bash 命令必须先 `cd /mnt/user-data/workspace`，再用相对路径。严禁硬编码 `/mnt/user-data/workspace/...` 绝对路径。

- **文件写入方式**：写文件一律用文件写入工具，**禁止 bash heredoc（`cat > file <<'EOF'`）或内联 shell 写文件**；单条 bash 命令必须 **< 10000 字符**。

## 目录结构（运行时）

```
.                            # /mnt/user-data/workspace
├── design_spec.md           # Stage 1 产物 — 人类可读设计规格
├── spec_lock.md             # Stage 1 产物 — 机器可读执行合约
├── sources/                 # Stage 1 导入 — 源 Markdown 文件
├── images/                  # Stage 2 Step 5 — 配图资源（AI/搜索/用户/公式）
├── svg_output/              # Stage 2 Step 6 — 逐页 SVG（NN_页名.svg）
│   ├── 01_封面.svg
│   ├── 02_内容页.svg
│   └── ...
├── svg_final/               # Stage 2 Step 6.1 — 后处理完成 SVG（逐页 finalize）
├── notes/                   # Stage 2/3 — 演讲者备注
│   ├── total.md             # Stage 2 Logic Construction — 合并稿
│   ├── 01_封面.md           # Stage 3 Step 7.1 — 拆分后逐页备注
│   └── ...
├── templates/               # Stage 1 Step 3 — 引用的模版 spec（可选）
├── outputs/                 # 仅最终产物
│   └── <project>_<ts>.pptx  # Stage 3 Step 7.3 — 含演讲者备注
└── backup/                  # Stage 3 — svg_output 快照备份
```

## 断点续跑 / 完成标志表

各步骤均把结果落盘，流水线可续跑。当某一步出错中断时：

1. **不从头重来**：按下表检查各步产物，跳过已完成的步骤，只重试失败步及其下游
2. **页级粒度续跑**：Stage 2/3 逐页操作按页判断——已生成的页跳过，**仅补做失败或缺失的页**
3. **重试前先诊断**：定位错误根因（依赖缺失、路径错误、单页内容异常等），修正后再重试
4. **续跑后校验**：补做步骤完成后确认产物完整再进入下游

| 步骤 | 完成标志（产物） |
|------|-----------------|
| Stage 1 | `design_spec.md` + `spec_lock.md` 存在（含 §IX Outline 页 stem 集） |
| Stage 2 | 每页 `svg_output/NN_页名.svg` + `svg_final/NN_页名.svg` 存在且质检通过；`notes/total.md` 存在，section 数 == SVG 页数 |
| Stage 3 | `svg_final/` 已生成；`outputs/*.pptx` 可打开且含演讲者备注 |

## 并行执行

Stage 2 逐页 SVG 生成彼此独立，但按 ppt-master 规则必须由主 agent 端到端串行完成（每页设计依赖完整上游上下文）——禁止委托给子 agent。

Stage 2 配图获取（Step 5）可按 Acquire Via 路径并行：ai 行和 web 行互不依赖，可并行处理各自 manifest。

Stage 3 子步骤（7.0 → 7.1 → 7.2 → 7.3）**必须串行**——每步完成后才能进入下一步。

---

## 工作流程

### Stage 1: 生成设计规格（ppt-outline-generator）

调用 `ppt-outline-generator` skill，完成 ppt-master Step 1–4：

1. Step 1: 源内容处理（非 MD → MD 转换）
2. Step 2: 项目初始化（scaffold `sources/ images/ templates/`）
3. Step 3: 模版选用（从 `<lib>` 查询/派发/fuse，默认自由设计）
4. Step 4: Strategist 八项确认（⛔ BLOCKING）→ 产出 `design_spec.md` + `spec_lock.md`

**产物**：
- `design_spec.md` — 十一节完整设计规格（§IX Outline 含页 stem 集：`NN_页名`）
- `spec_lock.md` — 机器可读执行合约（`page_layouts`、`page_charts`、`images`、每页 `page_rhythm`）

**完成标志**: `design_spec.md` + `spec_lock.md` 存在。

### Stage 2: 生成 SVG + 演讲者备注（ppt-svg-generator）

调用 `ppt-svg-generator` skill，完成 ppt-master Step 5–6：

- **Step 5（条件）**: 配图获取——`image_gen.py --manifest`（AI 生图）和/或 `image_search.py`（网页搜索）→ `images/*`；失败 retry once → Needs-Manual
- **Step 6 Executor**: 
  - 6.1 Visual Construction：逐页手写 SVG → `svg_output/NN_页名.svg`
    - 每页生成后运行 `finalize_svg.py` → `svg_final/NN_页名.svg`
    - svg_editor 可选提前启动（`--live` 模式），生成完成后 AskUserQuestion 可交互选项
    - 📄 每页仅展示单行摘要 + TaskUpdate 驱动进度（禁止累计文件清单）
  - 6.2 Quality Check Gate：`svg_quality_checker.py` 硬门槛（退出码 0，质检 `svg_output/`）
  - 6.2.1 用户编辑阶段 (User Editing Phase) ⛔ BLOCKING
    - AskUserQuestion 可交互选项（启动编辑器 / 对话编辑 / 应用注解 / 跳过编辑）
    - 暂停所有自动生成任务，等待用户点击选项或回复
  - 6.3 Logic Construction：生成演讲者备注 → `notes/total.md`
  - 6.3.1 PPTX 导出确认门 (Export Confirmation) ⛔ NEW BLOCKING
    - 询问用户是否开始整合输出 PPT → 仅用户确认后进入 Stage 3

**产物**：
- `svg_output/NN_页名.svg`（每页原始 SVG）
- `svg_final/NN_页名.svg`（每页后处理完成 SVG，供用户预览和编辑）
- `notes/total.md`（section 数 == SVG 页数，每 section 含 `要点` + `时长`）

**完成标志**: 所有 `svg_output/NN_页名.svg` + `svg_final/NN_页名.svg` 存在且质检通过 + `notes/total.md` 存在。

> ⛔ **PPTX 导出确认门**：Stage 2 完成后（SVG + 备注已就绪），
> **不要自动调用 `ppt-svg-to-pptx`**。
> 使用 AskUserQuestion 向用户展示可交互选项：
>
> > "演讲者备注已生成。您的 SVG 编辑已保存。"
>
> 1. ✅ **开始整合输出 PPT** — 进入 svg-to-pptx 流程，生成 .pptx 文件
> 2. ⏸️ **暂不生成** — 暂停，用户可随时要求继续
> 3. 🔙 **继续编辑 SVG** — 回到 6.2.1 用户编辑阶段
>
> 等待用户点击选项后再执行对应操作。

### Stage 3: 后处理 & 导出 PPTX（ppt-svg-to-pptx）

> ⚠️ 进入条件：用户已通过 PPTX 导出确认门（见上一步）。

调用 `ppt-svg-to-pptx` skill，完成 ppt-master Step 7：

1. **Step 7.0**: 质检（`svg_quality_checker.py`，退出码 0）
2. **Step 7.1**: 拆分备注（`total_md_split.py` → `notes/NN_页名.md`）
3. **Step 7.2**: SVG 后处理（`finalize_svg.py` → `svg_final/`，仅在 Stage 2 未执行时运行）
4. **Step 7.3**: 导出 PPTX（`svg_to_pptx.py` → `outputs/*.pptx`，**默认嵌入演讲者备注**）

> 原生 DrawingML 导出，**无需** cairosvg/libcairo/rsvg——不需要栅格化器。

**产物**：
- `svg_final/` — 后处理完成的 SVG
- `outputs/*.pptx` — 含演讲者备注的最终 PPTX

**完成标志**: `svg_final/` 已生成 + `outputs/*.pptx` 可打开且含演讲者备注。

---

### 交付

返回最终文件路径：

> ✅ PPT 已生成：`outputs/<project>_<timestamp>.pptx`（含演讲者备注，页数与 Stage 1 大纲一致）

---

## Lessons Learned

- **原生流无需栅格化器**: Stage 3 使用 ppt-master 原生 `svg_to_pptx`（DrawingML），不需要 cairosvg/libcairo/rsvg。旧兜底路径（`cairosvg` 栅格化再导入为图片）已废弃——原生转换保真度更高、不依赖系统级动态库。
- **路径兼容性**：运行期临时 Python 脚本不要把 `/mnt/user-data/...` 绝对路径硬编码到脚本内部；优先使用 `Path.cwd()` / 相对路径。
- **日志/临时文件路径**：沙箱会拦截指向 `/tmp` 等工作区外的绝对路径写入；安装日志、调试输出统一落到 `workspace/`。
- **续跑策略**：若 Stage 1（spec）/ Stage 2（SVG + notes）已完成，后续失败时不要回退重做上游内容；只补做失败步骤及其下游。
