# PPT 分步生成流水线 — 开发计划
  
  > 目标：将 PPT 生成重构为「总编排器 + 三段可独立执行子技能 + 统一模版提供方」的工作流，
  > 忠实映射 ppt-master 的 Step 1–7。
  >
  > 代码根目录：`/Users/louli/Documents/aigctools/clinical-sci-team/skills/custom/`
  > ppt-master 参考：`/Users/louli/Documents/aigctools/ppt-master/`
  > 样例参考：`/Users/louli/Documents/aigctools/ppt-master/examples/ppt169_fashion_weekly_digest/`
  
  ## 1. 架构与职责映射
  
  | Skill | 角色 | ppt-master 对应 | 关键产物 |
  |---|---|---|---|
  | `ppt-generator` | 总编排器 | 串联 Step1–7、断点续跑/重试 | 调度三段，最终交付 `outputs/*.pptx` |
  | `ppt-outline-generator` | Stage 1 | Step 1–4 | `design_spec.md` + `spec_lock.md` |
  | `ppt-svg-generator` | Stage 2 | Step 5–6 + 演讲者备注 | `images/*` + `svg_output/NN_页名.svg` + `notes/total.md` |
  | `ppt-svg-to-pptx` | Stage 3 | Step 7 | `svg_final/` + `outputs/*.pptx`（含演讲者备注） |
  | `ppt-template-design` | 统一模版提供方 + 创建/维护 | 模版库 + create-template/brand | `templates/{brands,layouts,decks,charts,icons}` + 索引 |
  
  每段是独立 skill，可单独执行；`ppt-generator` 负责按序串联、校验各段产物、异常按页/按段重试。
  
  ## 2. 路径与约定
  
  - `<project_path>` = `/mnt/user-data/workspace`
  - `<skill_dir>` = `/mnt/skills/custom/<skill>`
  - `<lib>`（统一模版库）= `/mnt/skills/custom/ppt-template-design/templates`
  - 上传源文件 = `/mnt/user-data/uploads`
  - 最终交付 = `outputs/*.pptx`
  - 运行时目录：`workspace/{sources,images,templates,svg_output,svg_final,notes}` + `outputs/`
  - bash 先 `cd /mnt/user-data/workspace` 用相对路径；仅 `<skill_dir>/scripts/...` 用绝对路径。
  - 写文件用文件写入工具，禁 heredoc，单条 bash 命令 < 10000 字符。
  
  ## 3. 已确认设计决策
  
  1. 模版统一由 `ppt-template-design` 提供；其它技能只读引用 `<lib>`，不再各自维护模版副本。
  2. 配图生成属 **Stage 2（svg-generator，= ppt-master Step5）**。
  3. **演讲者备注（`notes/total.md`）由 Stage 2（ppt-svg-generator）**在 SVG 全部生成、质检通过后的 Logic Construction Phase 生成，
     格式锁定为样例 `examples/ppt169_fashion_weekly_digest/notes/` 的结构；`total_md_split.py` 在 Stage 3 Step 7.1 运行拆分。
  4. 页名 stem（`NN_页名`，如 `01_封面`/`02_爱马仕家居`）在 Stage 1 确定，作为全流程页 id，
     Stage 2 的 SVG 文件名与之一致，Stage 3 据此把备注匹配到对应页。
  5. `ppt-svg-to-pptx` 用 ppt-master **原生 `svg_to_pptx`** 替换现有 clone-template
     （`svg_native/`、`template_fill/` 归档到 `.backup/`）。
  6. `finalize_svg` 在 Stage 3 运行产 `svg_final/`；导出**默认嵌入** `notes/NN_页名.md` 作为演讲者备注。
  
  ## 4. notes 格式规范（锁定样例）
  
  `notes/total.md`：每页一个 section，section 间用一行 `---` 分隔（前后空行）。
  
      # 01_封面
  
      欢迎来到 NEWS Café 美学周鉴。……（口播叙述，可含 [停顿]）
  
      要点：① …… ② …… ③ ……
      时长：0.5 分钟
  
      ---
  
      # 02_爱马仕家居
  
      [过渡] 首先，让我们从米兰设计周的重头戏开始。
  
      Hermès 家居世界艺术总监 ……[停顿]
  
      要点：① …… ② …… ③ ……
      时长：1 分钟
  
  规则：
  - 首页（封面）通常无 `[过渡]`；正文/章节页以 `[过渡] …` 开头。
  - 每 section 必含一行 `要点：① … ② … ③ …` 与一行 `时长：X 分钟`。
  - 口播语气依 §X（conversational / inform），每页约 1 分钟，整套 15–20 分钟。
  - `# ` 标题文本即 SVG 文件名 stem（去扩展名），供 `total_md_split.py` 切分与下游匹配。
  
  `notes/NN_页名.md`：由 `total_md_split.py` 从 `total.md` 切分（section 以 `# ` 开头、标题匹配 SVG 名、`---` 分隔）。
  
  ## 5. 任务分解（测试驱动、增量、最后串联）
  
  ### Task 1 — 基线快照与清单
  - 备份 5 个 `SKILL.md` 到各自 `.backup/SKILL.md.bak.<ts>`；清理 `.DS_Store`/`__pycache__`。
  - 核对各 skill `scripts/` 现状。
  - 验证：备份与原文件 `diff` 为空。
  - 状态：已完成（备份 @ `.backup/SKILL.md.bak.20260610-1958`）。
  
  ### Task 2 — ppt-template-design：统一模版提供方
  - SKILL.md 增「统一模版库提供方」段：规范 `<lib>` 路径 + 各 kind 索引 + 下游只读消费说明；
    保留创建/维护流程（create-template/brand、`register_template.py`）。
  - 验证：`<lib>/{brands,layouts,decks,charts}_index.json` 与 `<lib>/icons/README.md` 存在。
  - 状态：已完成（frontmatter + 提供方段已写入）。
  
  ### Task 3 — ppt-outline-generator：Stage 1（Step1–4）
  - Template Index 与 Step3 模版选用改引用 `<lib>`；spec 参考 md（`design_spec_reference.md`/
    `spec_lock_reference.md`）保留本地。
  - 更新 frontmatter、Deliverables、Handoff、Reference/Scripts 表。
  - 验证：跑通后 `design_spec.md`+`spec_lock.md` 存在；§IX Outline 页面 stem 集合确定（用于下游 SVG 命名）。
  
  ### Task 4 — ppt-svg-generator：Stage 2（Step5–6）
  - 输入 `design_spec.md`+`spec_lock.md`+`<lib>`+ 来自 Stage1 的页 stem 集。
  - Step5 配图获取：`image_gen.py --manifest`+`--render-md` / `image_search.py` → `images/*`，
    状态回填资源表，失败 retry once → Needs-Manual。
  - Step6 Executor：逐页读 `spec_lock.md`、批量读 `<lib>/layouts/*`、`<lib>/charts/*`，
    手写 SVG → `svg_output/NN_页名.svg`，内联图片，`svg_quality_checker.py` 硬门槛（退出码0）。
  - **Logic Construction Phase**（Quality Check Gate 通过后）：生成演讲者备注 → `notes/total.md`（格式见 §4）；
    每页约 1 分钟，整套 15–20 分钟；首页无 `[过渡]`，正文/章节页以 `[过渡] …` 开头。
  - 移除旧 `slides/` 输入模型与自建 `.pptx` 主题提取路径。
  - 验证：逐页 `svg_output/NN_页名.svg` 生成、无 `[待替换插图]`、质检退出码0；`notes/total.md` 存在，
    section 数 == SVG 页数，每 section 含 `要点` + `时长` 行。
  
  ### Task 5 — ppt-svg-to-pptx：Stage 3（Step7）平移 ppt-master 原生导出
  - 从 `ppt-master/scripts` vendor：`svg_to_pptx/`(整包) + 顶层 `svg_to_pptx.py`、全套 `svg_finalize/`、
    `finalize_svg.py`、`total_md_split.py`、`svg_quality_checker.py`、内部依赖
    `project_utils.py`/`config.py`/`error_helper.py`；可选 `notes_to_audio.py`+`tts_backends/`、
    `pptx_animations.py`、`animation_config.py`。
  - 归档现有 `svg_native/`+`template_fill/`+旧 `svg_to_pptx.py` 到 `.backup/clone-path/`。
  - 拷 references：`shared-standards.md`、`animations.md`；workflows：`verify-charts.md`、
    `customize-animations.md`、`generate-audio.md`。
  - SKILL.md 改写流程：质检（`svg_quality_checker.py`/`check_layout.py` 退出码0）→
    `finalize_svg.py`(→`svg_final/`) → `svg_to_pptx.py <project_path>`（原生 DrawingML，**默认嵌入
    `notes/NN_页名.md`**）→ `outputs/*.pptx`。更新 `requirements.txt`（python-pptx）。
  - 验证：`svg_to_pptx.py --help`、`finalize_svg.py` 导入无误；样例 `svg_output/`→`svg_final/`→
    可打开 pptx 且含备注。
  
  ### Task 6 — ppt-generator：编排器重写
  - 依赖（3 子技能 + ppt-template-design 提供方）、路径约束、Step1/2/3 与交接产物、
    断点续跑完成标志表、目录树（含 `notes/`、`svg_output/`、`svg_final/`）、并行说明。
  - 修正过时的 cairosvg/libcairo 兜底（原生流无需栅格化器）。
  - 验证：路径/产物与三子技能一致，无残留 `slides/`、`svg/`、`prompts/`。
  
  ### Task 7 — 校验与收尾
  - import-check 关键脚本；核对 5 个 SKILL.md 引用的 script/reference/workflow 路径均存在；
    无遗留 clone-template 默认引用；清理缓存。
  - 端到端：以 fashion-weekly 源跑 Stage1→2→3，逐项命中续跑标志表；最终 pptx 含备注、页数与大纲一致。
  
  ## 6. 断点续跑 / 完成标志表
  
  | 步骤 | 完成标志 |
  |---|---|
  | Stage1 | `design_spec.md`+`spec_lock.md` 存在 |
  | Stage2 | 每页 `svg_output/NN_页名.svg` 存在且图内联；`needed:true` 页有 `images/*`；`notes/total.md` 存在，section 数 == SVG 页数 |
  | Stage3 | `svg_final/` 已生成；`outputs/*.pptx` 可打开且含演讲者备注 |
  
  ## 7. 关键脚本迁移命令（Task 5 vendoring）
  
  ```bash
  SRC=/Users/louli/Documents/aigctools/ppt-master/skills/ppt-master/scripts
  MASTER=/Users/louli/Documents/aigctools/ppt-master/skills/ppt-master
  OUT=/Users/louli/Documents/aigctools/clinical-sci-team/skills/custom/ppt-outline-generator
  DST=/Users/louli/Documents/aigctools/clinical-sci-team/skills/custom/ppt-svg-to-pptx/scripts
  
  # Task 5: 归档 clone-template 路径
  mkdir -p "$DST/../.backup/clone-path"
  mv "$DST/svg_native" "$DST/template_fill" "$DST/svg_to_pptx.py" "$DST/../.backup/clone-path/" 2>/dev/null || true
  
  # Task 5: vendor ppt-master 原生导出
  cp -R "$SRC/svg_to_pptx" "$DST/"
  cp -p "$SRC/svg_to_pptx.py" "$DST/"
  cp -R "$SRC/svg_finalize" "$DST/"
  cp -p "$SRC/finalize_svg.py" "$SRC/total_md_split.py" "$SRC/svg_quality_checker.py" "$DST/"
  cp -p "$SRC/project_utils.py" "$SRC/config.py" "$SRC/error_helper.py" "$DST/"
  cp -p "$SRC/notes_to_audio.py" "$DST/" ; cp -R "$SRC/tts_backends" "$DST/"
  cp -p "$SRC/pptx_animations.py" "$SRC/animation_config.py" "$DST/"
  
  # Task 5: references + workflows
  DREF=/Users/louli/Documents/aigctools/clinical-sci-team/skills/custom/ppt-svg-to-pptx/references
  DWF=/Users/louli/Documents/aigctools/clinical-sci-team/skills/custom/ppt-svg-to-pptx/workflows
  mkdir -p "$DREF" "$DWF"
  cp -p "$MASTER/references/shared-standards.md" "$MASTER/references/animations.md" "$DREF/"
  cp -p "$MASTER/workflows/verify-charts.md" "$MASTER/workflows/customize-animations.md" "$MASTER/workflows/generate-audio.md" "$DWF/"
  
  find "$DST" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  
  8. 进度
  
  - [x] Task 1 基线快照（备份 @ .backup/SKILL.md.bak.20260610-1958）
  - [ ] Task 2 ppt-template-design 统一提供方段（已写入 SKILL.md）
  - [ ] Task 3 ppt-outline-generator（Step1–4）
  - [ ] Task 4 ppt-svg-generator（Step5–6）
  - [ ] Task 5 ppt-svg-to-pptx（Step7 原生导出）
  - [ ] Task 6 ppt-generator 编排器
  - [ ] Task 7 校验与收尾
  
  9. 验证（针对样例的可复现目标）
  
  以 examples/ppt169_fashion_weekly_digest 的源为输入跑 Stage 1→2，应复现：
  
  - notes/total.md：16 个 # NN_页名 section，含 [过渡]/[停顿]/要点/时长（由 Stage 2 生成）。
  - notes/NN_页名.md：16 个，stem 与 §IX 各页一一对应，且与 svg_output/NN_页名.svg 同名（由 Stage 3 Step7.1 拆分）。