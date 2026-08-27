# 入排章节提取与完整性自检

> 本文件是「怎么从试验方案原文得到 `eligibility_criteria_raw.md`」的唯一权威。
> 解析（四分类拆分）从该文件开始，见 SKILL.md「双轨解析」。
>
> 输入：`/mnt/user-data/uploads/` 下 `role=protocol_criteria` 的方案/入排标准文档
> 输出：`workspace/eligibility_criteria_raw.md` + `workspace/criteria_meta.json` + 段行号记录

## 核心原则

**入排标准必须逐条完整、逐字提取，宁可多带上下文，绝不截断或概括。**

历史故障：排除标准源文件共 20 条，仅提取到第 9 条，漏掉第 10-20 条共 55%；
育龄期女性 FSH/停经等客观判定标准被概括为一句话而丢失。

- 禁止将整份方案传给子代理。
- 用 grep 定位 → read_file 提取入排章节 → 保存为 `workspace/eligibility_criteria_raw.md`。

## ① 边界锚定提取（防止读窗截断）

grep 同时定位**起点与终点**行号，而非猜测固定窗口：

- 入选起点：正文中的 `4.1 入选标准`（跳过目录/TOC 行——TOC 行通常带 `....53` 页码点串；取后接条目正文的那一处）
- 排除起点：正文中的 `4.2 排除标准`
- 提取终点：排除标准之后的**下一个章节标题**（如 `5 药物与治疗` / `5.1` / `4.3`）

`read_file(start_line, end_line)` 覆盖 **[入选起点, 终点] 的完整区间**，一次读全；
区间过大时分段连续读，**段与段行号必须衔接、不留空洞**。

⛔ **禁止用 `grep -n` / `awk NR` 定位行号，一律调脚本**：

```bash
python3 /mnt/skills/custom/criteria-parser/scripts/locate_criteria_sections.py \
  --protocol /mnt/user-data/uploads/试验方案.md --workspace /mnt/user-data/workspace
```

脚本落盘 `criteria_meta.json` 的 `段行号`（供双轨解析各读自己那段，避免两轨都读全文 token 翻倍）、
`末条号`（**源文件声明值**，自检基线）、`补充章节`（源文件里存在的入排相关章节清单）。

**为什么必须用脚本**：`read_file(start_line, end_line)` 内部是
`"\n".join(content.splitlines()[start-1:end])`，而 `str.splitlines()` 除 `\n` 外还在
`\f`(换页) `\v` `\x85` `\u2028` 等处断行 —— `grep -n` / `awk NR` 只认 `\n`，两套行号错位。
PDF 转出的方案 .md 往往每页一个 `\f`，错位可达上百行。脚本统一用 `splitlines()` 坐标系
（与 `read_file` 一致），落盘的行号可直接喂 `read_file`，并会显式报告偏移量。

> 历史故障 thread `ec24d087`：方案 .md 有 131 个 `\f`，`grep -n` 得「排除段 3820-3988」，
> `read_file` 按 splitlines 坐标实际读到原文 3755-3923，只拿到排除第 1..11 条（真实 20 条）；
> 自检脚本也用 `splitlines()` 数源末条号，同样得 11 —— **两个错误在同一坐标系里互相抵消**，
> `n == N` 成立、自检空过，9 条排除标准与研究周期等章节静默丢失。

## ② 逐字完整提取（禁止概括，⛔ 禁止主代理抄写）

- 提取**必须由脚本机械切片**：把「提取区块」（每块 `{标题, start, end}`，按 raw 顺序，
  行号取自 ① 的 splitlines 坐标）与「方案元数据」写进 `criteria_meta.json`，然后跑
  `extract_criteria.py` 落盘——脚本负责切片、组装、自检（④ 的闸内置于其中）。
  ⛔ **禁止用 `write_file` 抄写章节原文**（会话 `bc8a9bc7`：locate 已给出精确行号，
  主代理仍逐字重写 11,441 输出 token、耗时 215s——纯机械复制付了 LLM 生成价，
  且产物永久驻留主代理上下文）。
- 每条标准**逐字复制**，必须保留：所有子项（a/b/c、项目符号 •）、`注：` 说明、
  表格化内容（如 IN-10 实验室检查值的系统/指标/阈值）、以及可客观判定的判定标准
  （如"非育龄期女性"的 ≥50 岁停经 12 个月 / <50 岁停经 12 个月+FSH 标准、避孕方法列举）。
- **禁止**把多子项/表格/客观标准压缩成一句话（如"方案定义了非育龄期女性的判定标准"）——
  这类概括会丢失可从病例获取的客观条件，直接污染后续解析与判定。
- 页眉页脚噪声（重复的"方案编号…版本号…"、孤立页码数字、跨页断行）随切片**原样保留**：
  切片是逐字的，下游自检与解析对噪声稳健；⛔ 禁止以"清理噪声"为名手工改写切片
  （改写即引入丢条风险，`ec24d087` 的自检空过正始于此）。

## ③ 必须包含的内容

1. **方案基本信息**：方案编号、方案标题、适应症、研究阶段、申办方等
2. **入选标准**（完整章节，条目 `1..N` 全覆盖）
3. **排除标准**（完整章节，条目 `1..M` 全覆盖）
4. **与入排相关的必要补充章节**：研究设计摘要、访视窗口/筛选期定义、合并用药限制、
   附录引用（如 CYP3A4 强效抑制剂/诱导剂清单、延长 QT 间期药物清单）等

## ④ 完整性自检（强制，写入后执行，不通过不得进入解析）

提取与自检一条命令完成（`extract_criteria.py` 切片落盘后**内置** ① 的源基线核对，
`exit 2` 即未通过且不落盘半成品）：

```bash
python3 /mnt/skills/custom/criteria-parser/scripts/extract_criteria.py \
  --meta /mnt/user-data/workspace/criteria_meta.json \
  --source /mnt/user-data/uploads/试验方案.md \
  --out /mnt/user-data/workspace/eligibility_criteria_raw.md
```

对**既有** raw（或想独立复核）时用 `locate_criteria_sections.py --verify-raw`：
⛔ **自检必须调脚本，禁止自行 grep/awk 数条号**（`--verify-raw` 用 ① 落盘的**源基线**
核对提取结果，`exit 2` 即未通过）：

```bash
python3 /mnt/skills/custom/criteria-parser/scripts/locate_criteria_sections.py \
  --protocol /mnt/user-data/uploads/试验方案.md --workspace /mnt/user-data/workspace \
  --verify-raw /mnt/user-data/workspace/eligibility_criteria_raw.md
```

脚本查三件事：
1. **入选/排除条目全覆盖**：源声明 `1..N` / `1..M` 与提取结果逐号比对，报出**具体缺哪几条**
   及该段的 splitlines 行号区间（可直接喂 `read_file` 补读）。
2. **补充章节未漏**：源文件里存在的入排相关章节（研究周期/研究设计/访视/筛选期/合并用药/附录）
   必须在提取结果中出现。
3. 编号连续性对重复编号（跨页重复）与尾部杂散编号稳健：取「从 1 起的最长连续前缀」。

⛔ **不得把源末条号写成自己提取结果的条数**——那样 `n == N` 恒成立，自检变成循环论证
（`ec24d087` 正是如此空过）。源末条号只能来自脚本对**源文件**的统计。

**exit 2 时的处置**：按脚本给出的缺失编号与行号区间 `read_file` 补读 → 逐字合并进
`eligibility_criteria_raw.md` → **重跑脚本**，循环至 `exit 0`。补全内容必须逐字复制原文，
禁止概括或改写。

**元数据非空自检**：`criteria_meta.json` 的 `方案元数据` 至少要有 `方案编号` 或 `方案标题`
非空（历史缺陷：该块全空且无人认领，报告无法追溯来源；`judge_pack.py assemble` 现在会
因它为空而直接阻断）。脚本只写 `段行号`/`末条号`/`补充章节`/`行号坐标系`，**保留**既有
`方案元数据`，两者互不覆盖。

## `criteria_meta.json` 结构

全篇级信息由本阶段落盘一次，双轨解析都不产出它（避免两轨写出冲突的元数据）；
`judge_pack.py assemble` 合成全量包时注入。

```json
{
  "方案元数据": {
    "方案编号": "XS-03-II201",
    "方案标题": "……",
    "试验药品": "……",
    "版本": "V1.2",
    "版本日期": "2025-11-28",
    "来源": "试验方案.pdf 第4章（4.1入选标准/4.2排除标准）",
    "分期臂": ["Ib期", "II期"]
  },
  "段行号": {
    "入选": {"start": 812, "end": 903},
    "排除": {"start": 903, "end": 1041}
  },
  "末条号": {"入选": 13, "排除": 20},
  "补充章节": [{"行号": 3718, "编号": "3.6", "标题": "研究周期"}],
  "行号坐标系": "splitlines（与 read_file 一致）",
  "提取区块": [
    {"标题": "研究设计与研究周期", "start": 1400, "end": 1647},
    {"标题": "入选标准与排除标准", "start": 1648, "end": 1843}
  ]
}
```

`段行号` 用 `[start, end)` 半开区间，**splitlines 坐标系**（可直接喂 `read_file`，
⛔ 不可与 `grep -n` 行号混用）；`末条号` 是**源文件声明值**（脚本从源文件独立统计，
非提取结果的条数），供解析轨自查条目是否齐全。`段行号`/`末条号`/`补充章节`/
`行号坐标系` 四个字段由 `locate_criteria_sections.py` 落盘，禁止手写；
**`提取区块` 与 `方案元数据` 由主代理写入**（块边界与命名是语义工作），
`extract_criteria.py` 吃它做机械切片——区间互不重叠（重叠即拒绝），
成功后回写 `raw段行号`/`raw总行数` 回执。

## 禁止重复解析

`role=protocol_criteria` 的文档在本阶段提取完毕后，**后续所有阶段禁止再解析**
（`parse_document` / `read_file` 原始 `.docx`/`.pdf`）——其内容已逐字进入
`eligibility_criteria_raw.md`，再解析属重复（历史故障：`入排标准.docx` 被重复 parse 一次）。
若发现 `eligibility_criteria_raw.md` 有缺条，回到本文件的自检流程补提取，
**不要**去解析原始文档。
