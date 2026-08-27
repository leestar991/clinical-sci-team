---
name: screening-report-generator
description: >
  入排筛选HTML报告生成技能 — 生成交互式入排比对判读报告(screening_report.html)和入排标准解析报告(criteria_report.html)。
  触发: '生成入排报告', '输出HTML报告', 'generate screening report', '/screening-report-generator'
---

# 入排筛选 HTML 报告生成

## 概述

本技能生成两份自包含的交互式 HTML 报告：
1. **`screening_report.html`** — 入排比对判读报告（主条件组级结论 + 逐条子条件判定 + 证据定位 + PDF截图；同一患者多份物料合并汇总——一条条件一个折叠结论，证据全部匹配展示）
2. **`criteria_report.html`** — 入排标准解析报告（四分类展示 + 条件转化 + 逻辑关系）

报告模板位于 `/mnt/skills/custom/screening-report-generator/templates/`。

---

## ⛔ 强制流程（唯一允许的生成方式）

**报告必须由技能内置构建器生成，禁止手写 HTML/CSS/JS。**

**输入前置**：`--criteria` 必须是**全量** `criteria_parsed.json`（由
`judge_pack.py assemble` 合成两轨 IN/EX 而来），**不是**某一轨的
`criteria_parsed_{IN|EX}.json`、也不是判定输入包 `criteria_judge_{IN|EX}.json`——
单轨文件只含一半四分类，报告会缺掉另一半标准。`--judgments` 必须是两轨合并后的
`judgments_{patient_id}.json`，不是轨道中间文件。

```bash
# 1) 构建（模板骨架 + 数据注入，一步完成）
python3 /mnt/skills/custom/screening-report-generator/scripts/build_reports.py \
  --criteria  /mnt/user-data/outputs/criteria_parsed.json \
  --judgments /mnt/user-data/outputs/judgments_{patient_id}.json \
  --workspace /mnt/user-data/workspace \
  --out-dir   /mnt/user-data/outputs

# 多患者/多物料：重复 --judgments，判定合并进同一套表格——同一患者的多份物料是
# 共享证据，一条条件跨物料折叠为一个结论（不符合>符合>存疑>无法判断）
#   --judgments .../judgments_P001.json --judgments .../judgments_P002.json

# 2) 校验（present_files 之前必须全 ✅，否则修数据重跑）
python3 /mnt/skills/custom/screening-report-generator/scripts/build_reports.py \
  --verify --out-dir /mnt/user-data/outputs
```

构建器做的事：读取模板 → 归一化中间 JSON → base64 内嵌证据截图（自动去重）→
注入 `<script id="data">` → 自校验。模板的 CSS/JS/DOM **零改动**，因此产出样式必然与模板一致。

**`--verify` 闸的失败处置**：出现 ❌ 时**回去修数据再重跑构建器**，
⛔ **不得**改写产出的 HTML 绕过校验（改 HTML 只会让报告脱离模板，且下次重建即丢失）。
常见成因：`--criteria` 传了单轨文件（`四分类` 只有一半）、`judgments` 传了轨道中间文件、
证据截图路径不存在。

**禁止事项（曾导致产出样式完全偏离模板的真实故障）**：

| ❌ 禁止 | ✅ 正确 |
|--------|--------|
| 写 `generate_report.py` 里用 f-string 拼 HTML/CSS | 调用 `build_reports.py` |
| 自定义配色（如 `#f5f7fb` / `#111827` / `badge.maybe`） | 用模板 `:root` 设计变量 |
| 自己发明 DOM（`.cards` / `.table-wrap` / `.crit-card` + `<details>`） | 模板的 `summaryBar` / `judgment-table` / `crit`+`srow` |
| 服务端渲染成静态 `<tr>` / `<div>` | 数据注入 `<script id="data">`，由模板 JS 渲染 |
| 丢掉 Lightbox / 跨物料折叠结论徽章 / 筛选计数 / localStorage 判定 | 模板已内置，不要重写 |

**必须校验通过的项**：模板指纹（`--inc:#0f766e`、`id="summaryBar"`、`djudge-v`、`openLB(`、
`function prow(`、`function srow(`、`id="data"`；`criteria_report.html` 另有 `function pinfo(`）、
数据块可解析、`crit`/`ids`/`docs`/`四分类` 非空、`parents`（主条件）非空、主条件 `members` 均在
`ids` 内、主条件与 `merged` 结论枚举合法、`merged` 覆盖全部条件ID 且与各物料判定折叠一致。
校验失败说明产出不是模板渲染的报告。

**⚠️ 建议级提醒（不阻断交付）**：主条件结论显示「未汇总」时打 ⚠ 而非 ❌ —— 说明
`judgments_{id}.json` 缺 `criteria_rollup`（没走 `judge_pack.py merge-judgments`，或是旧产物）。
报告仍可交付，但父行只有子条件计数、没有组级结论；处置见下方「主条件层」。

### 输入字段兼容

构建器同时接受中/英文键名，`judgments_{id}.json` 无需预先改写：

| 语义 | 接受的键名 |
|------|-----------|
| 结论 | `conclusion` \| `结论` |
| 理由 | `reason` \| `理由` |
| 证据数组 | `evidence` \| `证据` |
| 证据来源/页/摘录/命中 | `source`\|`来源`、`page`\|`页`、`quote`\|`原文摘录`、`hit`\|`命中` |
| 证据截图路径 | `screenshot_ref` \| `screenshot` \| `图`（相对 `workspace/` 或绝对路径均可） |

判定文件结构支持三种输入：
1. **统一判定产物（第一公民）** — 顶层 `judgments`、无 `documents` 维度：`{"patient_id","judgment_date","judgments":{"条件ID":{…}},"criteria_rollup":{…},"rollup_summary":{…}}`（由 `/eligibility-judgment` 统一证据源判定产出）。doc 键取 `patient_id`、标签取 `patient_name`，顶层 `criteria_rollup` 直接作主条件结论。
2. 历史多物料产物 `{"documents": {"doc_key": {"judgments": {...}}}}` — 仍兼容，`merged` 跨物料折叠保留为防御层。
3. 扁平 `{"IN-1": {...}}` — 兼容旧产物。

### 主条件层（两级表格的父行）

`screening_report.html` 的判定表是**两级**的：父行 = 一条原始入排标准（`IN-10` / `EX-1`）的
**组级结论**，展开后是该标准拆出的子条件行。父行数据来自两处：

| 数据 | 来源 | 说明 |
|---|---|---|
| 主条件清单与描述 | `criteria_parsed.json` 的 `四分类` + `描述索引` | 构建器按条件ID 推导主条件（`IN-2-1` → `IN-2`），描述取 `描述索引[主条件ID]`，缺失时回退该主条件首个子条件文本 |
| 主条件**结论** | `judgments_{id}.json` 的 `criteria_rollup`（统一判定产物在顶层；历史产物在 `documents[]` 内） | 由 `/eligibility-judgment` 的 `judge_pack.py merge-judgments` 产出；多物料时按「跨物料折叠」节同优先级折叠 |

⛔ **报告侧不自己折叠子条件结论**。折叠口径（AND `不符合 > 存疑 > 无法判断 > 符合`；
入选 `或组` 任一满足即整条满足；排除 `或组` 任一触发即整条触发）的唯一真相源是判定侧
`rollup.py`，两处各写一份必然漂移出「判定说符合、报告说不符合」的静默分歧。

### 跨物料折叠（一条条件一个结论）

同一患者的多份物料（如「筛选期病历」「筛选期检查」）是**共享证据材料**，报告**不再按物料
各自出一套判定**：多份文件里有证据就全部匹配展示（证据卡片按物料分组合并、标注来源），
但同一条条件的结论按 **不符合 > 符合 > 存疑 > 无法判断** 折叠为**唯一结论**——不允许
「符合 + 不符合」「符合 + 存疑」「存疑 + 无法判断」等矛盾结论共存。

| 层 | 折叠输入 | 产出 |
|---|---|---|
| 子条件 | 各物料 `documents[].judgments[条件ID].结论` | `DATA.merged[条件ID] = {结论, 判定: {物料: 原结论}}` |
| 主条件 | 各物料 `documents[].criteria_rollup[主条件ID].结论` | `DATA.parents[].结论`（全「未汇总」→「未汇总」） |

构建器只折叠**判定侧已有的结论**，绝不重算 AND/OR 口径（唯一真相源仍是判定侧
`rollup.py`）；`merged.判定` 保留各物料原结论供查证，页面不渲染为徽章——判定列只显示
一枚折叠结论徽章。物料标签缺省回退**物料名**（`documents` 的键，如「筛选期病历」），
缺 `doc_label` 字段时不再显示成患者ID，保证徽章/理由/证据分组可区分物料。

缺 `criteria_rollup` 时**降级**：父行结论显示「未汇总」、只带子条件计数，stderr 出声，
`--verify` 报 ⚠ 但不阻断。处置 = 回判定侧重跑 `judge_pack.py merge-judgments` 合并两轨后重建报告。
判定产物顶层的 `rollup_warnings`（`或组语义` 缺失/与轨矛盾等）也会被透传到 stderr。

### 图片去重池

构建器把所有证据截图集中放进 `DATA.imgs`（`{"#img1": "data:image/jpeg;base64,..."}`），
证据的 `图` / `原件[].缩略图` 只写 `"#imgN"` 引用；模板 JS 自动解析。
同一页截图被 N 条条件引用时只编码一次（37 条条件 6 页截图：1.1MB 而非 20MB+）。
`图` 也兼容直接内联 data URI。

---

## 通用设计规范

### 自包含原则
- 单个 HTML 文件，无外部依赖
- CSS 内嵌于 `<style>` 标签
- JavaScript 内嵌于 `<script>` 标签
- 图片以 base64 格式内嵌（`data:image/jpeg;base64,...`）
- 数据以 JSON 格式嵌入 `<script id="data" type="application/json">` 标签

### CSS 设计变量

```css
:root {
  --bg: #f4f6f8;
  --card: #fff;
  --ink: #141b23;
  --muted: #6a7583;
  --faint: #95a0ad;
  --line: #e4e8ed;
  --brand: #0f766e;
  --inc: #0f766e;       /* 入选标准色：深绿 */
  --inc-bg: #ecf6f4;
  --exc: #b4531b;       /* 排除标准色：橙色 */
  --exc-bg: #fbefe5;
  --ok: #12805c;        /* 符合：绿 */
  --ok-bg: #e7f6ef;
  --no: #c0392b;        /* 不符合：红 */
  --no-bg: #fdecea;
  --may: #b8860b;       /* 存疑：黄 */
  --may-bg: #fbf2dc;
  --na: #95a0ad;        /* 无法判断：灰 */
  --na-bg: #eef1f4;
}
```

### 设计原则
- 专业医学文档风格：清晰、高对比度、易读
- 响应式布局（桌面 ≥1200px / 平板 ≥768px 适配）
- 颜色语义化一致
- 入选标准卡片左侧色条用 `--inc`，排除标准用 `--exc`
- 字体栈：`-apple-system, "Segoe UI", "Microsoft YaHei", Roboto, sans-serif`
- 等宽字体（编号/数值）：`ui-monospace, Consolas, monospace`

---

## 报告一：入排比对判读报告 (`screening_report.html`)

### 数据输入
- `judgments_{patient_id}.json` — 逐条判定结果 + `documents[].criteria_rollup`（主条件组级结论）
- `criteria_parsed.json` — 入排标准清单（`四分类` + `描述索引`，后者供主条件行显示标准描述）
- 证据截图图片文件（转为 base64 嵌入）

### HTML 结构

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>入排标准 × 病历 · 比对判读</title>
  <style>/* 完整 CSS — 见下方规范 */</style>
</head>
<body>
  <!-- 1. 头部 -->
  <header>
    <h1>入排标准 × 病历 · 比对判读（表格视图）</h1>
    <div class="sub">方案编号 + 方案标题</div>
    <div class="meta">来源文件列表 + 判定语义说明</div>
  </header>

  <div class="wrap">
    <!-- 2. 跨文档警告（如有）-->
    <div class="warning-banner">
      <!-- 黄色警告条：标注文档间矛盾/疑非同一患者 -->
    </div>

    <!-- 3. 统一汇总条（跨物料，不再按物料分 tab） -->
    <div class="doc-summary" id="summaryBar">
      <!-- 主条件/子条件/物料数 + 判定数（=子条件数，每条件一个折叠结论）+ 四类计数 -->
    </div>

    <!-- 4. 图例 -->
    <div class="legend">
      🟢 符合 · 🔴 不符合 · 🟡 存疑 · ⚪ 无法判断
      "黄框=PDF定位高亮，点击图片放大 · 原件(↗)点击新标签页打开：图片显缩略图，txt/md 显文档卡片"
    </div>

    <!-- 5. 筛选标签栏（sticky） -->
    <div class="filter-bar">
      <span class="tabhint">按主条件筛选</span>
      <div class="tabs">全部 | 符合(N) | 不符合(N) | 存疑(N) | 无法判断(N) [| 未汇总(N)]</div>
      <button class="expand-all">展开全部</button>
      <input class="search" placeholder="搜索条件编号/内容...">
    </div>

    <!-- 6. 判定表格（两级：主条件行 + 可折叠的子条件行） -->
    <table class="judgment-table">
      <thead>
        <tr>
          <th>编号</th>
          <th>条件（子条件/原文）</th>
          <th>判定</th>
          <th>推断理由（按物料）</th>
          <th>证据与定位（全部物料合并）</th>
        </tr>
      </thead>
      <tbody>
        <!-- JS 动态渲染：tr.prow（主条件，colspan=5）+ tr.subrow（子条件，默认折叠） -->
      </tbody>
    </table>
  </div>

  <!-- 7. Lightbox 浮层 -->
  <div class="lightbox" id="lightbox">
    <div class="lb-overlay"></div>
    <div class="lb-content">
      <img class="lb-img">
      <div class="lb-caption"></div>
      <button class="lb-close">×</button>
    </div>
  </div>

  <!-- 8. 数据 -->
  <script id="data" type="application/json">
  {
    "crit": { /* criteria_parsed 中每条的 原文+子条件+来源+是否入选 */ },
    "ids": [ /* 有序条件ID列表 */ ],
    "parents": [
      /* 主条件（两级表格的父行），按编号自然序；结论=各物料 criteria_rollup 折叠（不符合>符合>存疑>无法判断） */
      {"pid": "IN-2", "inc": true, "desc": "年龄 18–70 岁，性别不限", "结论": "符合", "规则": "AND", "members": ["IN-2-1", "IN-2-2"]}
    ],
    "merged": {
      /* 子条件跨物料折叠：结论=唯一折叠结论，判定=各物料原结论（供查证，页面不渲染为徽章） */
      "IN-2-1": {"结论": "符合", "判定": {"筛选期病历": "符合", "筛选期检查": "无法判断"}}
    },
    "docs": {
      "doc_key": {
        "名": "文档显示名",
        "标签": "既往病历（男 · M017）",
        "J": {
          "IN-2-1": {
            "结论": "符合|不符合|存疑|无法判断",
            "理由": "推断理由文本",
            "证据": [{
              "来源": "既往病历",
              "src": "doc_key",
              "页": 1,
              "原文摘录": "OCR提取的原文",
              "命中": true,
              "图": "data:image/jpeg;base64,...",
              "原件": [
                {"链接": "workspace/images/筛选期病历/筛选期病历_page_001.jpg", "缩略图": "data:image/jpeg;base64,...", "说明": "病历第1页"},
                {"链接": "workspace/images/筛选期检查/筛选期检查_page_017.txt", "摘要": "临床试验：XS-03-II201 ..."}
              ]
            }]
          }
        },
        "R": {
          /* 主条件组级结论，取自 judgments 的 criteria_rollup（报告不重算；多物料时按同优先级折叠进 parents[].结论） */
          "IN-2": {"结论": "符合", "规则": "单条", "依据": ["IN-2-1"], "计数": {"符合": 1, "不符合": 0, "存疑": 0, "无法判断": 0}}
        },
        "cnt": { "符合": 18, "不符合": 2, "存疑": 5, "无法判断": 15 },
        "rcnt": { "符合": 8, "不符合": 1, "存疑": 2, "无法判断": 3 }
      }
    }
  }
  </script>

  <!-- 9. 交互逻辑 -->
  <script>
  // 功能：跨物料折叠结论徽章、筛选标签、搜索、Lightbox
  </script>
</body>
</html>
```

### 表格列规范

**主条件行（`tr.prow`，`colspan=5`）** —— 一条原始入排标准的组级结论，默认折叠：

| 元素 | 内容 |
|------|------|
| `▶` caret | 展开/收起该主条件的子条件行（展开时旋转 90°） |
| `.pnum` | 主条件编号（`IN-10` / `EX-1`），等宽字体 |
| `.djudge-v` 徽章 | **主条件折叠结论徽标**：各物料 `criteria_rollup` 按 不符合>符合>存疑>无法判断 折叠的唯一结论；全「未汇总」时显示「未汇总」（灰） |
| `.pdesc` | 标准描述（取 `描述索引`，缺失回退首个子条件文本） |
| `.pcnt` | 子条件**折叠结论**四类计数 mini 标签（每个子条件一个折叠结论） |
| `.pmeta` | `n 子条件 · m 份物料` + 折叠规则（`单条`/`AND`/`OR组`/`AND+OR组`） |
| 左侧色条 | 入选 `--inc`（绿）/ 排除 `--exc`（橙），与子条件行一致 |

**子条件行（`tr.subrow`）**：

| 列 | 宽度 | 内容渲染 |
|----|------|----------|
| 编号 | 80px | 等宽字体条件ID + 类别mini标签(入选/排除) + 左侧色条 |
| 条件 | 35% | 子条件文本(粗体14px) + 原文(灰色12px) + 来源引用 |
| 判定 | 80px | **一枚折叠结论徽章**：各物料判定按 不符合>符合>存疑>无法判断 折叠的唯一结论，颜色按判定类型 |
| 推断理由 | 25% | 各物料理由分小节展示（小节标注物料名） |
| 证据与定位 | 30% | 全部物料的证据按物料分组合并：来源标签+页码+摘录+定位截图(可点击放大)+原件引用(图片→缩略图 / txt·md→文档卡片，点击新标签页外跳打开原件链接) |

### 交互功能清单

1. **跨物料折叠**：每条条件显示一枚折叠结论徽章（不符合>符合>存疑>无法判断）；证据与理由合并全部物料、按物料分组标注来源
2. **主条件折叠**：点击主条件行展开/收起其子条件行；`展开全部` 按钮一键全展/全收
3. **筛选标签**：按**主条件结论**过滤与计数（全部/符合/不符合/存疑/无法判断，存在未汇总时多一档）；筛选生效时匹配的主条件自动展开，便于直接看到是哪几条子条件造成的
4. **搜索**：输入关键词实时过滤（搜索编号、子条件、原文、理由）；命中子条件时自动展开其主条件，并只显示命中的子条件行
5. **Lightbox**：点击证据定位截图(`图`) → 全屏浮层显示大图 + 标注信息
6. **原件外跳**：点击证据卡片内的原件缩略图/文档卡片(`原件`) → 在新标签页(`target="_blank"`)外跳打开原始文件链接（图片显示缩略图，txt/md 等文本显示 📄 文档卡片；区别于 Lightbox，不做站内放大）
7. **计数更新**：切换文档/筛选时动态更新各类计数
8. **颜色编码**：表格行左侧色条（入选=绿，排除=橙），结论badge颜色

---

## 报告二：入排标准解析报告 (`criteria_report.html`)

### 数据输入
- `criteria_parsed.json` — 结构化四分类入排标准

### HTML 结构

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>入排标准解析 · {方案编号}</title>
  <style>/* 完整 CSS */</style>
</head>
<body>
  <!-- 1. 头部 -->
  <header>
    <h1>入排标准解析 · {方案编号}</h1>
    <div class="sub">方案标题</div>
    <div class="meta">试验药品 | 版本 | 来源</div>
  </header>

  <div class="wrap">
    <!-- 2. 导航栏（sticky）-->
    <div class="bar">
      <div class="tabs">
        全部 | 入选·可获取(N) | 排除·可获取(N) | 入选·不可获取(N) | 排除·不可获取(N)
      </div>
      <div class="tools">
        <input class="search" placeholder="搜索子条件/原文/字段...">
        <div class="progress">符合:N 存疑:N 不符合:N 待定:N</div>
        <button class="reset">清除判定</button>
      </div>
    </div>

    <!-- 3. 内容区 -->
    <div id="content">
      <!-- JS 动态渲染分类卡片 -->
    </div>
  </div>

  <footer>拆分原则说明 + 日期处理说明</footer>

  <!-- 数据 -->
  <script id="data" type="application/json">
  /* 嵌入 criteria_parsed.json 全部内容 */
  </script>

  <!-- 交互逻辑 -->
  <script>
  // 功能：标签切换、搜索、折叠展开、判定按钮、localStorage持久化
  </script>
</body>
</html>
```

### 卡片层级结构

**父级卡片（每条原始标准）**：
- 编号徽标（入选=绿底，排除=橙底）
- 标准描述（来自描述索引）
- 子条件数量
- 折叠/展开按钮

**子条件行（每条拆分后的子条件）**：
- 子编号（如 2.1, 2.2）
- 子条件文本（高亮数值）
- 匹配字段提示badge
- 可获取性pill（绿色"可获取" / 红色"不可获取"）
- 判定按钮组（符合/存疑/不符合，localStorage持久化）

**展开详情**：
- **原文**：灰色背景块，显示完整原始方案文字
- **逻辑关系**：AND/OR说明
- **转化条件**（仅可获取类）：KV结构化展示
  - 匹配字段、运算符、阈值、单位
  - 或条件列表、除外说明
  - 并列条件、适用臂/人群
- **日期维度**（如有）：KV结构化展示
  - 事件、发生日期、参考事件、参考日期、时间窗、判断
- **备注**（如有）：黄色提示条

### 交互功能清单

1. **标签切换**：四分类 + 全部，点击切换显示
2. **搜索**：实时过滤子条件文本、原文、匹配字段
3. **折叠/展开**：点击父卡片头部切换子条件显示
4. **判定按钮**：符合/存疑/不符合，状态持久化到 localStorage
5. **进度统计**：实时更新各判定状态计数
6. **重置**：清除所有人工判定
7. **数值高亮**：子条件文本中的数值自动高亮显示

---

## 交付文件清单

报告产出统一写入 `/mnt/user-data/outputs/` 并 `present_files`：

- `outputs/screening_report.html` --入排比对判读报告（必交付）
- `outputs/criteria_report.html` --入排标准解析报告（必交付）

**规则**：每个文件仅 `present_files` 一次；这两个报告在报告交付阶段由主代理批量 present，不与前期已 present 的 `criteria_parsed.json` / `judgments_{id}.json` 等重复。

⛔ **`present_files` 不校验文件是否存在**——present 一个不存在的 `outputs/x.html` 会返回
"Successfully presented files"，错误被静默吞掉。因此必须先 `cp` + `ls -l` 确认存在且非 0 字节，
**下一轮**才 present（三步法见 SOUL 原则 9）。故障叙述见 **`references/failure-archive.md`**。

---

## 报告生成流程

### 由主代理直接执行（不委派子代理）

原因：
- 报告由构建器脚本渲染，主代理只需一条 bash 命令 + 一次校验，委派反而增加交接成本
- 输入是 `outputs/` 下的既有 JSON 路径，主代理已持有，无需子代理再检索
- 绝不让子代理"自己写一份 HTML"——这是样式偏离模板的根因

### ⚠️ 数据嵌入的关键规则

> 以下规则由 `build_reports.py` 自动保证，此处仅作数据契约说明与排障参考。

**`criteria_report.html`** 的 `<script id="data">` 必须**直接嵌入 criteria_parsed.json 的完整内容**，不做任何包装或嵌套：

```html
<script id="data" type="application/json">
{
  "方案元数据": { ... },     ← 直接从 criteria_parsed.json 复制
  "解析说明": { ... },       ← 直接从 criteria_parsed.json 复制
  "四分类": { ... },         ← 直接从 criteria_parsed.json 复制（JS代码从这读取）
                             每个类目是**以 条件ID 为键的对象**（旧 workspace 的数组形态也兼容）；
                             模板在读取时统一归一并按 条件ID 自然序排序，**不依赖容器书写顺序**
  "汇总统计": { ... },      ← 直接从 criteria_parsed.json 复制
  "描述索引": { ... }        ← 直接从 criteria_parsed.json 复制
}
</script>
```

❌ **错误做法**（会导致页面空白）：
```html
<!-- 不要这样！不要多加一层包装！ -->
<script id="data" type="application/json">
{
  "方案元数据": {},
  "解析说明": { "方案元数据": {...}, "四分类": {...} ... }
}
</script>
```

**`screening_report.html`** 的 `<script id="data">` 需要组装为模板期望的格式：

```html
<script id="data" type="application/json">
{
  "protocol": { "id": "XS-03-II201", "title": "...", "source_files": [...] },
  "crit": {
    "IN-2-1": { "inc": true, "子条件": "...", "原文": "...", "来源": "入选标准 第2条" },
    ...
  },
  "ids": ["IN-2-1", "IN-2-2", ...],
  "parents": [
    { "pid": "IN-2", "inc": true, "desc": "年龄 18–70 岁，性别不限", "结论": "符合", "规则": "AND", "members": ["IN-2-1", "IN-2-2"] },
    ...
  ],
  "merged": {
    "IN-2-1": { "结论": "符合", "判定": { "筛选期病历": "符合", "筛选期检查": "无法判断" } },
    ...
  },
  "docs": {
    "doc_key": {
      "名": "...",
      "标签": "...",
      "J": { "IN-2-1": { "conclusion": "符合", "reason": "...", "evidence": [...] }, ... },
      "R": { "IN-2": { "结论": "符合", "规则": "AND", "依据": ["IN-2-1"], "计数": {...} }, ... },
      "cnt": { "符合": N, "不符合": N, "存疑": N, "无法判断": N },
      "rcnt": { "符合": N, "不符合": N, "存疑": N, "无法判断": N },
      "warnings": []
    }
  }
}
</script>
```

`parents` / `R` / `rcnt` 是两级表格的主条件层：`parents` 来自 `criteria_parsed.json`（含 `描述索引`），
`R` / `rcnt` 直接取自 `judgments_{id}.json` 的 `criteria_rollup` / `rollup_summary`。`parents[].结论`
与 `merged` 是**跨物料折叠层**：`parents[].结论` = 各物料 `criteria_rollup` 按 不符合>符合>存疑>
无法判断 折叠（全「未汇总」→「未汇总」）；`merged[条件ID]` = 各物料判定折叠的唯一结论 + 原判定表。

### 步骤

1. **确认输入路径**（从 `phase4_summary.json` 取，不重读全量 JSON）：
   - `outputs/criteria_parsed.json`
   - `outputs/judgments_{patient_id}.json`（每患者一个）

2. **执行构建器**（一条命令完成模板读取 + 数据归一化 + 截图内嵌 + 注入 + 自校验）：

   ```bash
   python3 /mnt/skills/custom/screening-report-generator/scripts/build_reports.py \
     --criteria  /mnt/user-data/outputs/criteria_parsed.json \
     --judgments /mnt/user-data/outputs/judgments_{patient_id}.json \
     --workspace /mnt/user-data/workspace \
     --out-dir   /mnt/user-data/outputs
   ```

   常用可选参数：
   - `--no-images`：不内嵌 base64 截图（仅保留原件链接），用于快速产出或体积敏感场景
   - `--max-image-bytes 400000`：超过该大小的图片先用 PIL 压缩（无 PIL 时自动降级为不内嵌）
   - `--templates <dir>`：模板目录（默认自动定位技能内 `templates/`）

3. **校验**（输出必须全 ✅ 才能交付）：

   ```bash
   python3 /mnt/skills/custom/screening-report-generator/scripts/build_reports.py \
     --verify --out-dir /mnt/user-data/outputs
   ```

   常见失败与处置：

   | 校验失败项 | 原因 | 处置 |
   |-----------|------|------|
   | 模板指纹缺失 | 报告不是模板渲染的（例如被手写 HTML 覆盖） | 删除产出，重跑构建器 |
   | 数据块不可解析 | 数据里含未转义 `</script>` | 构建器已处理；若手工改过数据块则重跑构建器 |
   | `crit`/`ids` 为空 | `criteria_parsed.json` 的「四分类」为空或键名不符；或误传了单轨 `criteria_parsed_{IN\|EX}.json` | 回到标准解析/合成阶段修产物（确认传的是 `assemble` 出的全量包） |
   | `docs` 为空 / 判定条目 0 | `--judgments` 路径错或判定文件结构异常 | 核对 `phase4_summary.json` 路径 |
   | `parents`（主条件）为空 | `criteria_parsed.json` 的条件ID 不符合 `IN-n[-m]` / `EX-n[-m]` 编号规范，或 `四分类` 为空 | 回标准解析阶段修条件ID 体系 |
   | 主条件 `members` 不在 `ids` 内 | 报告 HTML 被手工改过数据块 | 重跑构建器，不要手改产出 |
   | `merged` 缺失/不覆盖全部条件ID/与各物料判定折叠不一致 | 报告 HTML 被手工改过数据块（或用了旧构建器产物） | 重跑构建器，不要手改产出 |
   | ⚠ 主条件结论「未汇总」 | `judgments_{id}.json` 缺 `criteria_rollup`（未走 `merge-judgments`，或旧产物） | 回判定侧用 `judge_pack.py merge-judgments` 合并两轨后重建报告；**不阻断交付**，但父行只有子条件计数 |
   | 「四分类」为空 | criteria_report 数据被多包了一层 | 构建器直接嵌入原文件，不要手工包装 |

4. **交付**：
   - `present_files`：`outputs/screening_report.html` + `outputs/criteria_report.html`
   - 若构建器 stderr 提示「证据截图未找到」，检查 `workspace/images/{source}/` 是否已由
     `/pdf-image-extractor` 产出；报告仍可交付，但会缺少定位截图。

> 下方「HTML 结构 / CSS 设计变量 / 表格列规范 / 交互功能清单」是模板的**设计说明**，
> 用于评审与二次修改模板本身；日常生成报告不需要照着手写，直接用构建器。

### 证据截图 base64 处理

> 构建器已内置：自动定位 `screenshot_ref`、去重、超限压缩（需 PIL；无 PIL 时降级为不内嵌）。
> 以下命令仅用于手工排查单张图片。

```bash
# 将证据图片转为 base64（通过 bash 工具）
python3 -c "
import base64, json
with open('image.png', 'rb') as f:
    b64 = base64.b64encode(f.read()).decode()
print(f'data:image/png;base64,{b64}')
"
```

如图片过大（>500KB），先压缩再编码：
```bash
python3 -c "
from PIL import Image
import io, base64
img = Image.open('image.png')
img.thumbnail((1200, 1600))
buf = io.BytesIO()
img.save(buf, format='JPEG', quality=75)
b64 = base64.b64encode(buf.getvalue()).decode()
print(f'data:image/jpeg;base64,{b64}')
"
```

### 原件引用 (`原件` 字段)

在 `screening_report.html` 每条证据（`证据[]`）中，除定位截图 `图`（点击站内 Lightbox 放大）外，可追加 `原件` 字段，用于引用**原始文件**。原件可为**图片**（扫描页 jpg/png 等），也可为**文本**（OCR/解析导出的 `txt`、`md` 等）。缩略图渲染在“证据与定位”列内，点击在**新标签页外跳打开原件链接**（`target="_blank"`，不做站内放大）：

- 图片型 → 渲染为图片缩略图；
- 文本型（txt/md/markdown/csv/json/log）→ 渲染为 📄 文档卡片（显示扩展名 + 可选文本预览）。

> 数据来源示例：PDF 拆页产物目录（如 `workspace/images/筛选期病历/`、`workspace/images/筛选期检查/`）中，扫描页为 `*_page_NNN.jpg`，可提取文本页为 `*_page_NNN.txt`，`*_manifest.json` 的每页 `format` 字段（`jpg`/`txt`）即原件类型，可据此填充 `链接`。

字段格式（数组，支持多个；也兼容单对象或纯字符串路径/URL）：

```json
"原件": [
  {
    "链接": "workspace/images/筛选期病历/筛选期病历_page_001.jpg",  // 必填：点击外跳打开的文件路径/URL
    "缩略图": "data:image/jpeg;base64,...",                        // 可选(仅图片)：缩略图 src；缺省时用「链接」作缩略图源
    "说明": "病历第1页"                                            // 可选：卡片下方说明文字，缺省时取文件名
  },
  {
    "链接": "workspace/images/筛选期检查/筛选期检查_page_017.txt",  // 文本原件
    "类型": "text",                                               // 可选：显式指定 text/image；缺省按扩展名推断
    "摘要": "临床试验：XS-03-II201 ..."                            // 可选(仅文本)：文档卡片内的短预览
  }
]
```

渲染规则：
- `链接` 为空的项会被跳过。
- **类型判定**：优先用显式 `类型`/`type`（`text`/`image`）；否则按 `链接` 扩展名推断——`txt/md/markdown/csv/json/log` → 文本卡片，`jpg/jpeg/png/gif/webp/bmp/svg/tif` → 图片缩略图；无法识别时若有图片 `缩略图` 则按图片，否则按文本。
- 图片型：`缩略图` 缺省时直接用 `链接` 作为 `<img src>`；原图较大时建议单独生成压缩后的 base64 缩略图填入 `缩略图`，`链接` 仍指向原图，避免页面体积过大。
- 文本型：显示 📄 图标 + 扩展名徽标；填了 `摘要` 时附一段截断预览（≤80 字符）。
- 兼容旧写法：`原件` 亦可写作 `原件图` / `证件图`；数组元素可为对象或纯字符串路径（字符串即作为 `链接`，类型按扩展名推断）。
- 每张缩略图/卡片右上角带 `↗` 角标，提示为外跳链接。
