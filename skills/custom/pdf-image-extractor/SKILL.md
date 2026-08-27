---
name: pdf-image-extractor
description: >
  Document OCR for medical records — turns PDFs / Word / Excel / scanned images into
  searchable text + tables. Primary path is the `parse_document` tool (gateway-side
  TextIn OCR: feed a whole PDF or a single page image, get back markdown + HTML tables
  with NO base64 entering the conversation). For batch processing of multiple page images
  (>2), use `parse_image_batch` which sends all images in one tool call and returns
  per-page OCR results. Falls back to rendering pages via PDFium
  (`pdf_to_image.py`) + `view_image` only for structured forms / handwriting / when
  TextIn output is insufficient. Use for medical-record PDFs (lab reports, imaging /
  pathology reports, clinical notes), scanned documents, or any file you must extract
  facts from.
  Triggers on: '文档OCR', '病历OCR', 'PDF识别', 'PDF转文字', '提取PDF内容', '病历图片提取',
  'PDF转图片', 'parse document', 'parse image batch', 'ocr', 'extract text from pdf',
  'pdf to markdown', 'pdf to image', 'pdf to png'.
---

# Document OCR（文档 OCR：parse_document 主 · 图片+view_image 副）

把 PDF / Word / Excel / 扫描图片转成**可检索的文本 + 表格**,供下游 LLM 判定与确定性 grep 使用。

**核心工具优先级(必读)**:
1. **`parse_document`(单文件)** — DeerFlow 内置**工具调用**(function call),网关侧走 TextIn OCR。可直接吃**整份 PDF/Office 文档**或**单张分页图片**,返回**索引**(页数/表格数/产物目录),正文落在 `document.md`、表格落在 `tables/NNN.html`。图片字节走网关直传,**不进对话上下文、无需视觉模型**。
   ⚠️ 它调用外部服务(每页计费)。整份一次调用(路线 A)最省,逐页(路线 B)才有页边界——**选哪条由上层按用户选择决定,技能不设默认**;逐页解析时每轮 ≤ 2-3 个,不要一次铺开十几个。
2. **`parse_image_batch`(批量)** — 当需要 OCR 的图片 **>2 张**时,优先使用此工具。一次工具调用传入所有图片路径,网关侧批量走 TextIn OCR,返回**每页的 OCR 结果**(含 `document.md` + 表格),并自动在 `ocr/{source}/` 下写入同 stem 的 `.md` 文件(含首行 `（来源图片：…）`),**无需逐张 read_file 再手工 write_file**。比逐张 `parse_document` 省轮次、省上下文、省固定开销。
   ⚠️ 同样走外部服务(每页计费)。适合路线 B 逐页场景:一个 source 的几十张分页图,一次 `parse_image_batch` 全部处理完,不用拆成多个子任务逐张调用。
3. **`pdf_to_image.py`(副,分页渲染)** — 本地渲染、无 API 费用。用于两种场景:**多患者混合 PDF 需要按页拆分**、或**整份 parse_document 失败后降级逐页重试**;也为 `view_image` 兜底提供页图。
4. **`view_image`(兜底)** — 逐页解析仍失败,或结构化表单/量表/手写页的 OCR 文本不足、疑似错抄时,加载该页原图供视觉模型复核。每轮最多 2-3 张(payload 硬限制,见约束)。

## 输入预检(先剔除,再选路线)

**推荐直接用脚本**——规则内聚、可复现，避免调用方每次用内联代码重写一遍:

```bash
python3 /mnt/skills/custom/pdf-image-extractor/scripts/classify_uploads.py \
  --uploads /mnt/user-data/uploads \
  --out    /mnt/user-data/workspace/pdf_classification.json \
  [--images-dir /mnt/user-data/workspace/images]     # 拆页后重跑可回填 total_pages / scanned_pages
```

脚本一次完成:剔除(零字节 + sidecar) → PDF 类型判定(`scan`/`mixed`/`text`) → 为 `scan`/`mixed`
预设 `ocr_route: "A"` → 非 PDF 落 `non_pdf` 并给 `role` 提示。
**重跑不覆盖已有的 `ocr_route` / `route_reason` / `role` / `handled_by`**,所以降级决策与角色判定不会被冲掉。
产物结构见下方「产物:pdf_classification.json」。

脚本背后的规则(手工执行时同样适用)——解析前必须剔除两类"看似是输入、实则不可用"的文件,否则会白花 OCR 成本或**卡在空文件读取上**:

1. **`size == 0` 的文件** —— 空文件/上传中间件转换失败的产物。既不解析也不 read_file,直接跳过并记录原因。空文件不会因为多读几次变得有内容(历史故障:流程反复读一个 0 字节 `.md` 直至卡死,而同名 PDF 早已拆图完成)。
2. **sidecar `.md`** —— 上传中间件会为每个文档生成**同 stem 的 `.md`** 转换产物。它不是独立输入,只是同一份内容的文本视图:
   - `size > 0` → 作为该文档"是否有文本层"的判据(决定 scan/mixed/text),`text` 型直接读它即可,**无需解析原文件**;
   - `size == 0` → 说明转换失败,该文档按**扫描件**处理,并把这个空 `.md` 彻底忽略。

> 判定口径:存在同 stem 的 `.pdf`/`.docx`/`.doc`/`.xlsx`/`.pptx` 时,该 `.md` 即为其 sidecar。

### PDF 类型判定(依据 sidecar `.md` 大小)

| 条件 | 类型 | 处理 |
|---|---|---|
| `md_size == 0` | `scan` | 全扫描页 → `pdf_to_image.py` 拆页 |
| `md_size > 0` 且 `pdf_size / md_size > 20` | `mixed` | 部分文字 + 大量扫描页 → 也需拆页;文本页读 `.txt` |
| 其余 | `text` | 文字为主 → 直接 `read_file` sidecar `.md`,**不拆页、不 OCR** |

### 产物:`pdf_classification.json`

```json
{"scan": [{"pdf": "病历.pdf", "source_name": "病历", "pdf_size": 7721565, "md_size": 0,
           "total_pages": 10, "scanned_pages": 10,
           "ocr_route": null, "route_reason": null}],   ← null = 待用户选择处理模式后回填
 "mixed": [], "text": [{"pdf": "报告.pdf", "handled_by": "read_md"}],
 "non_pdf": [{"file": "入排标准.docx", "sidecar_md": "入排标准.md", "sidecar_md_size": 6848,
              "role": "protocol_criteria", "handled_by": null}],
 "ignored": [{"file": "病历.md", "reason": "size=0（空文件 / 上传中间件转换失败）"}]}
```

- `ocr_route` / `route_reason`:解析路线与理由(见下节)。脚本写 **`null`**(待选择);**必须由调用方在用户显式选择处理模式后回填** `A`/`B`,降级时也在这里改写。仍为 `null` 表示"未确认",下游禁止推断。
- `role` / `handled_by`:业务语义,脚本只给 `role` 提示值,**由调用方确认**。上游已提取完成的文档(如入排标准 → `eligibility_criteria_raw.md`)必须标明并**禁止再解析**。
- `ignored`:后续所有阶段**禁止 read_file / parse_document**。

### 补齐业务语义(`non_pdf` 每项必须由 LLM 确认)

脚本给不出业务语义,`non_pdf` 每一项都要由调用方确认 `role` + `handled_by`:

| `role` | 含义 | `handled_by`(唯一处理方) |
|---|---|---|
| `protocol_criteria` | 试验方案 / 入排标准类文档 | `phase1_criteria_extract` —— 已由 `/criteria-parser` 的「章节提取」逐字提取为 `eligibility_criteria_raw.md`,**后续阶段禁止再解析** |
| `patient_record` | 患者病历 / 检查资料(独立文件,非某文档的 sidecar) | `ocr_route_a` / `ocr_route_b`(路线由本技能的路线选择表决定,结果写入 `ocr_route`) |
| `other` | 与入排判定无关 | `skip` |

`ignored` 段的文件(零字节 / sidecar)在后续所有阶段**禁止 read_file / parse_document**;
它存在的意义是让"为什么没处理它"可追溯。

### 拆页始终执行(不因选了路线 A 而跳过)

对 `scan` + `mixed` 的**每个** source 都要跑 `pdf_to_image.py`(纯本地渲染、无 API 费用),
产出 `workspace/images/{source}/` + manifest。分页图是路线 B 与 `view_image` 兜底的前提,
**不因选了路线 A 而跳过**(与「关键约束 14」同义:拆页 ≠ 选定路线 B)。

拆页后可带 `--images-dir` 重跑 `classify_uploads.py` 回填 `total_pages` / `scanned_pages`
(`scanned_pages` = 需要**图片 OCR** 的页数;覆盖率分母是 `scanned_pages + text_pages`,
不是单独任一个 —— 文本层页也必须归集进 `ocr/`;重跑不覆盖已填的
`ocr_route`/`role`/`handled_by`)。

### 委派模板

处理模式（单/多患者 × 整份/逐页）由**用户显式选择**决定,本技能不设默认路线;
`ask_clarification` 的参数、三项选项原文、以及收到回答后落成 `patient_mode`/`ocr_route`/
是否拆分聚合/证据链能力的映射表,见 **`references/mode-selection.md`**。

把 OCR 派给子代理时,模板见 **`references/ocr-delegation.md`**(路线 A 整份 / 路线 B 逐页
各一份,含"不要自行降级、只回报 `route_a_failed`"与"result 禁回传 OCR 正文"等边界)。

本技能各条硬规则背后的真实故障叙述见 **`references/failure-archive.md`**
(处理模式确认的三次中断失效、反复读零字节文件卡死、自造目录导致同页重复 OCR)。
⛔ 按需读单节,不要整篇加载——规则以本文件与 `mode-selection.md` 为准。


## 预处理与编排(与解析流水线交织的 OCR 侧流程)

本节拥有 OCR 侧的**处理流程与编排规则**(2026-08-26 自 SOUL 下沉;模式确认的 HITL 机制
骨架仍由编排层拥有,参数与选项映射见 `references/mode-selection.md`)。

### 拆页流水线(预处理阶段,与解析轨并行)

1. 跑 `scripts/classify_uploads.py` 归类 → `pdf_classification.json`;返回后**补齐 `non_pdf`
   每项的 `role` + `handled_by`**(入排标准/方案类一律 `role=protocol_criteria`,由解析侧
   提取、后续禁止再解析);
2. 对每个 `scan`/`mixed` PDF **各发一个 bash** 拆页(`scripts/pdf_to_image.py`,同轮并行,
   不要串成顺序脚本)→ `images/{source}/` + manifest;
3. 拆页后带 `--images-dir` 重跑归类脚本,回填 `total_pages` / `scanned_pages`。

### 编排边界(四件事,其余归执行规则)

- **路线由用户从三种模式中选定,无默认值**:模式1 → `ocr_route="A"`;模式2/3 → `"B"`。
  `classify_uploads.py` 产出的 `ocr_route` 是 **`null`**(脚本故意不给默认值),必须被用户
  选择覆盖后才可推进;仍为 `null` 时禁止派 OCR、禁止推断、禁止"按最快的走"。用户事后改
  主意可以改 `ocr_route` 并重做该 source(A/B 互斥禁的是"同时跑两条",不是"改主意")。
- **路线决策落盘**:每个 source 的 `ocr_route` + `route_reason` 写入 `pdf_classification.json`;
  子任务不得自行降级——A 失败时只回报失败状态 + 错误原文,**由主代理改写 `ocr_route` 后再派 B**。
- **在途 OCR 子任务 ≤ 2**(控制外部服务并发与计费节奏);剩余预算给两条解析/QC 轨。
  ⛔ 路线 B 逐页 OCR 优先用 `parse_image_batch`(一次调用处理全部图片,自动写入 `.md`),
  不要拆成多个子任务逐张 `parse_document`——固定开销(系统提示 + 读技能文档)每个子代理
  都要重付。`parse_image_batch` 一次调用完成一个 source 的全部扫描页,省轮次、省上下文、
  省子代理数。
- **产物去向**:OCR 结果必须落在 `workspace/ocr/{source}/`;`workspace/parsed/<hash>/` 是
  技能中间产物,不 present、不交付;`parsed/` 有产物而 `ocr/` 为空 = 本阶段失败。

### 调用量纪律(OCR 派发三量)

派发 OCR 子任务时的三个量度,**集中在此**(执行侧细节见 §parse_document 工具调用铁律):

| 量度 | 上限 | 理由 |
|---|---|---|
| **每子任务图片数** | **6-9 张**(路线 B 派发粒度,⛔ 不是 1-2 张) | 系统提示 + 读技能文档的固定开销每个子代理都要重付;单轮上限≠每子任务上限,子代理内可多轮 |
| **`parse_document` 单轮调用数** | ≤ 2-3 个 | 每次调用都下载文件 + 打外部 OCR 服务并按页计费 |
| **在途 OCR 子任务数** | ≤ 2(见上方编排边界) | 控制外部服务并发与计费节奏;剩余预算给两条解析/QC 轨 |

- 派 OCR 子任务带 `expected_outputs`(路线 B 声明 `ocr/{source}/` 下首个 `.md` 产物或
  覆盖率产物);同轮 task 数不超平台子代理预算(超发被框架静默截断,等于漏派)。

## 路线选择(**由调用方/用户决定,技能不设默认**——A/B 互斥,禁止同时做)

```
路线 A —— 整份文档 parse_document,1 次调用拿全文 + 全部表格
          快、省、失败面小;但**无页边界** → 证据无法逐页定位
路线 B —— pdf_to_image.py 拆页 → 逐张图 parse_document(每轮 ≤ 2-3 张)
          保留页边界 → 证据带页码 + 原件截图;调用数 = 页数,慢一些
          （多患者混合在一个文件里时**只能**走 B,否则无法按页拆分归属）
          │
          └── 个别页 Error:?  → view_image 兜底该页
A 返回 Error:(超时/网关故障/识别失败,已退避重试 1 次)? → 降级 B
```

> ⛔ **技能不预设默认路线**：A/B 是"速度成本"与"证据链可定位性"之间的**业务取舍**，必须由**上层编排让用户显式选择**后传进来。
> `classify_uploads.py` 因此把 `ocr_route` 写成 `null`，`ocr_coverage.py` 遇到 `null` 直接报"⛔ 未选择"而不当作 A。
> 历史故障：路线有默认值时，模型会凭空写 `route_reason: "用户已确认…"` 直接推进，用户从未被问过。

**A 的成本优势**:整份解析把 `parse_document` 调用数从 N(页数)压到 **1**——调用次数、固定开销与 agent 轮次都最少,失败面也只有一个点。**代价是没有页边界**,取舍见下表。

> 📌 **一则曾被误判的故障**:某轮 10 张分页图解析全部失败、`workspace/parsed/` 未生成、`ocr/` 全空,当时被归因为"并行触发限流",实际根因是**客户端把图片路由到了不存在的 `image_to_markdown` 机器人**(`code=40007 机器人不存在或未发布`),与并发无关;该 bug 已修(见「parse_document 工具调用铁律」)。**没有实测证据表明本工具会因 2-3 个并发被限流**,不要用"限流"解释解析失败——先看 `Error:` 里的 code。

> ⚠️ **`parse_document` 并发适度**:**一轮** ≤ 2-3 个(每个调用都要下载文件 + 打外部服务),同轮发起后再统一读产物、写 `.md`。**禁止**一轮铺开十张以上。
> ⛔ 这是**单轮**上限,**不是每子任务上限**:子代理可多轮,路线 B 每个子任务应带 **6-9 张**图
> (3 张/轮 × 3 轮)。把它误读成"每子任务 2-3 张"会让子任务数翻 3-4 倍,而每个子代理的固定开销
> (系统提示 + 读技能文档)都要重付一遍(会话 `69612125`:28 页派了 16 个子任务,均 ≈296K tokens/个)。

| | 路线 A（整份） | 路线 B（逐页） |
|---|---|---|
| 工具调用 | `parse_document` ×1 | `parse_image_batch` ×1（推荐，>2 张）或 `parse_document` ×N（≤10 张） |
| 失败面 | 单点，重试成本低 | N 个点，需逐页补漏 |
| 轮次/时延 | ~3 轮，单子任务 | 批量：1 轮 1 次调用；逐张：多子任务、多轮 |
| 页边界 / `evidence.page` | ❌ 无 | ✅ 有 |
| 页图截图引用 `screenshot_ref` | ❌ 无 | ✅ 有 |
| 多患者按页拆分 | ❌ 不可能 | ✅ 可 |

> **A 无页边界是实测结论**：对真实的多页整份解析产物（6 页 / 3 页）grep `第N页`、`page`、分页符,**命中数为 0**——TextIn 整份解析只产出**一份合并 `document.md`** + 全文档打平编号的表格。因此多患者拆分必须走 B；若只是**偶尔**需要某条证据的页码,不要为此把整份改成逐页,而是用 `view_image` 对候选页确认后回填页码(每轮 ≤ 2-3 张)。

**拆页与路线解耦**:`pdf_to_image.py` 是**本地渲染、无 API 费用**,建议无论选哪条路线都先拆页——分页图是 `view_image` 兜底与路线 B 降级的前提。**已经拆好图 ≠ 必须走路线 B**;反之,选定 B 后就不得再整份解析。

### `parse_document` 失败分类与降级(禁止盲目重试)

| `Error:` 类型 | 处置 |
|---|---|
| 格式不支持 / 文件不存在 / 超出 `max_bytes` | **不重试**,如实回报并跳过(输入问题,重试无用) |
| 超时 / 网关错误 / `30203 基础服务故障` 等瞬时失败 | **下一轮退避重试 1 次** → 仍失败则降级下一级 |
| `40007 机器人不存在或未发布` / `40008 机器人未开通` | **配置/账号问题,重试与降级都无用**:说明请求打到了不存在或未开通的服务。如实上报,要求检查 endpoint 与账号开通状态 |
| `40003 余额不足` / `40101`/`40102` 凭证问题 | 同上,不重试,如实上报 |
| 整份(A)重试后仍失败 | 降级 **B**:逐页解析,每子任务 2-3 张 |
| 逐页(B)中个别页失败 | **view_image 兜底**该页(每轮 ≤ 2-3 张),据原图按统一内容格式写出该页 `.md`,文件头注明"由 view_image 人工读图" |

降级是**单向**的:A → B → view_image;已降级到 B 后不得回头整份解析。

### ⛔ 解析去重铁律(A/B 互斥 + 同一目标只解析一次)

历史故障(入排筛选 thread `6f0f0504`):同一份 10 页病历 PDF 被解析了**三遍**——先对整份 PDF `parse_document`(路线 A,已成功拿到 `parsed/eff324ae328d/`,10 页 8 表),又拆成 10 张分页图逐张 `parse_document`(路线 B),还额外拆了 10 个单页 PDF 与一套 ASCII 改名副本;另有一份 `入排标准.docx` 在上游已提取完成后被再次 `parse_document`。OCR 成本 ×3,`workspace/ocr/` 却仍为空。

- **路线二选一并落定**:对同一个文件**只能选 A 或 B**,决策结果应记录下来(如 `ocr_route` 字段)。选定 B 后**禁止**再对该文件整份 `parse_document`;反之选定 A 后禁止再拆页逐张解析。
- **同一目标只解析一次**:解析前先看**目标产物是否已存在**——路线 B 以 `ocr/{source}/{stem}.md` 为幂等键(存在即跳过该页),路线 A 以该文件已有的 `parsed/<hash>/document.md` 为准。
- **禁止"换个形态再解析一遍"**:同一页内容不得既走分页图又走单页 PDF;**禁止**为规避文件名(中文/全角括号/空格)而复制出 ASCII 副本再解析——路径用引号包裹或传绝对路径即可。
  > 补注:历史上有轮把每页另存成单页 PDF 才 OCR 成功,那不是多余动作,而是在绕开"图片被路由到不存在的 `image_to_markdown`"这个客户端 bug(已修)。**现在图片可直接解析,不需要也不允许再拆单页 PDF。**
- **上游已提取的文档不得再解析**:若某文档的内容已由上游流程提取为工作文件(如入排标准 → `eligibility_criteria_raw.md`),后续阶段**禁止**对该原始文档再调 `parse_document`。
- **解析目标必须来自任务清单**:只解析被显式指派的路径,禁止自行扩展到 `uploads/` 下的其它文件,禁止解析预检已剔除的空文件/sidecar。

---

## parse_document 工具调用铁律(路线 A/B 通用,必读——已发生多次回归)

`parse_document` 是你**当前已加载的工具**(tool call / function call 入口),用工具调用语法直接发起即可。它**不是** Python 模块、不是 pip 包、不是 CLI 可执行文件,**不能也不需要** import 或当命令行调用。

**严禁**用 bash/python 做以下任何探测/调用(必然返回空/报错,会误导你放弃工具路径、回退到自装 OCR 或 view_image,已导致 OCR 全空、丢全部表格):
- `python -c "import parse_document"` / `importlib.util.find_spec('parse_document')`(查模块)
- `shutil.which('parse_document')` / `which parse_document`(查 CLI)
- `subprocess.run(['parse_document', path])` / shell 里 `parse_document <path>`(当 CLI 跑)
- `python -m parse_document <path>`
- `pip install parse_document`,或改用 docling / marker / pymupdf4llm / pytesseract 等自行 OCR

**验证可用性的唯一正确方式 = 直接发起一次 `parse_document` 工具调用**(参数 `path=<绝对路径>`)。返回文本以 `Error:` 开头才算真失败;若工具列表里根本没有 `parse_document` 这个工具名,属配置异常,应明确报错告知调用方,**禁止用 bash/python 绕路**。

**⚠️ 网关侧 endpoint(2026-07-28 实测)**:所有输入(PDF / Office / **图片**)都走同一个 TextIn 接口
`POST /ai/service/v1/pdf_to_markdown`——TextIn 自己嗅探文件类型。**不存在 `image_to_markdown` 机器人**:
把图片发到该路径会返回 `code=40007 机器人不存在或未发布`(用合成 200x200 PNG 复现),而同一张 PNG 发到
`pdf_to_markdown` 返回 `code=200, total_page_number=1`。历史上正是这个路由 bug 让**每一张分页图 OCR 都失败**、
整份 PDF 却正常,并被误判为"并行限流"。若再看到 `40007`/`40008`,一律按**服务未开通/路由错误**处理:
重试和降级都无效,直接上报。

**返回值语义**:parse_document 返回的是**索引**(页数、表格数、产物目录 `…/parsed/<hash>/`),**不是正文**。取正文与表格要:
1. 从索引文本里取 `document.md` 路径 → `read_file` 得 OCR 正文。
2. 若索引显示表格数 > 0 → 逐个 `read_file` `tables/NNN.html`。**TextIn 的表格只存在于 `tables/*.html`,不会出现在 `document.md`,遗漏会丢数据**。

---

## 路线 A:整份文档直接 parse_document(主力,单文档/无需分页)

### A1. 直接对文件发起 parse_document
对整份 PDF / Word / Excel / 单张图片发起工具调用(支持后缀:pdf/doc/docx/ppt/pptx/xls/xlsx/jpg/jpeg/png/bmp/tiff):
```
parse_document(path="/mnt/user-data/uploads/病历.pdf")
```
返回索引后,`read_file` 其 `document.md`;表格数 > 0 时逐个 `read_file` `tables/NNN.html`。

### A2. 整理为结构化 md
按下方**统一「内容格式」**把正文 + 表格整理写入目标 `.md`——统一命名 `workspace/ocr/{source}/{source}_full.md`(或任务指定路径),首行标注 `（来源文档：{原文件虚拟路径}）`(`/mnt/user-data/...`,⛔ 不写宿主机绝对路径)。**只有这个 `.md` 落盘才算该文档完成**,`parsed/<hash>/` 只是中间产物。

> 因为整份解析无页边界,路线 A 产出的证据只能标"来源文档 + 原文摘录",`evidence.page` 留空、无 `screenshot_ref`。若下游需要页码或原件截图定位,应改选路线 B(不是"两条都跑")。
> 路线 A 的来源行由**你**写；路线 B 的 `（来源图片：…）` 由 `parse_image_batch` 写,不要手工补。

### A3. 失败降级
- parse_document 返回 `Error:` 先**分类**（见「失败分类与降级」表）：格式/缺文件/超限、以及 `40007`/`40008`/`40003`/`4010x`（服务未开通、余额、凭证）→ 不重试；超时/网关故障 → **下一轮退避重试 1 次**。
- 重试后仍失败 → 转**路线 B**：`pdf_to_image.py` 把该 PDF 拆页，逐张图 parse_document（每轮 ≤ 2-3 张）。若错误码是 `40007`/`40008`/`40003`/`4010x`（服务未开通、账号/凭证问题），**降级也不会成功**——直接上报，不要把每页都失败一遍。
- 若拆页后单页仍 `Error:` → 用 **view_image 兜底**复核该页（见下），结构化表单/手写页尤其适用。

---

## 路线 B:分页渲染 → 逐张图 parse_document(需分页粒度 / 整份失败降级)

### B1. 拆页(文本层优先,仅扫描页渲染图片)
`pdf_to_image.py` 默认 `--text-mode auto` 逐页探测文本层:文本型页面(可提取字符 ≥ 阈值)直接写 `.txt`、**跳过渲染**;扫描型页面才渲染成图片。
```bash
python3 /mnt/skills/custom/pdf-image-extractor/scripts/pdf_to_image.py \
  /mnt/user-data/uploads/病历.pdf \
  --dpi 150 --format png --max-size 300 \
  --output-dir /mnt/user-data/workspace/images/{source_name}/
  # 默认 --text-mode auto --text-threshold 100
  # --max-size 300:单图 ≤300KB,超限自动降质→降采样。此处主要控制 TextIn 上传体积
  #   与 view_image 兜底时的 token 占用;真正的清晰度杠杆是 --dpi。
  # 若 TextIn 识别质量差,先降 DPI(150→120/100)再考虑其它手段。
```
运行后**先读 `{stem}_manifest.json`**,据 `pages[].type` 决定下一步:
```
读 manifest → 每页 type
├── type == "text"    → 跑 collect_text_pages.py 归集成 ocr/{source}/{stem}.md
│                        (逐字复制,零图片 token,不调 parse_document、不派 OCR 子代理)
└── type == "scanned" → 逐张图发起 parse_document(B2)
```
manifest 顶层 `text_pages` / `scanned_pages` 计数可快速评估图片 OCR 工作量。

> 其它模式:`--text-mode image-only` 恢复全渲染(仅在文本层探测误判时用);`--text-mode text-only` 确认为纯文本 PDF(全抽文本、不渲染)。
> 更高保真嵌入原图:`pdfimages -png <pdf> <prefix>`(poppler),与渲染页 MD5 去重后择优。

### B2. 批量 parse_image_batch（推荐，>2 张时优先）或逐张 parse_document

**批量路径（推荐）**：当需要 OCR 的图片 > 2 张时，用 `parse_image_batch` 一次性处理：

```
parse_image_batch(paths=[
  "/mnt/user-data/workspace/images/筛选期病历/筛选期病历_page_001.jpg",
  "/mnt/user-data/workspace/images/筛选期病历/筛选期病历_page_002.jpg",
  ...
])
```

工具返回每页的 OCR 索引，并**自动在 `ocr/{source}/` 下写入同 stem 的 `.md`**（含首行 `（来源图片：…）` 和 key-fields 速览），**无需逐张 `read_file` 再手工 `write_file`**。一次调用处理一个 source 的全部扫描页，比逐张 `parse_document` 省轮次、省上下文。

**逐张路径（≤2 张时）**：对每张 `type == "scanned"` 的分页图片发起 parse_document(遵循上方铁律):
```
parse_document(path="/mnt/user-data/workspace/images/筛选期病历/筛选期病历_page_001.jpg")
```
`read_file` 其 `document.md` + 表格,按**统一「内容格式」**写入同名 `.md`:
`images/筛选期病历/筛选期病历_page_001.jpg` → `ocr/筛选期病历/筛选期病历_page_001.md`（图片扩展名以实际 png/jpg 为准，OCR 产物固定 `.md` 且与图片同 stem）。

**幂等与完成判定**：
- 解析前先看 `ocr/{source}/{stem}.md` 是否已存在——**存在即跳过该页**，不重复 parse（补漏轮次同样适用）。
- `parsed/<hash>/` 只是中间产物；**只有 `ocr/{source}/{stem}.md` 落盘才算该页完成**。`parsed/` 有产物而 `ocr/` 为空 = 任务失败（历史故障：12 次 parse_document 产出 12 个 `parsed/` 目录，`ocr/` 却是空的）。
- 选定路线 B 后**不得**再对同一 PDF 整份 parse_document；也不得把同页另存单页 PDF / ASCII 副本再解析（见「解析去重铁律」）。

### B3. 单页失败
某张图 parse_document 返回 `Error:` → 瞬时错误（超时/网关故障）**下一轮退避重试 1 次**；仍失败则 **view_image 兜底**该页（每轮 ≤ 2-3 张），据原图按统一内容格式写出该页 `.md`，文件头注明 `（本页由 view_image 人工读图，非 TextIn OCR）`。**不重试整批**；覆盖率校验会捕获仍缺的页。

---

## 统一「内容格式」（路线 A/B 写 .md 时必须遵守）

防止关键字段淹没在长段落中（历史故障：`ECOG:1分` 被埋在 1200 字符密集段落被漏检）：

1. **首行标注来源**（分页图片：**工具已写好，不用你写**）：
   - **路线 B 分页图片** —— `parse_image_batch` 落盘时已在首行写入 `（来源图片：{虚拟路径}）`。
     ⛔ **不得重写、不得改写、不得删除、不得移到别处**。这一行是 `ocr_records.md` 页块的起始行，
     也是判定产物 `evidence[].page` / `screenshot_ref` 的唯一来源
     （契约见 `patient-separator/references/aggregate-ocr.md`）。
     若发现某页缺这一行（早期工具版本的遗留产物），重跑 `parse_image_batch` 即会就地补上，
     **不会**重复调用 OCR 服务（`repaired` 计数）；不要手工 `write_file` 去补。
     故障档案：thread `1fee1395` 的 7 页全无这一行，判定产物 `screenshot_ref`/`page` 归零。
   - **路线 A 整份 parse** —— 由你在写 `{source}_full.md` 时首行标注
     `（来源文档：{原文件虚拟路径}）`。整份解析**无页边界**，故 `evidence.page` 留空、
     无 `screenshot_ref`；需要页码/原件截图定位就改选路线 B（不是"两条都跑"）。
   - 路径一律用 `/mnt/user-data/...` **虚拟路径**，⛔ 不得写宿主机绝对路径（`/Users/…`），换部署即失效。
2. **页首/文首「关键字段速览」块**：把离散临床事实（评分/生命体征/关键检验值/分期/基因/诊断）抽成一行 HTML 注释，供下游 LLM 与确定性 grep 快速命中：
   `<!-- key-fields: ECOG=1分; 身高=145cm; 体重=50.0kg; KRAS=26.29%; MSI=MSS; ... -->`
   （只登记确有的字段；无则 `<!-- key-fields: (none) -->`）
3. **正文无损重排**：对同一段落内并列的临床事实（分号/句号/顿号分隔）逐条**断行**，一事实一行；**只做断行与保序，禁止改写、概括、删改任何原文数值与用词**。原始长段落照常保留，仅在其内部补换行提高可检索性。表格内容（来自 `tables/NNN.html`）随对应事实一并纳入断行整理，不得丢弃。
4. **通用要求：所有类型的识别结果必须包含日期**（检验/报告/检查/记录/取材/入出院日期）。

### 按内容类型的提取要点
- **化验单**：项目名 + 英文缩写 + 数值 + 单位 + 参考范围 + 异常标记(H/L) + **检验/报告日期**
- **影像报告**：检查类型 + 部位 + 影像所见全文 + 诊断印象/结论 + **检查 + 报告日期**
- **病历文书**：OCR 全文 + 结构化字段（主诉、现病史、**既往史（单独标注）**、个人史、体格检查、初步诊断）+ **记录日期**
- **病理报告**：病理诊断 + 组织学类型 + 分级 + 免疫组化标记物(逐项) + 切缘状态 + 淋巴结转移 + TNM 分期 + **取材 + 报告日期**
- **出入院小结/诊断**：入院 + 出院日期 + 入院诊断 + 出院诊断 + 诊疗经过摘要 + **记录日期**

### 数据处理规则
- **所有项目必须带日期**：每条化验值、影像结论、病理结果都关联其检查/报告日期。
- **区分既往史**：病历文书中的「既往史」必须单独标注，与本次就诊数据分开，避免混淆。
- **重复检验项取最新**：同一检验项目多次出现（如多次血常规），以最新一次为准用于入排匹配；历史值可保留作参考但需标注日期。

---

## view_image 兜底（路线 A/B 共用，仅在必要时）

**触发条件**：结构化表单/量表/含手写页的 OCR 文本不足或疑似错抄时，加载该页原图供**视觉模型**复核后再采信。**这不是 OCR 主路径**，不要对所有页无脑 view_image。

**⚠️ 批次硬约束**：
- 每轮 `view_image` 工具调用**最多 2-3 张**图片。
- **禁止**一轮发 5 张以上——base64 payload 过大会导致模型无法接收图像内容（历史故障：一次性对所有页 view_image，前面批次内容丢失）。
- 节奏：`view_image × 2-3` → 复核并 `write_file` → 下一批。
- 单图 ≤ 20MB（超过 view_image 限制的图需降采样后再加载）。

## 覆盖率校验 + 重复解析自检（路线 A/B 通用，防漏页/防重复计费）

**用脚本，不要手写统计**（分母算错是历史故障主因）：

```bash
python3 /mnt/skills/custom/pdf-image-extractor/scripts/ocr_coverage.py \
  --workspace /mnt/user-data/workspace [--json /tmp/coverage.json]
```

判定口径：
- 路线 **A**：该 source 只需一份 `ocr/{source}/{source}_full.md`。
- 路线 **B**：分母 = manifest 中**全部页**（`scanned` + `text`），每页都要有 `ocr/{source}/{stem}.md`；
  两类页补法不同，脚本分别报 `missing_scanned` / `missing_text`：
  - `type == "scanned"` → 补派 OCR 子代理；
  - `type == "text"` → 跑 `collect_text_pages.py` 机械归集，**禁止派 OCR**（图片没渲染，
    派了只会走 `view_image` 兜底白烧轮次）。
  ⛔ 分母**不是**只算 `scanned`：文本层页「不需要 OCR」≠「不需要进证据库」
  （故障 `69612125`：26 页里 11 页文本层含基因检测报告，只算 scanned 时报 `covered=True`，
  证据静默丢失，导致 `IN-4-1` 被判「无法判断：缺基因检测报告」）。
  ⛔ 也**不是** `total_pages`：更早一次故障用它做分母 → 文本层页永远判不覆盖、白跑补漏轮次。
- 路线取自 `pdf_classification.json.ocr_route`（缺文件时回退为扫 `images/` 并按 A 处理）。

输出与处置：
- `covered=False` → **只对 `missing` 补做解析**（幂等：已有 `.md` 的页/文档不得重跑），**未补全前不得标记 covered**。
- `duplicate_parse_suspected=True`（`parsed/` 解析产物目录数 > OCR 产出数）→ 立即停止解析，核对是否 A/B 双跑、整份+逐页重复、或解析了不该解析的文件（见「解析去重铁律」）。**禁止**"反正已经花了钱"继续叠加。

## 脚本参数

### `classify_uploads.py`（输入预检 + 类型判定 + 路线预设）
```
python3 .../scripts/classify_uploads.py --uploads DIR --out FILE [--images-dir DIR]
```
### `collect_text_pages.py`（文本层页归集，路线 B 必做）
```
python3 .../scripts/collect_text_pages.py --workspace DIR [--source NAME] [--json]
```
把 manifest 中 `type == "text"` 的页的 `.txt` 逐字写成 `ocr/{source}/{stem}.md`（幂等，
已存在且非空则跳过）。⛔ 不生成 `key-fields` 速览——那需要语义理解，脚本做等于编造。
`exit 2` = 有页归集失败（`.txt` 缺失或为空；为空说明文本层探测误判，该页应改按扫描页渲染 + OCR）。

### `ocr_coverage.py`（覆盖率 + 重复解析自检）
```
python3 .../scripts/ocr_coverage.py --workspace DIR [--json FILE]
```
### `pdf_to_image.py`（拆页渲染，本地免费）
```
python3 /mnt/skills/custom/pdf-image-extractor/scripts/pdf_to_image.py <pdf_path> [选项]

  --dpi N            分辨率（默认 300；入排逐页 OCR 用 150 足够，小字模糊升 300）
  --format png|jpeg  输出格式（默认 png，文字类推荐 png）
  --quality N        JPEG 质量 1-100（默认 95）
  --pages 1-3,5      页码范围（默认全部）
  --max-size N       单张最大 KB（默认 1024=1MB），超限自动降质→降采样，OCR 推荐 300
  --output-dir PATH  输出目录（默认 PDF 同目录）
  --text-mode M      auto(默认,文本页抽 .txt/扫描页渲染) | image-only | text-only
  --text-threshold N auto 模式判"文本型页面"的最小可提取字符数（默认 100）
  --recursive        递归处理子目录
```

## 关键约束

1. **parse_document 是工具、不是模块/CLI**：只能作为工具调用发起，禁止 import/find_spec/which/subprocess/pip（见上方铁律）。
2. **表格必读 `tables/*.html`**：TextIn 表格不在 `document.md`，索引表格数 > 0 时逐个 read_file，否则丢数据。
3. **OCR 质量自检**：识别后检查关键字段（诊断、化验值）是否大面积 null → 标记异常，结构化表单页可 view_image 复核。
4. **默认 DPI 150**（路线 B）：逐页 OCR 150 足够且避免图片过大；小字模糊升 300。
5. **文本页零图片**：`type == "text"` 页用 `collect_text_pages.py` 归集 `.txt` → `ocr/`，
   严禁再渲染/parse、严禁派 OCR 子代理。**归集是必做步骤**，不是可选优化——
   不归集则该页内容永远进不了 `ocr_records.md`，判定层看不到（故障 `69612125`）。
6. **每批 view_image ≤ 3 张**：payload 硬限制，一次太多模型收不到图像。
7. **保留源文件映射**：命名可追溯，OCR `.md` 与图片同 stem（`筛选期病历_page_001.jpg` ↔ `筛选期病历_page_001.md`）。
8. **全页覆盖校验**（路线 B）：按 manifest `total_pages` 校验文本页对应 `.txt`、扫描页对应图片 + OCR `.md`，缺页必补。
9. **路径纪律**：一律 `/mnt/user-data/...` 虚拟路径，禁止硬编码宿主机绝对路径。
10. **同一目标只解析一次 · A/B 互斥**：选定路线后不得对同一文件用另一路线重解析；页级幂等键 = `ocr/{source}/{stem}.md` 已存在则跳过；上游已提取为工作文件的文档（如入排标准 → `eligibility_criteria_raw.md`）禁止再解析（见「解析去重铁律」）。
11. **不得为文件名另存副本**：中文/全角括号/空格路径用引号或绝对路径处理，**禁止**复制 ASCII 副本、单页 PDF 等"换形态"再解析——这只会重复计费。
12. **`.md` 落盘才算完成**：`parsed/<hash>/` 是中间产物；路线 B 缺 `ocr/{source}/{stem}.md` 即该页未完成，路线 A 缺 `ocr/{source}/{source}_full.md` 即该文档未完成。
13. **空文件与 sidecar 先剔除**：`size == 0` 的文件禁止解析、禁止 read_file（读空文件不会有结果，只会卡流程）；同 stem 的 `.md` 是文档 sidecar，只作"有无文本层"判据，不作独立解析目标（见「输入预检」）。
14. **拆页不等于选定路线 B**：`pdf_to_image.py` 是本地免费渲染，可先拆页供 `view_image` 兜底；解析路线仍按路线选择表独立决定。
15. **工具选择**：单文件/单张图 → `parse_document`；批量图片 >2 张 → `parse_image_batch`（一次调用处理全部，自动写入 `.md`，省轮次省上下文）。路线 A/B 由上层按用户选择传入，**技能不替用户选**；A 的调用数是 1，B 推荐用 `parse_image_batch` 一次完成。曾有一轮 10 张图全失败被误判为"限流"，实际是 `40007` 机器人不存在（客户端 endpoint 路由 bug，已修）——**先读错误码再归因**。
16. **失败先分类再决定重试**：格式/缺文件/超限 → 不重试；`40007`/`40008`/`40003`/`4010x`（服务未开通、余额、凭证）→ 不重试也不降级，直接上报；超时/网关故障 → 退避重试 1 次 → 再降级（A → B → view_image，单向）。
