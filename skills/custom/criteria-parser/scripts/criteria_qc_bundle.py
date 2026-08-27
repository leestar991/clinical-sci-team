#!/usr/bin/env python3
"""解析 QC 取证素材**预装配**（按轨）：把逐条 grep + 内联 python 的取证压成一次读。

## 为什么需要它：与判定侧同一个病，换了阶段

会话 `93d8a2c6` 的判定 QC 因逐条取证耗尽 150 步额度而失败，对策是
`eligibility-judgment/scripts/evidence_bundle.py`。会话 `e3c15416` 显示**解析 QC 同形复发**：

- `EX track QC round 2` 跑了 **77 步**，工具构成 `bash 28 / read_file 11`，撞上
  `max_turns=150` **失败**；其中 3 处是内联 `python3 -c` 只为把 `criteria_parsed_EX.json`
  读出来 print 几个字段。
- 全会话 **26 处**内联 `python3 -c`，绝大多数是 `json.load` + `print` 的只读自检。

成本模型（实测）：**计费 input ≈ (AI 步数 / 2) × 该 task 累积内容量**。取证内容本身很小，
贵在它被摊到几十步里、每步都重传此前全部历史。所以治法是**减少步数**，不是少读。

## 它装配什么：按**原条号**分组（这是与判定侧取证包的关键差别）

解析 QC 的五项语义检查（`criteria-qc-checklist.md` 第二层）问的都是
「**原文这一条** → 被拆成的这 N 条子条件，拆得对不对 / 转化得对不对 / 漏没漏」。
所以素材必须按原条号成组，raw 原文每组只贴一次：

1. **该原条号在 `eligibility_criteria_raw.md` 里的原文段**（一次，不按子条件重复）；
2. 该组每个子条件：`条件ID` / `子条件` / 分类 / `转化条件` 摘要（同义词·匹配字段·运算符·
   阈值·或条件·除外）/ `日期维度` / `或组`+`或组语义` / `描述索引` 里的短描述；
3. **机械预判**（省掉 QC 自己算）：
   - `原文` 能否在 raw.md 中逐字可查（口径与闸 9 完全一致，直接复用其归一化函数）；
   - `阈值` 是文字描述 → 提示「需按三档判据定档②/③」；
   - `运算符` 不在 `CANONICAL_OPERATORS` 内 → 闸 12 命中，并回报命中的外部标准名
     （命中即第三档 `upstream_issues` 可能性最高）。

## 它**不**做什么

⛔ 不下任何 QC 结论、不改任何产物、不替代语义 QC。定档仍由 QC 做——本脚本只把"要素"摆齐。
⛔ 也不替代 `check_track_structure.py`：那是**判据闸**（输出"哪条不合格"），本脚本输出"核这条
需要的原文与要素"。两者互补。

## 用法

    python3 /mnt/skills/custom/criteria-parser/scripts/criteria_qc_bundle.py \
        --workspace /mnt/user-data/workspace --track EX \
        --out /mnt/user-data/workspace/criteria_qc_bundle_EX.md

exit 0 = 已装配（即便存在原文查不到的条目——那是 QC 的判断对象）；
exit 2 = 输入不可读（`criteria_parsed_{TRACK}.json` 缺失或 JSON 坏掉），
         **或本轨段定位不可信**（见下）。

## ⛔ 段定位失败必须 exit 2，不得静默退回整篇

本脚本的每一组都以「该原条号在 raw.md 的原文段」开头，那是闸 9 比对与拆分核验的全部依据。
段定位靠 `_TRACK_HEADINGS` 匹配轨段标题；匹配不上时旧版**退回整篇**继续装配，于是
`clause_spans` 的「同编号保留最后一个」让本轨每条都取到后一段的原文 —— 产物看起来完整、
`exit 0`，原文却全错。这个形态在真实会话里发作了两次（都不是报错）：

| 会话 | 触发 | 表征 |
|---|---|---|
| `a7c19ea1` | 无 `track` 参数，两段条号同起编 | IN 轨每条取到排除段原文（诊断出但当时未能修） |
| `5aa5d6d6` | 标题是 `## 4.1 入选标准`，正则只认裸标题 | 同上；修完标题正则后 end 探测的 `#{1,3}` 又回溯命中段内 `#### 肿瘤性疾病`，EX 段在小标题处截断 → 20 条全部「未能定位」 |

`5aa5d6d6` 的代价：两轨 QC 子代理各自撞上错误映射，一个自行绕过、一个据此产出**假阳性
阻断项**（EX-6）；主代理为查这个 bug 花掉 10 分钟与两次全量脚本重写，而 13 条真阻断项
一条未修，会话随后被取消。

所以现在：段定位失败、或本轨段内一个条号都取不到 → `MappingUntrusted` → `exit 2` 并点名原因。
⛔ 宁可让装配失败，也不要交一份看起来正常、原文全错的取证包。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

CATEGORIES = {
    "IN": ("入选_可从病例获取", "入选_不可从病例获取"),
    "EX": ("排除_可从病例获取", "排除_不可从病例获取"),
}

# 产物自己不能变成新的上下文炸弹（判定侧取证包的教训）。
MAX_RAW_LINES_PER_GROUP = 14
MAX_GROUP_CHARS = 2400
MAX_BUNDLE_CHARS = 80000


def _load_gate_module():
    """复用 `check_track_structure.py` 的判据，⛔ 不在本脚本里重写。

    闸 9 的归一化（NFKC + 全删空白 + 视觉等价字符折叠）和闸 12 的标准运算符集合都是**口径**，
    重写一份就会漂移——两个脚本对同一条目给出不同结论，QC 只能靠猜。
    """
    path = Path(__file__).with_name("check_track_structure.py")
    spec = importlib.util.spec_from_file_location("_cts_for_bundle", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _category_entries(items: object) -> list[dict]:
    """把一个 `四分类` 类目容器归一成条目列表（dict 为当前形态，list 为旧 workspace 只读兼容）。

    ⛔ 不要写成 `if not isinstance(items, list): continue` —— dict 数据会被当成"没有条目"，
    取证素材包变空，而 QC 子代理只会看到"素材包已生成"（thread `3a745b38` 的教训）。
    形态是否合法由 `check_track_structure.py` 闸13 阻断，这里只做归一。
    """
    if isinstance(items, dict):
        return [it for it in items.values() if isinstance(it, dict)]
    if isinstance(items, list):
        return [it for it in items if isinstance(it, dict)]
    return []


def group_by_clause(data: dict, track: str, gate) -> dict[int, list[tuple[str, dict]]]:
    """原条号 → [(类目, 条目), ...]，按条件ID 自然序。"""
    groups: dict[int, list[tuple[str, dict]]] = {}
    buckets = data.get("四分类")
    if not isinstance(buckets, dict):
        return groups
    for cat in CATEGORIES.get(track, ()):
        for it in _category_entries(buckets.get(cat)):
            parsed = gate.parse_cid(str(it.get("条件ID") or ""))
            if parsed is None:
                groups.setdefault(-1, []).append((cat, it))
                continue
            groups.setdefault(parsed[1], []).append((cat, it))
    for n in groups:
        groups[n].sort(key=lambda pair: (gate.parse_cid(str(pair[1].get("条件ID") or "")) or ("", 0, 0))[2] or 0)
    return groups


_CLAUSE_LINE = re.compile(r"^\s*(\d{1,3})\s*[.．、)）]\s*\S")


#: raw.md 的轨段标题。入选段在前、排除段在后，两段的条号都从 1 重新起编。
#: ⚠️ 标题可能是「## 4.1 入选标准」/「## 4.2 排除标准」（带编号前缀），也可能是裸「## 入选标准」。
#: 第一版只匹配裸标题，遇到带「4.1/4.2」前缀的标题时 `_track_section_bounds` 定位失败、
#: 退回整篇，于是 `clause_spans` 的「保留最后一个同编号」bug（thread a7c19ea1）重新发作：
#: IN 轨每个原条号都拿到排除标准那一段的原文。这里允许可选的编号前缀。
_TRACK_HEADINGS = {
    "IN": re.compile(r"^\s*#{1,6}\s*(?:\d+(?:\.\d+)*\s*)?入选标准\s*$"),
    "EX": re.compile(r"^\s*#{1,6}\s*(?:\d+(?:\.\d+)*\s*)?排除标准\s*$"),
}

#: 用于识别「段结束」的任意标题行：1–3 级标题。⛔ 必须排除 4 级及以上
#: （`#### 肿瘤性疾病` / `#### 基础性疾病` 这类**段内小标题**不是段边界）——
#: 裸 `#{1,3}\s*\S` 会因 `\S` 接受第 4 个 `#` 而回溯命中 `####`，把排除段在第一个
#: 小标题处截断，导致该段所有原条号都定位不到原文窗口。
_HEADING_LINE = re.compile(r"^\s*#{1,2}(?!#)\s*\S")


def _track_section_bounds(raw_lines: list[str], track: str) -> tuple[int, int, str | None]:
    """本轨段的行区间（1-based，右开）+ 定位失败原因。

    返回 `(lo, hi, problem)`。`problem` 非 None 表示这次定位**不可信**，调用方必须把它
    冒泡成 `exit 2`，⛔ 不要当作"退回旧行为"继续装配 —— 这正是两个真实 bug 都能带着
    全错的映射 `exit 0` 通过的机制（见 `build_bundle` 的 `_MAPPING_UNTRUSTED_HINT`）。

    两种失败：
    * **标题定位不到**（`start == 0`）：退回整篇后，`clause_spans` 的「同编号保留最后一个」
      会让本轨每个条号都取到**后一段**的原文。
    * **段内无条号**（`hi <= start` 或区间内一个 `N．` 都没有）：段边界被段内小标题截断，
      本轨所有条号都定位不到窗口。
    第二种在这里只判断边界形状，"区间内有没有条号"由 `clause_spans` 判（它才有 marks）。
    """
    start = 0
    for i, line in enumerate(raw_lines, start=1):
        if _TRACK_HEADINGS[track].match(line):
            start = i
            break
    if start == 0:
        return (
            1,
            len(raw_lines) + 1,
            f"raw.md 里找不到 {track} 轨段标题（期望形如 `## 入选标准` / `## 4.1 排除标准`，"
            f"允许 1-6 级 `#` 与可选的章节编号前缀）",
        )
    end = len(raw_lines) + 1
    for i, line in enumerate(raw_lines[start:], start=start + 1):
        if any(pattern.match(line) for pattern in _TRACK_HEADINGS.values()) or _HEADING_LINE.match(line):
            end = i
            break
    return start, end, None


def clause_spans_checked(raw_lines: list[str], track: str | None = None) -> tuple[dict[int, tuple[int, int]], str | None]:
    """`clause_spans` + 定位可信度。返回 `(spans, problem)`；`problem` 非 None 即映射不可信。

    ⚠️ 第一版是「按 `原文` 锚定 + 固定 14 行」，实跑立刻露馅：原条号 1 给 L1-8、2 给 L3-8、
    3 给 L6-8 —— 第 3~8 行被贴了三遍。窗口必须由 raw 自身的条号边界决定，这样天然不重叠，
    也不会把后面几条的原文吞进来。（与 `evidence_bundle.py` 同一个教训。）

    ⚠️ 第二个 bug（会话 a7c19ea1 现场诊断，当时因技能目录只读而未能修）：同一条号在入排两段
    各出现一次（两段都从 1 起编），旧代码无条件「保留最后一个」并注释为"本轨段在后" ——
    这只对 EX 成立。跑 IN 轨时每个条号都会拿到**排除标准**那一段的原文，于是闸 9（`原文`
    与 raw 对照）按错误的窗口比对，报出的差异全是假的。给定 `track` 时只在本轨段内取条号。

    ⚠️ 第三次（会话 `5aa5d6d6`）：上面那个「按轨取段」的修复**依赖标题正则匹配得上**，而它
    两次都匹配不上真实的 raw.md —— 一次是标题带 `4.1/4.2` 编号前缀、一次是 end 探测的
    `#{1,3}` 回溯命中段内的 `#### 肿瘤性疾病`。两次的表征都不是报错，而是
    **带着全错的映射 `exit 0`**：IN 轨每条取到排除段原文、EX 轨 20 条全部「未能定位」。
    两轨 QC 子代理各自撞上后，一个绕过、一个据此产出了假阳性阻断项（EX-6），主代理为查这个
    bug 烧掉 10 分钟与两次全量脚本重写，而 13 条真阻断项一条都没修。

    所以本函数不只算窗口，还判断这次定位**可不可信**：段定位失败、或本轨段内一个条号都没有，
    都返回 `problem`。`build_bundle` 据此 `exit 2` —— 宁可让装配失败并点名原因，
    也不要交一份看起来正常、实则原文全错的取证包。
    """
    if track not in _TRACK_HEADINGS:
        # 不给 track（或未知轨）：保持旧的就近行为，不做可信度判断（其他调用方可能只要窗口）。
        lo, hi, problem = 1, len(raw_lines) + 1, None
    else:
        lo, hi, problem = _track_section_bounds(raw_lines, track)

    marks: list[tuple[int, int]] = []
    for i, line in enumerate(raw_lines, start=1):
        m = _CLAUSE_LINE.match(line)
        if m:
            marks.append((int(m.group(1)), i))
    spans: dict[int, tuple[int, int]] = {}
    for idx, (num, start) in enumerate(marks):
        if not (lo <= start < hi):
            continue
        end = (marks[idx + 1][1] - 1) if idx + 1 < len(marks) else len(raw_lines)
        # 本轨段的最后一条不能吞进下一段（排除标准/补充章节）的正文。
        end = min(end, hi - 1)
        end = min(end, start + MAX_RAW_LINES_PER_GROUP - 1)
        spans[num] = (start, max(start, end))

    # 段定位成功、raw 里也确实有条号，却在本轨段内一条都没取到 → 边界被段内小标题截断
    # （bug2 的形态）。空 raw 或整篇无条号不算异常：那是上游抽取的问题，闸 9 会报。
    if problem is None and track in _TRACK_HEADINGS and marks and not spans:
        problem = (
            f"{track} 轨段（L{lo}-{hi - 1}）内找不到任何 `N．` 条号行，而 raw.md 全篇有 {len(marks)} 个 —— "
            f"段边界疑似被段内小标题截断（`_HEADING_LINE` 只应匹配 1-3 级标题，"
            f"`#### 小标题` 不是段边界）"
        )
    return spans, problem


def clause_spans(raw_lines: list[str], track: str | None = None) -> dict[int, tuple[int, int]]:
    """`clause_spans_checked` 的窗口部分（向后兼容的薄封装）。

    ⛔ 装配路径请用 `clause_spans_checked` 并处理 `problem`：丢掉它就等于把「映射全错」
    降级成「静默产出错误素材」，那正是 `5aa5d6d6` 的失败形态。
    """
    return clause_spans_checked(raw_lines, track)[0]


# 明确的离散取值：它们**已经可执行**，不需要定档。
_DETERMINISTIC_VALUES = frozenset({"有", "无", "是", "否", "阳性", "阴性", "存在", "不存在", "已签署", "未签署", "男", "女", "不限"})
# 依赖外部评价/相对比较的信号词：命中即几乎肯定要定档（第②或第③档）。
_TIERING_SIGNALS = ("判断", "定义", "根据", "按照", "相比", "评价", "标准", "研究者", "临床", "显著", "适当", "必要")


def _needs_tiering(threshold: str) -> bool:
    """该 `阈值` 是否真的需要按三档判据定档。

    ⚠️ 第一版是「只要是字符串就标」，实跑把 `阈值="有"` 也标了 —— 那是明确的离散值、本来就可执行。
    把假阳报给 QC 等于让它白核一条，而本脚本存在的意义就是减少无谓步数。
    """
    value = threshold.strip()
    if not value or value in _DETERMINISTIC_VALUES:
        return False
    if any(sig in value for sig in _TIERING_SIGNALS):
        return True
    # 短取值（如 "需药物干预"）也可能是可执行的枚举，但边界模糊 —— 按长度保守判定。
    return len(value) > 5


def threshold_note(transform: dict, gate) -> list[str]:
    """机械预判：阈值形态与运算符是否需要按三档判据定档。"""
    notes: list[str] = []
    threshold = transform.get("阈值")
    if isinstance(threshold, str) and _needs_tiering(threshold):
        notes.append("`阈值` 是文字描述 → 需按三档判据定档（② 可结构化=阻断 / ③ 依赖外部标准=upstream）")
    operator = str(transform.get("运算符") or "").strip()
    if operator and operator not in gate.CANONICAL_OPERATORS:
        hit = [s for s in gate._REFERENCE_STANDARDS if s in json.dumps(transform, ensure_ascii=False)]
        extra = f"，命中外部标准 {hit}" if hit else ""
        notes.append(f"闸12 命中：`运算符`=`{operator}` 不在标准集合内{extra} → 第三档可能性最高")
    return notes


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"…[截断，原 {len(text)} 字符]"
    return text[: max(0, limit - len(marker))] + marker


class MappingUntrusted(Exception):
    """本轨段定位不可信 —— 禁止交付取证包（`exit 2`）。

    为什么是异常而不是产物里的一行警告：警告要靠 QC 子代理读到并正确解读，而实测两个 QC
    子代理面对同一个错误映射给出了两种反应（一个绕过、一个据此报了假阳性阻断项）。
    映射错了，整份取证包的「raw 原文」都是错的，它承载的正是闸 9 与拆分核验的全部依据。
    """


_MAPPING_UNTRUSTED_HINT = (
    "⛔ 取证包未产出：本轨原条号→raw 原文的映射不可信，交付它会让 QC 按错误原文核验\n"
    "   （真实后果见会话 `5aa5d6d6`：IN 轨每条取到排除段原文、EX 轨 20 条全部定位不到，\n"
    "   两轨 QC 一个绕过、一个据此产出假阳性阻断项）。\n"
    "→ 先修 raw.md 的轨段标题或本脚本的段定位正则，再重跑本命令；\n"
    "   ⛔ 不要跳过取证包直接派 QC（QC 会逐条 grep 取证并耗尽步数额度）。"
)


def build_bundle(workspace: Path, track: str) -> tuple[str, dict]:
    gate = _load_gate_module()
    data = load_json(workspace / f"criteria_parsed_{track}.json")
    raw_path = workspace / "eligibility_criteria_raw.md"
    raw_lines = raw_path.read_text(encoding="utf-8", errors="replace").splitlines() if raw_path.exists() else []
    raw_body = gate._norm_text("\n".join(raw_lines))
    index = data.get("描述索引") if isinstance(data.get("描述索引"), dict) else {}

    groups = group_by_clause(data, track, gate)
    spans, mapping_problem = clause_spans_checked(raw_lines, track)
    # raw.md 缺失/为空时不阻断：那时全篇没有原文可对照，每组都会如实标「未能定位」，
    # 闸 9 会独立报缺原文。只有「raw 有内容、却定位错了」才是本异常要拦的。
    if mapping_problem and raw_lines:
        raise MappingUntrusted(mapping_problem)
    stats = {
        "原条号组数": len(groups),
        "子条件数": sum(len(v) for v in groups.values()),
        "原文查不到": [],
        "需定档": [],
        "闸12命中": [],
        "raw 行数": len(raw_lines),
        "截断的组": [],
    }

    head = [
        f"# 解析 QC 取证素材包 — {track} 轨",
        "",
        "> 由 `criteria_qc_bundle.py` 机械装配，**一次读取即拿到全部取证素材**，按**原条号**分组。",
        "> ⛔ 不要再逐条 `grep` 原文、也不要写内联 `python3 -c` 去 print 字段 —— 那会把核验拆成",
        "> 几十步，而每一步都要重传此前所有步的上下文（实测 18×~30×；`e3c15416` 的 EX QC round 2",
        "> 就是这么用掉 77 步并撞上 max_turns=150 失败的）。",
        "> ⛔ 本文件不含任何 QC 结论；「机械预判」只是要素提示，定档与判级仍由你做。",
        "",
    ]

    blocks: list[str] = []
    for clause in sorted(groups):
        rows = groups[clause]
        lines = [f"## 原条号 {clause if clause > 0 else '（条件ID 不合规）'}", ""]
        window = spans.get(clause)
        if window:
            lines += [f"**raw 原文** `L{window[0]}-{window[1]}`", "```"]
            lines += [f"{n}: {raw_lines[n - 1].rstrip()}" for n in range(window[0], window[1] + 1)]
            lines += ["```", ""]
        else:
            lines += ["**raw 原文**：⚠️ 未能按 `原文` 定位到 raw.md 行区间（可能原文被改写）", ""]

        lines.append(f"**该原条号被拆成 {len(rows)} 条子条件**：")
        lines.append("")
        for cat, it in rows:
            cid = str(it.get("条件ID"))
            transform = it.get("转化条件") if isinstance(it.get("转化条件"), dict) else {}
            lines.append(f"- **{cid}**（{cat}）：{_truncate(str(it.get('子条件') or ''), 220)}")
            quote = gate._norm_text(str(it.get("原文") or ""))
            if quote and raw_body and quote not in raw_body:
                stats["原文查不到"].append(cid)
                lines.append("  - ⛔ **`原文` 在 raw.md 中逐字查不到**（闸9 口径）→ 疑似改写或凭空生成")
            if it.get("或组"):
                lines.append(f"  - `或组`={it.get('或组')}（{it.get('或组语义') or '⚠️ 语义缺失'}）")
            if transform:
                bits = []
                for key in ("同义词", "匹配字段", "运算符", "阈值", "或条件", "除外", "单位"):
                    if transform.get(key) not in (None, "", [], {}):
                        bits.append(f"`{key}`={json.dumps(transform.get(key), ensure_ascii=False)}")
                if bits:
                    lines.append(f"  - 转化条件：{_truncate('；'.join(bits), 420)}")
            elif "不可从病例获取" not in cat:
                lines.append("  - ⚠️ 「可从病例获取」却无 `转化条件`")
            if it.get("日期维度") not in (None, {}, ""):
                lines.append(f"  - 日期维度：{_truncate(json.dumps(it.get('日期维度'), ensure_ascii=False), 260)}")
            if index.get(cid):
                lines.append(f"  - 描述索引：{_truncate(str(index.get(cid)), 120)}")
            for note in threshold_note(transform, gate):
                lines.append(f"  - 🔎 {note}")
                (stats["闸12命中"] if note.startswith("闸12") else stats["需定档"]).append(cid)

        block = "\n".join(lines)
        if len(block) > MAX_GROUP_CHARS:
            block = _truncate(block, MAX_GROUP_CHARS)
            stats["截断的组"].append(clause)
        blocks.append(block + "\n")

    summary = [
        "## 装配摘要",
        "",
        f"- 原条号 {stats['原条号组数']} 组 → 子条件 {stats['子条件数']} 条；raw {stats['raw 行数']} 行",
    ]
    if stats["原文查不到"]:
        summary.append(f"- ⛔ `原文` 逐字查不到（阻断级线索）：{stats['原文查不到'][:15]}")
    if stats["需定档"]:
        summary.append(f"- 🔎 `阈值` 为文字描述、需按三档定档：{sorted(set(stats['需定档']))[:15]}")
    if stats["闸12命中"]:
        summary.append(f"- 🔎 闸12 命中（第三档 `upstream_issues` 可能性最高）：{sorted(set(stats['闸12命中']))[:15]}")
    summary.append("")

    text = "\n".join(head + summary) + "\n" + "\n".join(blocks)
    if len(text) > MAX_BUNDLE_CHARS:
        notice = "- ⚠️ 产物超长：raw 原文窗口已省略，需要时按各组给出的行号补读。"
        stripped = "\n".join(head + summary[:-1] + [notice, ""]) + "\n"
        for block in blocks:
            keep = [ln for ln in block.split("\n") if not ln.startswith("```") and not (ln[:1].isdigit() and ": " in ln[:8])]
            stripped += "\n".join(keep) + "\n"
        text = _truncate(stripped, MAX_BUNDLE_CHARS)
        stats["超长已省略窗口"] = True
    return text, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="装配解析 QC 取证素材包（把逐条取证压成一次读）")
    ap.add_argument("--workspace", required=True, help="工作目录（含 criteria_parsed_{TRACK}.json 与 eligibility_criteria_raw.md）")
    ap.add_argument("--track", required=True, choices=["IN", "EX"])
    ap.add_argument("--out", help="输出路径，默认 {workspace}/criteria_qc_bundle_{TRACK}.md")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace)
    try:
        text, stats = build_bundle(workspace, args.track)
    except FileNotFoundError as e:
        print(f"输入不存在：{e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"JSON 不可解析：{e}", file=sys.stderr)
        return 2
    except MappingUntrusted as e:
        print(f"⛔ 段定位不可信：{e}", file=sys.stderr)
        print(_MAPPING_UNTRUSTED_HINT, file=sys.stderr)
        # 上一轮的产物必须删掉：QC 子代理的开工前置只看「取证包在不在」，留着它等于让
        # 本轮的 exit 2 被一份**过期**素材包掩盖 —— 同一类「看起来正常」的静默失败。
        stale = Path(args.out) if args.out else workspace / f"criteria_qc_bundle_{args.track}.md"
        if stale.exists():
            stale.unlink()
            print(f"→ 已删除上一轮的过期产物 {stale.name}（避免 QC 拿它当本轮素材）", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else workspace / f"criteria_qc_bundle_{args.track}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"→ {out}  ({len(text):,} 字符)")
    print(f"  原条号 {stats['原条号组数']} 组 / 子条件 {stats['子条件数']} 条")
    if stats["原文查不到"]:
        print(f"  ⛔ `原文` 查不到：{stats['原文查不到'][:10]}")
    if stats["闸12命中"]:
        print(f"  🔎 闸12 命中：{sorted(set(stats['闸12命中']))[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
