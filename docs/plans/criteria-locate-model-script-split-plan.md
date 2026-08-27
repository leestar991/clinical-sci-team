# 入排章节定位：脚本正则改为「模型定位 + 脚本核对」分工

> 状态：**待实施**
>
> 提出时间：2026-08-17
>
> 触发会话：`d1a8b5bb-b4d3-45ae-a97c-406590aea4a8`（Phase 1 用了 61 步，其中 26 步在调试本脚本）
>
> 关联历史故障：`ec24d087`（9 条排除标准静默丢失）、`6e5ac7c1`（read_file 越界返回空串，凭空编造 92% 条目）
>
> 目标文件：
> - `skills/custom/criteria-parser/scripts/locate_criteria_sections.py`（356 行）
> - `skills/custom/criteria-parser/references/criteria-extraction.md`（136 行）
> - `tests/skills/test_locate_criteria_sections.py`（34 个用例）

---

## 0. 一句话

把脚本里**「猜章节在哪」的正则定位（`locate()`）删掉交给模型**，把**「核对有没有漏条」的机械自检（`verify_raw()`）保留并强化**，中间加一个**纯机械的坐标系探针**兜住 `grep -n` 与 `read_file` 的行号错位。

---

## 1. 动机：一次事故 + 一个不可收敛的假设

### 1.1 事故账（会话 d1a8b5bb 实测）

Phase 1 共 61 步，其中 **seq 15–44 共 26 步、约 6 分钟、占墙钟一半**花在调试本脚本，而非做预处理。真正的预处理只用了约 20 步。

起点是 `08:12:27` 第一次运行脚本就失败：

```
⛔ 未能在 试验方案.md 正文中定位章节标题：入选标准、排除标准
Exit Code: 2
```

之后 agent 进入 13 轮反向工程：`grep` 找标题 → `sed` 看原文 → `grep` 脚本里的正则名 → **三次 `read_file` 分段读完 349 行脚本**（seq 23/29/35）→ 三段 `python3 -c` 手工验证正则 → 试探 `/mnt/skills` 可写性 → 两次 heredoc 打补丁 → 重跑通过（`08:18:35`）。

失败原因是两个正则与本方案格式不匹配：

| 脚本原有假设 | 本方案实际 |
|---|---|
| 标题形如 `4.1 入选标准`（编号后有空白） | `###### 4.1入选标准`（`#` 前缀 + 无空白） |
| 条目形如 `1. ` / `1、`（半角） | `1．`（全角句点 U+FF0E） |

agent 当场补出的正则（现已在文件里）：

```python
ITEM_RE = re.compile(r"^[ \t]{0,3}#{0,6}[ \t]*(\d{1,2})[.．、](?!\d)")
MD_NUM_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(\d{1,2}(?:\.\d{1,2})*)([^\d\s．、。，,：:；;].*)$")
```

**注意这次修复是有效的** —— 它确实让脚本在格式 A 上跑通了。问题不是这次修得对不对，而是下一种格式还要再修一次。

### 1.2 格式不可穷举（实测两种共存格式）

workspace 里同时存着两种结构完全不同的方案文件：

| | 行数 | md 标题数 | 全角条目 `1．` | 半角条目 `1.` | 换页符 `\f` | HTML 表格行 |
|---|---|---|---|---|---|---|
| **A**（本次会话） | 4180 | 240 | 51 | 177 | 0 | 969 |
| **B**（`ec24d087` 同类） | 8249 | **1** | 0 | 417 | **131** | 0 |

A 靠 `######` markdown 标题定位；B 几乎没有 markdown 标题，靠纯文本行 + 131 个换页符。**一套正则不可能同时吃下这两种**，而 OCR 输出格式还会继续变。这不是"再补几个正则"能收敛的问题 —— 每种新格式都是一次 20+ 步的赌博。

### 1.3 成本对比：脚本失败一次 ≫ 模型读全文二十次

会话 d1a8b5bb 的方案文件，入排章节只占全文 **1.8%**：

| | 行数 | 字符 | ≈token（1.65 字符/token，仓库中文实测口径） |
|---|---|---|---|
| 全文 | 4180 | 141,790 | 85,900 |
| 入排章节（1651–1843） | 193 | 2,596 | **1,573** |

即"整文档丢给模型"的上限是 86k token/次。而脚本失败一次已经烧掉 26 步 / 6 分钟。**脆弱的省 token 手段比它省下的更贵。**

---

## 2. 为什么不能走「完全交给模型」

三条硬约束，缺一不可。

### 2.1 坐标系错位：`grep -n` 与 `read_file` 在格式 B 上差 131 行

`read_file(start_line, end_line)` 在 sandbox 里的实现是

```python
content = "\n".join(content.splitlines()[start_line - 1 : end_line])
```

`str.splitlines()` 除 `\n` 外还在 `\f` `\v` `\x1c` `\x1d` `\x1e` `\x85`(NEL) `U+2028` `U+2029` 处断行，而 `grep -n` / `awk NR` **只认 `\n`**。实测：

```
A(4180/本次):        grep -n 行数=4180  splitlines 行数=4180  偏移=0
B(8118/ec24d087类):  grep -n 行数=8118  splitlines 行数=8249  偏移=131
```

所以"让模型自己 `grep -n` 定位再 `read_file`"在格式 A 上碰巧没事，在格式 B 上**直接复现 `ec24d087`**：模型 grep 得「排除段 3820-3988」，read_file 实际落在 3755-3923，只读到排除第 1..11 条（真实 20 条）。

⚠️ **这是本计划最容易做错的地方**：单纯删掉脚本、让模型 grep，等于把 `ec24d087` 的坑重新挖开。必须保留坐标探针。

### 2.2 越界静默返回空串

`read_file` 对越界切片返回**空字符串而非报错**（`if not content` 的兜底在切片之前）。thread `6e5ac7c1` 由此凭空编造了 92% 的条目 —— 子代理拿到空输入却不知道。

### 2.3 漏条无机械信号

模型漏一条排除标准，没有任何东西会报错。在临床入排筛选里这是最不能接受的错误：**漏一条排除标准直接导致患者误判入组**。`verify_raw()` 是目前唯一能拦住它的东西，且它的判据是纯机械的 —— 数条目编号、查从 1 开始的连续性、比对补充章节标题是否都在：

```python
nums = item_numbers(seg)
got = last_contiguous(nums)
if got != src_max:
    missing = [n for n in range(1, src_max + 1) if n not in set(nums)]
    problems.append(f"⛔ {track}标准丢条：源文件声明 1..{src_max}，提取结果只到 1..{got}（缺 {missing}）...")
```

**这部分不需要任何格式假设**，只需要知道"源里最大条号是多少"，而这个数字模型能可靠地报。

---

## 3. 方案：三段分工

| 环节 | 谁做 | 为什么 |
|---|---|---|
| ① 坐标系探针（断行符扫描 + 两套行号偏移） | **脚本**（保留 `scan_breaks`） | 纯机械，且是 §2.1 的唯一防线 |
| ② 找到入排章节在哪、逐字提取 | **模型**（`grep` 粗筛 → `read_file` 带 range 精读） | 格式无法穷举；模型对三种标题写法一视同仁 |
| ③ 条号连续性 + 补充章节完整性核对 | **脚本**（保留 `verify_raw`，改为接收模型报的基线） | 机械可判定，是拦住静默漏条的唯一门禁 |
| ④ 定位正则 `locate()` | **删除** | 本次 26 步的唯一来源，注定跟不上格式变化 |

### 3.1 关键设计：`--verify-raw` 的基线从哪来

现状 `verify_raw(loc, raw_path)` 的 `loc` 来自 `locate()` 自己算的 `末条号`。删掉 `locate()` 后基线必须换源，这里有一个**循环论证陷阱**必须避开。

脚本原注释说得很清楚（`locate_criteria_sections.py:23-24`）：

> 源末条号由脚本从**源文件**独立算出，不再由主代理从自己的提取结果里数 —— 这是 `--verify-raw` 能真正发现丢条的前提（否则永远是循环论证）。

所以新方案里，末条号必须由模型**从源文件区间**读出并显式传入，而非从它自己写的 raw.md 里数。命令行契约：

```bash
# ① 坐标探针（无格式假设，永不失败）
locate_criteria_sections.py --protocol <方案.md> --probe

# ② 模型定位后，把区间与末条号交给脚本核对
locate_criteria_sections.py --protocol <方案.md> --workspace <ws> \
    --in-range 1651:1725 --in-last-item 11 \
    --ex-range 1725:1843 --ex-last-item 20 \
    --verify-raw <ws>/eligibility_criteria_raw.md
```

脚本仍**独立复核**模型报的末条号：用 `item_numbers()` 在模型给的源区间里重数一遍，与 `--in-last-item` / `--ex-last-item` 比对。不一致就阻断并报出两个数 —— 这样既不循环论证（基线在源文件区间上重算），又能捕捉模型报错数字的情况。

`--probe` 的输出要**自带可操作指引**，这是替代那 13 轮反向工程的关键：

```
坐标系：splitlines（与 read_file 一致）
⚠️ 检出异常断行符：\f 换页×131 → splitlines 比 grep -n 多 131 行
⛔ 禁止把 grep -n / awk NR 的行号直接喂给 read_file，先按下方换算
   grep 行号 N → read_file 行号 ≈ N + (该行之前的断行符数)
候选标题行（splitlines 坐标，已排除目录行）：
   3706  4.1 入选标准
   3820  4.2 排除标准
```

即**探针直接给出 splitlines 坐标系下的候选行号**，模型不必自己做坐标换算 —— 这一步机械且零格式假设（只需搜"入选标准"/"排除标准"字样并统计其前的断行符）。

### 3.2 保留 / 删除清单

| 函数 | 处置 | 理由 |
|---|---|---|
| `scan_breaks` | **保留** | §2.1 防线 |
| `last_contiguous` | 保留 | 核对用 |
| `item_numbers` | 保留 | 核对用 |
| `split_raw` / `raw_section_lines` | 保留 | 切分 raw.md 自身，与源格式无关 |
| `verify_raw` | **保留 + 改签名** | 接收模型报的区间/末条号，独立复核 |
| `merge_meta` / `summarize` | 保留 + 适配 | 落盘字段不变，加 probe 输出 |
| `_norm` / `LocateBlocked` | 保留 | 工具函数 |
| **`locate`** | **删除** | 26 步的来源 |
| **`_headings`** | **删除**（仅 `locate` 用） | 同上 |
| **`HEADING_RE` / `MD_NUM_HEADING_RE` / `MD_HEADING_RE`** | **降级** | 不再用于定位；`_is_heading_line` 仍需判 raw.md 标题，保留但只作用于 raw.md（数百行、由模型自己写、格式可控） |
| `ITEM_RE` | 保留 | 数条号用，需覆盖全角/半角/`#` 前缀 |
| `IN_TITLE_RE` / `EX_TITLE_RE` | 保留 | probe 搜候选行 + raw.md 分段 |
| `SUPPLEMENT_KEYWORDS` | 保留 | 补充章节核对；probe 一并搜出候选 |

⚠️ `_is_heading_line` 的作用域收窄是**有意为之**：它服务于 `split_raw`，切的是 raw.md —— 那是模型自己按固定模板写的文件，标题格式由 `criteria-extraction.md` 规定，不是 OCR 的野格式。这与被删掉的 `locate()`（要吃任意 OCR 输出）性质完全不同。

---

## 4. 实施任务

按 TDD：每个任务先写失败用例，再改实现。

### Task 1 — 用两种真实格式固化回归基线（先做，不改实现）

在 `tests/skills/test_locate_criteria_sections.py` 增加两个 fixture，分别复刻格式 A / B 的结构特征：

- **A**：`###### 4.1入选标准` + 全角 `1．` 条目 + 无换页符 + HTML 表格行
- **B**：纯文本 `4.1 入选标准` + 半角 `1.` 条目 + 每页一个 `\f`

先只加**探针与核对**的用例（此时会失败，因为 `--probe` 还不存在）。这一步的价值是把 §1.2 的格式差异写进测试，防止未来又用单一格式验证。

现有 34 个用例中锁坐标系语义的那批（`test_form_feed_shifts_coordinates_and_is_reported`、`test_grep_style_line_numbers_would_be_wrong`、`test_section_lines_feed_read_file_correctly`、`test_raw_section_lines_feed_read_file_on_raw_md`、`test_meta_records_both_coordinate_systems` 等）**必须全部继续通过** —— 它们守的是 `ec24d087` / `6e5ac7c1`。锁 `locate()` 定位行为的那批（`test_toc_lines_are_skipped`、`test_terminator_missing_falls_back_to_eof`、`test_missing_section_titles_blocked`、`test_exclusion_before_inclusion_blocked`、`test_numbered_item_not_mistaken_for_heading`）随 `locate()` 一起改写为 probe 的候选行为。

### Task 2 — 新增 `--probe` 模式

实现 §3.1 的探针输出：断行符扫描 + 坐标偏移 + splitlines 坐标系下的候选标题行 + 补充章节候选。

硬要求：**probe 永不因格式而失败**。找不到候选就把"文件里所有含『入选』『排除』字样的行"全列出来，附行号与原文，让模型自己判断。这正是那 13 轮探索在手工做的事。

### Task 3 — `verify_raw` 改为接收显式基线

改签名，加 `--in-range` / `--in-last-item` / `--ex-range` / `--ex-last-item`。脚本在模型给的源区间上用 `item_numbers()` 独立重算末条号并与模型报的值比对，不一致则阻断（§3.1 的反循环论证要求）。

### Task 4 — 删除 `locate()` 与 `_headings()`

此时相关用例应已由 Task 1/2 改写完毕，删除后全绿。

### Task 5 — 改写 `criteria-extraction.md` 的流程指令

现状第 30 行写的是：

> ⛔ **禁止用 `grep -n` / `awk NR` 定位行号，一律调脚本**

这条要改，但**不能简单反转**成"随便 grep"。新指令的语义是：

- 先跑 `--probe`，从**探针输出**里取 splitlines 坐标系的候选行号
- 允许用 `grep` 做内容层面的粗筛（找哪些行提到入排），但**行号一律以 probe 输出为准**
- 提取完成后必须跑带 `--verify-raw` 的核对，不通过不得进入下一阶段

第 42-48 行关于 `\f` 错位与 `ec24d087` 的说明**原文保留** —— 它解释的坐标系陷阱依然存在，只是防线从"脚本替你定位"变成"探针告诉你正确坐标"。

### Task 6 — 跑全量 skill 测试

`tests/skills/` 下与 criteria 相关的三个文件（`test_locate_criteria_sections.py`、`test_criteria_qc_bundle.py`、`test_criteria_qc_bundle_handover.py`）全绿，且 `test_expected_outputs_contract.py` / `test_soul_skill_contract.py` 不回归（它们可能引用了脚本的 CLI 契约）。

---

## 5. 预期收益

| | 上下文成本 | 定位步数 | 失败模式 |
|---|---|---|---|
| **现状**（脚本正则定位） | 1.6k token | 1 步 / **26 步** | 格式不匹配 → 长时间调试，且不可预测 |
| 纯模型（读全文） | 86k token | 1–2 步 | **静默漏条**（不可接受） |
| **本方案** | ~4k token（probe 输出 + 193 行区间） | 2–3 步 | 漏条被 `verify_raw` 拦下并报出缺哪几条 |

上下文比现状高约 **2.4k token** —— 相对本次会话 2.65M 总量是 **0.09%**，可忽略。换来的是消除整类"格式变化导致 20+ 步调试"的失败，同时保住机械门禁。

---

## 6. 风险与不做的事

### 6.1 风险：探针候选行过多

若方案正文多处提及"入选标准"（本次会话格式 A 里有 4 处：目录 2 行 + HTML 表格 2 行 + 正文标题），probe 会列出全部。缓解：probe 输出按"是否目录行（`\.{4,}`）"和"是否 HTML 表格行（含 `<td`）"分组标注，正文候选优先列出。**不做自动挑选** —— 那等于把 `locate()` 的猜测逻辑换个地方重建。

### 6.2 风险：模型报错末条号

由 §3.1 的独立复核兜住：脚本在源区间上重数一遍。若模型报的区间本身就错（比如漏掉了排除段尾部），重数结果会小于真实值，但**与模型报的值一致**，复核就发现不了。这是本方案的残余风险 —— 缓解手段是 `verify_raw` 额外检查"排除段末尾之后的下一个非空行是否仍是条目行"，若是则提示区间可能截断。此项列为 Task 3 的子项。

### 6.3 不做的事

- **不动 `skills/custom/` 的 git 跟踪问题**（按你指示忽略）。但请注意：该目录 gitignored，所以 agent 本次修好的正则、以及本计划的改动都只存在于本机文件系统。
- **不改 `read_file` 的越界静默行为**（§2.2）。那是 sandbox 工具层的事，影响面远超本 skill，应单独立项。
- **不动双轨解析、QC、判定阶段**。本计划只覆盖 Phase 1 的章节定位与提取自检。
- **不引入新的第三方依赖**。

---

## 7. 验收口径

1. 两种真实格式（A 4180 行 / B 8249 行）都能在 **3 步内**完成定位（probe → read_file → verify），无脚本调试
2. 格式 B 上故意用 `grep -n` 行号去读，`verify_raw` 必须报丢条（复现 `ec24d087` 的检出能力）
3. 故意从 raw.md 删掉排除标准第 7 条，`verify_raw` 必须报 `缺 [7]` 且 exit 2
4. 现有 34 个用例中锁坐标系语义的那批全部继续通过
5. `--probe` 对一个既无 markdown 标题、又无编号、纯乱格式的输入**不得抛异常**，须降级为"列出所有含入排字样的行"
