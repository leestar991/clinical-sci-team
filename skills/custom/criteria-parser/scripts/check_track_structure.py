#!/usr/bin/env python3
"""单轨标准结构闸（解析产出自检 + QC 前置 + 修订后守恒记账）。

把「进语义 QC 之前该查什么、修订之后该查什么」收敛成一个确定性脚本，
避免编排层每次用内联 python 现写一遍（规则漂移的根源）。

历史故障 thread `5d987e97` 一次踩中本脚本的三个闸：

1. 解析子代理在 `四分类` 用子条件粒度（`EX-4-1/-2/-3`）而在 `描述索引` 用父条粒度（`EX-4`），
   形成两套 ID 体系：`描述索引` 缺 10 项、多 7 项 → 闸 5。
   ⚠️ **该案的对齐方向后来被证明是反的**：`描述索引` 描述的是原始标准条款，**主条件粒度才是
   正确形态**（两份 HTML 报告都按主条件ID 查它）。当年按「以四分类实体为准修描述索引」收敛，
   等于把索引改坏；会话 `9a83ccc9` 照此产出 64 个子条件键、0 个主条件键，报告主条件行只能
   回退成「首个子条件文本」，`IN-2` 显示成「年龄 ≥ 18 周岁」，静默丢掉了 ≤70 岁的上限。
   闸 5 现按**主条件ID** 双向对齐——仍然抓「两套 ID 体系」，只是锚在正确的那一套上。
2. 该结构不一致没有先收敛就派了 `task(quality-control)`，语义 QC 把结构噪声当语义问题报出，
   白吃掉本轨仅有的 3 轮配额之一。
3. 主代理用单次全量 `write_file` 重写整轨文件做修订，`EX-7` 实体条目**整条消失**，
   而总条数「符合预期地下降了」（QC 要求的合并本身就会减少条数），
   靠看总条数发现不了 → 闸 6 / 闸 7 直接按原条号点名。

## 闸清单（任一不过 → exit 2）

| # | 闸 | 判据 |
|---|-----|------|
| 1 | 顶层结构 | JSON 合法；有 `四分类`；**只含本轨 2 个类目**；不得含 `方案元数据`/`解析说明`/`汇总统计`（全篇级，由 `judge_pack.py assemble` 负责）|
| 2 | 条件ID 唯一 | 全轨无重复条件ID |
| 3 | 前缀与轨一致 | IN 轨全 `IN-*`、EX 轨全 `EX-*` |
| 4 | 子序号规范 | 同一原条号不得「一个带子序号、一个不带」混用；带子序号时 1..N 连续无缺号 |
| 5 | 描述索引双向对齐 | `描述索引` 键集合 == `四分类` 实体的**主条件ID** 集合（`miss_in_index` / `extra_in_index` 均空）|
| 6 | 原条号全覆盖 | 需 `criteria_meta.json.末条号`：原条号 `1..末条号` **每个都至少有 1 个实体条目** |
| 7 | QC 目标实体存在 | 需 `--qc`：`blocking_issues[].condition_id` 的每个原条号，修订后仍有实体 |
| 8 | QC 原地打转探测 | 需 `--qc`：本轮 `blocking_issues` 的条件ID 集合与上一轮**完全相同** → 修订无效或问题在上游原文 |

闸 8 的动机（历史故障 thread `345f2bf4`）：入选第 10 条原文在 PDF→MD 提取中丢了否定词，
QC 第 3/4/5 轮阻断项完全一致，两次触顶暂停都没打破循环——因为出路（回原件核实那个否定词）
在 QC 与修订的权限之外。命中闸 8 时应改走 `references/criteria-qc-checklist.md` 的
`upstream_issues` 路径，而不是再试一种写法。轮次历史记在 `criteria_qc_history_{TRACK}.json`。

每次运行都会落盘 `criteria_structure_gate_{TRACK}.json`（`exit_code` + 被检文件内容哈希），
供 QC 子代理开工前自检前置、未过闸时自行拒工（不依赖主代理守规矩）。

闸 6 是**无基线**的丢条探测器：合并只会减少子序号、绝不会让一个原条号从文件里整体消失
（QC 的动作只有合并/补拆/改写/补回，没有"删除整条原文标准"）。

闸 5 的**对齐方向唯一**：以 `四分类` 实体的**主条件ID** 为准修 `描述索引`（一条原始标准一个键），
⛔ 不得反向删实体、也不得把索引改成子条件粒度。

闸 11 的动机（规则变更 2026-08-06）：OR 关系从「整体保留不拆分」改为「异质替代分支拆成
并行原子子条件 + `或组`/`或组语义` 标记」。两轨的组级汇总方向相反（入选=任一满足即整条满足、
排除=任一触发即整条触发），入选轨漏标或标反，判定侧就会按约束 18「全部入选符合」把
"满足其一即可"读成"必须全部满足" —— 例如 IN-5（PSA 进展/软组织进展/骨病灶进展）患者只满足
一支、另两支为「无法判断」时，整体被错判为不符合入选，等于错误淘汰患者。组标记是纯结构信息，
适合机械校验；"这几支到底是不是同一件事"需语义判断，只给建议级提示。

闸 11 的第二次补强（会话 `1fee1395`）：旧实现只遍历「`或组` 非空」的条目，`逻辑关系` 字段
**从不被读取**——一条该是 OR 组、却标 AND 且没有 `或组` 的条目对它完全不可见，只能等第二层
语义 QC 报出来。该会话 EX 轨 R2 的阻断项全是这一类（EX-9-1..6 与 EX-4-1/4-2 的 AND→OR 误标），
白吃掉轮次配额。现补三项：① `逻辑关系` 收成枚举 `单条件`/`AND`/`OR分支`（阻断）——该字段此前是
自由文本，同一个 OR 组出现过 `"OR分支（同组：IN-10-OR）"` 与 `"AND（同组：IN-10-OR…同时与
IN-10-6 AND关系）"` 两种相反写法，后者的 AND 指跨组关系却会被读成组内 AND；跨组说明改写进
`逻辑关系备注`（不校验）。② 标了 `OR分支` 必须同时有 `或组`（阻断）。③ 某原条号下全部子条件
标 AND 且无 `或组`、而原文含**列举式** OR 连接词 → 建议级（不阻断：真 AND 拆分的原文也可能
含裸 `或`，如「14天内未接受输血或G-CSF支持」，误阻断就白吃一轮配额）。

闸 10 的动机（历史故障 thread `afb85bcd`，是 `345f2bf4` 那句血小板标准的延续）：
QC R1 把 `IN-10-4`/`IN-10-6` 正确归入 `upstream_issues`（原文丢否定词，结构化层无解），
主代理随即指示修订子代理「先不要硬改成确定结论；保持忠实原文，不要伪造修复」——
子代理**正确地**完全不动（result 明确回报「未做伪造性『确定修复』」）。但「不动」保留的
恰是解析时写下的「两项均无」确定性表达，它超出原文；于是 R2 的 QC 正确判为
「上游歧义未隔离/第二档子类型」并把这两项**升级为 blocking**，占掉 R2 四项阻断里的两项；
R3 中性化之后才回落到 `upstream_issues`。upstream ↔ blocking 振荡白烧了一整轮配额。
根因是流程缺一步：「核实完成前，已写入的超范围表达该怎么办」没有规定，
于是「别伪造修复」被合理地理解成「别动」。闸 10 把这一步变成机械要求。

闸 9 的动机（历史故障 thread `6e5ac7c1`）：委派模板把 `criteria_meta.json.段行号`
（**试验方案.md** 的坐标，约 3766-4052）用于 `read_file` **eligibility_criteria_raw.md**
（仅 794 行）→ 越界切片，`read_file` 返回**静默空字符串**（`if not content: return "(empty)"`
在切片之前，兜不住越界），两个解析子代理拿到空输入后按通用 mCRC 知识**凭空编造**了
54 条中的 50 条（92%），出现原方案完全没有的 `DPD 缺乏`/`Gilbert 综合征`/`第三间隙积液`。
闸 1-8 全过 —— 因为**条件ID 体系是自洽的**（IN-1..11、EX-1..20 全覆盖、子序号连续、
描述索引对齐），没有任何一道闸比对过内容与原文。闸 9 就是这道缺失的内容锚。
禁止为了让索引对上而删改实体条目或退回拆分粒度——拆分粒度只能由语义 QC 的
`blocking_issues` 驱动改变。

闸 9 的两次补强（会话 `d393714d`：EX 轨解析 863k/23 步 → **2.81M/54 步**）：原实现只说
"查不到"，agent 于是反复读本脚本源码逆推归一化规则、再逐字符比对 raw。两个真因与对策 ——
① NFKC **不折叠**的视觉等价字符（`·` 间隔号族、`–—‐～` 破折号族、零宽、中英引号）：
   `_CHAR_FOLD_TABLE` 逐类折叠。⛔ 比较符与数字**不折叠**：`≥3 个月`→`>3 个月` 是真实篡改。
② `a) b) c)` 分支在原方案里跨行排布，拼进一条 `原文` 后不再是连续子串：
   `_segments_match_in_order` 允许按 `；`/`/` 逐段命中，用「每段 ≥8 字 + 顺序单调 + ≤6 段」
   三重约束挡住乱序拼接与"剁碎碰运气"。
失配时 `原文失配定位` 直接给出首个失配偏移 / 最长匹配前缀 / raw 最相近片段 / 建议动作，
处置手册见 `references/criteria-repair.md`「闸9 失配的处置」。

## 用法

    # 解析产出后 / 每轮语义 QC 之前（⛔ 不得与 task(quality-control) 同轮发出）
    python3 check_track_structure.py --workspace /mnt/user-data/workspace

    # 修订前留基线（可选，用于修订后看清哪些条件ID 消失/新增）
    python3 check_track_structure.py --workspace /mnt/user-data/workspace --track EX --snapshot

    # 修订后：带上本轮 QC 报告，按 blocking_issues 点名核对目标实体
    python3 check_track_structure.py --workspace /mnt/user-data/workspace --track EX \\
        --qc /mnt/user-data/workspace/criteria_qc_EX.json

`--track` 省略时检查 workspace 下存在的两轨。exit 0 = 全过；exit 2 = 有闸未过（禁止推进）。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

# `upstream_issues` 条目「已标注待核实」的识别词。命中任一即认为已中性化（闸 10）。
UPSTREAM_PENDING_MARKS = ("待核实", "原文歧义", "缺否定词", "原文缺词", "不可判定", "待人工确认")

TRACK_CATEGORIES = {
    "IN": ("入选_可从病例获取", "入选_不可从病例获取"),
    "EX": ("排除_可从病例获取", "排除_不可从病例获取"),
}
TRACK_META_KEY = {"IN": "入选", "EX": "排除"}
# 全篇级字段：单轨文件不得产出（两轨都写会冲突 / 必须由 assemble 重算）
FORBIDDEN_TOP_KEYS = ("方案元数据", "解析说明", "汇总统计")
CID_RE = re.compile(r"^(IN|EX)-(\d+)(?:-(\d+))?$")


def parse_cid(cid: str) -> tuple[str, int, int | None] | None:
    """`EX-4-2` → ("EX", 4, 2)；`EX-9` → ("EX", 9, None)；不合规 → None。"""
    m = CID_RE.match(str(cid).strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), (int(m.group(3)) if m.group(3) else None)


# NFKC **不折叠**的字符差异，逐类列出（事实 4.3）。全角 `＞`(U+FF1E) 这类 NFKC 本就折叠，不必管；
# 真正把 EX 轨解析拖进 54 步字符级循环的是下面这些：PDF→MD 提取会把间隔号、破折号、引号换成
# 视觉相同但码位不同的字符，而 agent 从报告里只看到「查不到」，只能逐字符试。
_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x00AD],
    None,  # 直接删除
)
_CHAR_FOLD = {
    # 间隔号族 → `·`（NFKC 只把 U+FF65 折到 U+FF65，不同族之间不互折）
    "\u00b7": "·", "\u2022": "·", "\u2027": "·", "\u30fb": "·", "\u2219": "·", "\u16eb": "·",
    # 破折号 / 连字符 / 波浪号族 → `-`
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u2212": "-", "\u2043": "-", "\u02d7": "-", "\uff5e": "-", "\u301c": "-", "\u223c": "-",
    # 引号族 → 直引号
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"', "\u3003": '"',
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
}
_CHAR_FOLD_TABLE = {ord(k): v for k, v in _CHAR_FOLD.items()}
_CHAR_FOLD_TABLE.update(_ZERO_WIDTH)


def _norm_text(s: str) -> str:
    """空白全删 + NFKC 归一 + 视觉等价字符折叠，用于 `原文` 与 raw.md 的逐字包含性比对。

    PDF→MD 提取会带入不稳定的空格/换行（`年龄 18 周岁` vs `年龄18周岁`）与全半角差异，
    直接子串匹配会大量假阳性；抹掉空白后比对既稳健又不放过真正的改写。

    ⚠️ 折叠范围**只含视觉等价**的码位差异（`·`/`–—‐`/零宽/中英引号）。语义字符一律不动——
    折叠 `≥`↔`>` 或数字宽度会让"把 ≥3 个月改写成 >3 个月"这种真实篡改蒙混过关，那正是本闸要抓的。
    """
    folded = unicodedata.normalize("NFKC", s or "").translate(_CHAR_FOLD_TABLE)
    return re.sub(r"\s+", "", folded)


# OR 分支的分隔符。`原文` 跨行拼接 `a) b) c)` 后不再是 raw 的连续子串（拼接时丢掉了 `a)` 标记），
# 但每个分支仍是。
#
# ⛔ **不含 `、`**：它在单句内做并列（"肝、肾功能"），拿它切段等于把任何长句拆成碎片，碎片各自
#   都能在 raw 里找到 → 本闸失效。
# ⛔ **不含 `或`**：第一版含了它，结果真实分支 `6 个月内发生过心肌梗死或不稳定性心绞痛` 被拆成
#   `不稳定性心绞痛`（7 字）而低于最小长度，跨行拼接用例反而被拦。`或` 连接的两项在 raw 里本来
#   就是连续的（空白已删），无需切段。
_OR_SPLIT = re.compile(r"[;；/]")

# 分段匹配的三重约束（防放过真实改写）
_MIN_SEGMENT_LEN = 8  # 每段归一化后的最小字符数
_MAX_SEGMENTS = 6  # 分段数量上限


def _segments_match_in_order(quote: str, body: str) -> bool:
    """`原文` 按 OR 切段后，是否**逐段均为 raw 子串且位置顺序单调**。

    为什么需要它：`a) …… 或 b) …… 或 c) ……` 这类分支列表在原方案里跨行排布，解析方把分支
    拼进一条 `原文` 后，整串不再是 raw 的连续子串——但每个分支都是。没有这条，EX 轨会陷入
    「逐字符比对 raw」的死循环（`d393714d`：2.81M token / 54 步）。

    三重约束缺一不可：
    - **每段 ≥ 8 字**：短段（"性别不限"、"≥3 个月"）在长文里几乎必然能找到，放过短段等于不设闸；
    - **位置顺序单调**：防乱序拼接——把 c) 的内容挂到 a) 的条件下是真实的语义篡改；
    - **段数 ≤ 6**：段数越多越接近"把改写后的句子剁碎后逐块碰运气"。
    """
    segments = [seg for seg in (s.strip() for s in _OR_SPLIT.split(quote)) if seg]
    if len(segments) < 2 or len(segments) > _MAX_SEGMENTS:
        return False
    if any(len(seg) < _MIN_SEGMENT_LEN for seg in segments):
        return False
    cursor = 0
    for seg in segments:
        found = body.find(seg, cursor)
        if found < 0:
            return False
        cursor = found + len(seg)  # 顺序单调：下一段必须在本段之后
    return True


def _mismatch_diagnosis(quote: str, body: str) -> dict:
    """`原文` 查不到时，给出**改哪个字符**级别的定位，而不是只说"查不到"。

    没有这个，agent 只能反复 read_file 脚本源码逆推归一化逻辑再逐字符试（事实 4.4）。
    返回的偏移是**归一化后**的坐标，所以同时给出前缀原文，让人能对上位置。
    """
    matcher = difflib.SequenceMatcher(None, quote, body, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]
    prefix_len = 0
    for block in blocks:
        if block.a == prefix_len:  # 从头开始连续对上的部分
            prefix_len = block.a + block.size
    longest = max(blocks, key=lambda b: b.size, default=None)
    near = ""
    # 「raw 里有没有相近片段」是**相对**判断：短 `原文` 的最长公共段天然就短。
    # ⚠️ 第一版用了 `>= _MIN_SEGMENT_LEN`（8 字）的绝对阈值，结果 `预期生存期>3 个月。`（把 ≥ 改成 >
    # 的单字符篡改，公共段 5 字）被判成"凭空生成，须整轨重做"——把只需改一个字符的活儿升级成整轨
    # 重做，是最贵的一种误导。改为占 `原文` 的比例 + 绝对下限。
    if longest is not None and longest.size >= 4 and longest.size / max(len(quote), 1) >= 0.35:
        # raw 中与 quote 最长公共片段的邻域，让 agent 直接看到原字符长什么样
        start = max(0, longest.b - 12)
        near = body[start : longest.b + longest.size + 12]
    diag = {
        "首个失配偏移": prefix_len,
        "最长匹配前缀": quote[:prefix_len][-40:],
        "失配处": quote[prefix_len : prefix_len + 24],
        "raw最相近片段": near,
    }
    if near:
        diag["建议"] = "疑似字符差异：照抄 `raw最相近片段` 里的原字符（含标点与全半角），⛔ 不要改写语义"
    elif len(_OR_SPLIT.split(quote)) > 1:
        diag["建议"] = f"疑似 OR 分支跨行拼接：每个分支需 ≥{_MIN_SEGMENT_LEN} 字、顺序与原文一致、分支数 ≤{_MAX_SEGMENTS}"
    else:
        diag["建议"] = "raw 中找不到任何相近片段 → 疑似凭空生成，须整轨重做本轨解析"
    return diag


def category_entries(items: object) -> list[dict]:
    """把一个 `四分类` 类目容器归一成条目列表。

    形态有两种：

    * **dict（当前形态）** —— key 是 `条件ID`。这是 `apply_json_patches` 能按身份定位的前提：
      `/四分类/{类目}/{条件ID}/...` 命中即命中、不命中即报错。
    * **list（旧 workspace，只读兼容）** —— 历史数据还没跑迁移脚本时仍能被读。

    ⛔ **不要把"形态不认识"写成静默 `continue`。** 这是本函数存在的全部理由：改 dict 之前，
    每个消费点都是 `if not isinstance(items, list): continue`，于是 dict 数据被当成"没有条目"。
    实测后果是结构闸报 `total=0 / problems=[] / ✅ 全过`（`描述索引` 恰好为空时），而 QC 子代理
    的开工前置自检只看 `exit_code`，于是在一份"零条目"的视图上正常开工；`描述索引` 非空时更糟——
    闸5 会把它误诊成「丢条，必须补回实体」，指挥 agent 去删索引键或重复补条目。
    形态判定交给调用方的 `_check_container_shapes`（闸13），本函数只做归一。
    """
    if isinstance(items, dict):
        return [it for it in items.values() if isinstance(it, dict)]
    if isinstance(items, list):
        return [it for it in items if isinstance(it, dict)]
    return []


def _check_container_shapes(data: dict, report: dict) -> None:
    """闸 13：类目容器形态合法，且 dict 的 key 逐字等于条目的 `条件ID`。

    key 与字段冗余是有意的（消费方与人读都依赖字段），但冗余必须有机械判据：两者不一致时
    「按 key 定位」与「按字段遍历」会得到不同答案，那就是下一个漂移源。

    形态本身也在这里阻断——`category_entries` 对无法识别的形态返回空列表，若不在此报错，
    后面每一道闸都会看到"零条目"并给出误诊。
    """
    for cat, items in (data.get("四分类") or {}).items():
        if isinstance(items, list):
            report["notes"].append(f"闸13 类目 `{cat}` 仍是数组（旧形态，只读兼容）→ 修订前请先跑 scripts/migrate_criteria_parsed_to_dict.py，数字下标寻址会漂移")
            continue
        if not isinstance(items, dict):
            report["problems"].append(f"闸13 类目 `{cat}` 既不是对象也不是数组（{type(items).__name__}）→ `四分类` 的每个类目必须是以 `条件ID` 为键的对象")
            continue
        for key, item in items.items():
            if not isinstance(item, dict):
                report["problems"].append(f"闸13 类目 `{cat}` 的 `{key}` 不是对象（{type(item).__name__}）")
                continue
            cid = item.get("条件ID")
            if not isinstance(cid, str) or not cid.strip():
                report["problems"].append(f"闸13 类目 `{cat}` 的 `{key}` 缺少 `条件ID` 字段（key 与字段必须并存且一致）")
            elif cid.strip() != key:
                report["problems"].append(f"闸13 类目 `{cat}` 的 key `{key}` 与条目 `条件ID`=`{cid}` 不一致 → 按 key 定位与按字段遍历会得到不同答案，必须改成一致")


def collect(data: dict, track: str) -> tuple[list[str], list[str]]:
    """返回 (按出现顺序的实体条件ID, 出现在非本轨类目里的条件ID)。"""
    ids: list[str] = []
    off_track: list[str] = []
    allowed = TRACK_CATEGORIES[track]
    for cat, items in (data.get("四分类") or {}).items():
        for it in category_entries(items):
            cid = it.get("条件ID")
            if cid is None:
                continue
            ids.append(str(cid))
            if cat not in allowed:
                off_track.append(str(cid))
    return ids, off_track


# `或组语义` 的合法取值，按轨固定。两轨的汇总方向相反，写反会让整体结论反转，
# 因此值与轨的配对由闸 11 机械校验，不留自由文本空间。
OR_GROUP_SEMANTICS: dict[str, str] = {
    "IN": "任一满足即整条满足",
    "EX": "任一触发即整条触发",
}

# `或条件` 分支数达到该值且未拆成 `或组` 时给建议级提示。2 支很常见是同一事实的
# 单位/参考范围变体（睾酮 <50 ng/dL 或 <1.7 nmol/L），3 支起才更可能是异质替代分支。
_OR_SPLIT_ADVISORY_MIN_BRANCHES = 3

# `逻辑关系` 的合法取值。收成枚举的理由（会话 `1fee1395` 实测）：该字段此前是自由文本，
# 同一个 OR 组里出现过 `"OR分支（同组：IN-10-OR）"` 与
# `"AND（同组：IN-10-OR…同时与IN-10-6（胆红素）AND关系）"` 两种相反写法——后者的 `AND`
# 指的是跨组关系，下游却会读成组内 AND（R3 建议项 `CQC-R3-S02`）。两轨的用词也各自演化
# （EX 写 `"OR（异质替代分支，或组 EX-1-OR）"`、IN 写 `"OR分支（同组：IN-5-OR）"`）。
# 取值沿用既有词汇（`单条件`/`AND`/`OR分支`），迁移只需去掉括号里的散文。
# 跨组/跨条的关系说明搬到 `逻辑关系备注`，自由文本，不参与校验。
LOGIC_RELATIONS: tuple[str, ...] = ("单条件", "AND", "OR分支")
LOGIC_REMARK_FIELD = "逻辑关系备注"

# 列举式 OR 连接词：出现这些词，说明原文在**罗列可替代的多项**，而非用 `或` 连接同一事实的
# 两种写法。裸 `或` 不算——IN-10 血细胞条「14天内未接受输血或G-CSF支持」是 AND 拆分却含 `或`。
_OR_ENUMERATING_MARKERS = ("和/或", "任一", "任何一项", "以下之一", "其中之一", "以下任何", "或者")

# ── 闸 12：阈值/运算符可执行性分档 ──────────────────────────────────
# `转化条件.运算符` 的标准取值（`criteria-parser/SKILL.md` 的字段定义）。`∈` 与 `in` 等价，
# 两种写法都收（实测 IN-9 用 `∈` 写 `ECOG in [0,1]`），不为拼写差异制造噪声。
CANONICAL_OPERATORS: frozenset[str] = frozenset({"≥", "≤", ">", "<", "=", "!=", "in", "∈", "不限"})

# 引用型外部评价标准：阈值的判定要件写在这些标准里、不在方案原文里。命中即更可能属
# `criteria-qc-checklist.md` 的**第三档**（`upstream_issues`，首轮归档、不占阻断额度），
# 而不是第二档（可结构化未结构化 → 阻断）。
_REFERENCE_STANDARDS = (
    "PCWG3",
    "PCWG2",
    "PCWG",
    "iRECIST",
    "mRECIST",
    "RECIST",
    "CTCAE",
    "NYHA",
    "Child-Pugh",
    "Lugano",
    "IMWG",
    "IWCLL",
)


def _iter_items(data: dict):
    """yield 四分类下的每个条目 dict。"""
    for items in (data.get("四分类") or {}).values():
        for o in category_entries(items):
            if o.get("条件ID"):
                yield o


_TOKEN_RE = re.compile(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}")


def _tokens(value: object) -> set[str]:
    """把任意字段值拆成词元集合（拉丁词 ≥2 字母、汉字串 ≥2 字）。"""
    if value in (None, "", [], {}):
        return set()
    return set(_TOKEN_RE.findall(json.dumps(value, ensure_ascii=False)))


def _check_cross_entry_pollution(data: dict, report: dict) -> None:
    """闸 14（**建议级**）：转化条件与本条原文完全不搭 → 疑似跨条目数据错位。

    判据来自 thread `3a745b38` 的指纹：数字下标漂移只改 `匹配字段`/`阈值`，而 `同义词` /
    `证据位置` / `原文` / `子条件` 原封不动（单字段 pointer 的必然结果）。于是"转化条件的词元与
    本条描述性字段**零交集**"就是一个可机械计算的错位信号。

    实测（该事故的 `criteria_parsed_EX.json`，44 条）：命中 12 条，其中 9 条正是 QC 第 3 轮点名的
    污染条目；误报 3、漏报 3（只换了阈值、匹配字段仍对的三条）。精确率/召回各约 75%。

    ⛔ **只能是建议级。** 3 条误报若做成阻断就要白吃一轮 QC 配额——与闸11/闸12 注释里已写明的取舍
    一致。本闸的定位是**兜底**：dict 化之后新的错位路径已由 `apply_json_patches` 的 key 寻址关掉
    （不命中即报错），它服务的是上线前已被污染的历史数据、以及将来可能出现的非工具写入路径。
    """
    suspects: list[str] = []
    for it in _iter_items(data):
        conv = it.get("转化条件")
        if not isinstance(conv, dict):
            continue
        left = _tokens(conv.get("匹配字段")) | _tokens(conv.get("阈值"))
        if not left:
            continue
        right = _tokens(it.get("原文")) | _tokens(it.get("子条件")) | _tokens(conv.get("同义词")) | _tokens(conv.get("证据位置"))
        if right and not (left & right):
            suspects.append(str(it.get("条件ID")))
    if suspects:
        report["notes"].append(
            f"闸14（建议级）{len(suspects)} 条的 `转化条件.匹配字段`/`阈值` 与本条 `原文`/`子条件`/`同义词`/`证据位置` "
            f"**零词元交集**，疑似跨条目数据错位：{sorted(suspects)}。"
            "逐条核对这几条的转化条件是否属于本条标准；确认错位就按正确内容重写该条目的 `转化条件`。"
            "（历史成因 thread `3a745b38`：数字下标漂移只改 匹配字段/阈值，描述性字段原封不动——正是这个指纹。"
            "本闸约 25% 误报，仅作提示、不阻断。）"
        )


# ── 闸 15：定性阈值 × 定量要件混用 ────────────────────────────────
# 一条子条件的 `转化条件` 只有一组 `运算符`/`阈值`，无法同时表达定性判定与定量阈值。
#
# 故障 `EX-12-1`（会话 `c80c47d9`，本会话唯一未收敛项）：原文是「活动性乙型肝炎（HBsAg
# **阳性** 且 HBV-DNA **> 10^3 IU/ml**）」，修订方写成 `阈值='阳性'` + `运算符='='` 同时
# 作用于 `匹配字段=['HBsAg','HBV-DNA']`，而 HBV-DNA 是数值型病毒载量（IU/ml），不存在
# 阳性/阴性。修订方没有可用字段承载并列的定量要件，于是**自创了 schema 外的 `并列条件`**，
# 被 QC 记进 residual。闸 12 查不出——`=` 是合法运算符，它只看运算符在不在标准集合内。
#
# 该条从第 2 轮才被发现（`criteria_qc_history_EX.json` 两轮 blocking_ids 零交集），
# 到第 3 轮 `passed` 仍为 false。正确做法是按 `parsing-rules.md` 的「跨字段类型 AND 必拆」
# 拆成两条，各自持有类型一致的 `运算符`/`阈值`。
_QUALITATIVE_THRESHOLDS: frozenset[str] = frozenset({"阳性", "阴性", "有", "无", "存在", "不存在", "可见", "未见", "是", "否"})

# 定量要件：比较符紧跟数值（`>10^3`、`≥ 3`、`<1.5`）。**必须带比较符** —— 裸数字会把条号
# （「排除标准第 12 条」）、剂量描述、年份统统命中，几乎每条都误报。
_QUANTITATIVE_RE = re.compile(r"[><≥≤]\s*\d")


def _check_qualitative_quantitative_mix(data: dict, report: dict) -> None:
    """闸 15（**阻断级**）：定性阈值用在原文含定量要件的条目上。

    双重判据（缺一不可）：`阈值` 是定性词 **且** 本条的 `子条件` 含 `[><≥≤]\\s*\\d` 形态的
    定量要件。只看前者会误伤正常的定性条目（`HIV 抗体阳性`，无定量要求）；只看后者会误伤
    本就用数值阈值的条目。裸数字不算定量要件——否则原文里的条号数字就足以触发。

    ⛔ **判据看 `子条件` 而不是 `原文`**：同一原条号的多个子条件**共享**整段 `原文`（忠实原文
    的正确做法）。实测 EX-12 四条共享「乙肝 HBV-DNA>10^3 / 丙肝 / HIV / 梅毒」整段，定量要件
    只属 EX-12-1，按 `原文` 判会把 EX-12-2/3/4 三条一起误报——3 条误阻断就是一轮 QC 配额。
    `子条件` 是本条专属的，才是正确的判据来源；缺 `子条件` 时才退回 `原文`。
    """
    offenders: list[str] = []
    detail: list[str] = []
    for it in _iter_items(data):
        conv = it.get("转化条件")
        if not isinstance(conv, dict):
            continue
        threshold = conv.get("阈值")
        if not isinstance(threshold, str) or threshold.strip() not in _QUALITATIVE_THRESHOLDS:
            continue
        source = str(it.get("子条件") or it.get("原文") or "")
        hit = _QUANTITATIVE_RE.search(source)
        if hit:
            cid = str(it.get("条件ID"))
            offenders.append(cid)
            # 点出定性阈值原值与命中的定量片段：agent 要据此定位改哪个字段，
            # 只给条件ID 会逼它再读一次条目（闸9 的教训：`d393714d` 因"只说查不到"3 倍恶化）。
            detail.append(f"{cid}（阈值={threshold.strip()!r} × 定量片段 {source[max(0, hit.start() - 12) : hit.end() + 8].strip()!r}）")
    if offenders:
        report["problems"].append(
            f"闸15 {len(offenders)} 条的 `转化条件.阈值` 是定性词，而本条 `子条件` 含定量要件"
            f"（`>`/`≥`/`<`/`≤` + 数值）：{'；'.join(detail)}。"
            "一组 `运算符`/`阈值` 表达不了「定性判定 + 定量阈值」两种类型 —— "
            "按 `references/parsing-rules.md` §拆分原则「必须拆分（AND）」的**跨字段类型**判据"
            "拆成两条子条件（同一 `逻辑关系=AND`、`逻辑关系备注` 写明配对），各自持有类型一致的"
            "`运算符`/`阈值`。"
            "⛔ 禁止自创 `并列条件` 之类 schema 外字段承载定量要件"
            "（故障 `c80c47d9`/`EX-12-1`：QC 第 3 轮仍未收敛的唯一残留项）。"
        )


# ── 闸 16：跨 `可从病例获取` 边界的 AND 未拆 ──────────────────────
# `EX-1` 原文是「MSI-H/dMMR（客观检测） 且 经研究者评估适合免疫治疗（主观）」。这个 AND
# 必拆：不拆则整条只能标一个 `可从病例获取` —— 标 true 会让判定侧去病历找研究者评估
# （必然「无法判断」），标 false 会丢掉可客观核验的 MSI 检测。
#
# 会话 `c80c47d9` 里解析子代理**自己想明白了**（`逻辑关系备注` 写着"客观检测与研究者评估
# 按可获取性拆分"），但那靠判断力而非规则：`parsing-rules.md` 的 AND 例外只写了「限定性
# AND 不拆」，按字面 `EX-1` 完全可以被当成限定性 AND 留在一条内。
_SUBJECTIVE_MARKS = ("研究者评估", "研究者判断", "研究者认为", "受试者自述", "受试者承诺", "研究者的判断", "预计", "预期生存")


def _check_obtainability_split(data: dict, report: dict) -> None:
    """闸 16（**建议级**）：单个 AND 实体的原文含主观评估要件 → 疑似跨可获取性未拆。

    只在该原条号下**只有一个** `可从病例获取=true` 的 AND 实体、且没有对应的
    `可从病例获取=false` 兄弟时才提示 —— 已拆的正确形态（兄弟取值不一致）必须静默，
    否则正确做法反而被报错。

    建议级而非阻断：判定「原文是否含主观评估要件」靠词表，词表必有假阳
    （原文可能只是在描述研究者的其他动作）。与闸 11/12/14 的取舍一致。
    """
    by_parent: dict[str, list[dict]] = {}
    for it in _iter_items(data):
        parsed = parse_cid(str(it.get("条件ID") or ""))
        if not parsed:
            continue
        by_parent.setdefault(f"{parsed[0]}-{parsed[1]}", []).append(it)

    suspects: list[str] = []
    for siblings in by_parent.values():
        obtainable = [s for s in siblings if s.get("可从病例获取") is True]
        not_obtainable = [s for s in siblings if s.get("可从病例获取") is False]
        if not_obtainable:
            continue  # 已按可获取性拆开 —— 正确形态
        for it in obtainable:
            if it.get("逻辑关系") != "AND":
                continue
            source = f"{it.get('原文') or ''}\n{it.get('子条件') or ''}"
            if any(mark in source for mark in _SUBJECTIVE_MARKS):
                suspects.append(str(it.get("条件ID")))
    if suspects:
        report["notes"].append(
            f"闸16（建议级）{len(suspects)} 条标 `逻辑关系=AND`、`可从病例获取=true`，但原文含主观评估要件"
            f"（{'/'.join(_SUBJECTIVE_MARKS[:3])}…），且该原条号下没有 `可从病例获取=false` 的兄弟条目："
            f"{sorted(suspects)}。"
            "若这个 AND 的一半是客观病历数据、另一半需研究者/受试者主观参与，按 "
            "`references/parsing-rules.md` §拆分原则的**跨可获取性**判据拆成两条"
            "（客观那条 `可从病例获取=true`，主观那条 false）—— 不拆则整条只能标一个值，"
            "标 true 会让判定侧去病历找主观评估（必然「无法判断」），标 false 会丢掉可客观核验的一半。"
            "（正确样板见会话 `c80c47d9` 的 EX-1-1/EX-1-2。本闸靠词表判定、有假阳，仅提示不阻断。）"
        )


def _check_or_groups(data: dict, track: str, report: dict) -> None:
    """闸 11：OR 分支已拆成并行子条件，且 `或组` / `或组语义` 自洽。

    规则变更（2026-08-06）：原先 OR 分支整体保留在 `或条件` 字段里、不拆分。现改为把**异质**
    OR 分支拆成并行原子子条件，用 `或组` 标记同组、`或组语义` 声明汇总方向。

    为什么组标记必须机械校验：两轨汇总方向相反。EX 是"组内任一触发即整条触发"（与约束 17
    一致）；IN 是"组内任一满足即整条满足"。IN 若漏标或标错，约束 18「全部入选'符合'」就会把
    "满足其一即可"读成"必须全部满足" —— 例如 IN-5（PSA 进展 **或** 软组织进展 **或** 骨病灶
    进展）患者只满足 PSA 一支，另两支为「无法判断」，整体会被错判为不符合入选，等于错误淘汰。

    可机械判定的进 `problems`（阻断）；"这几支是不是同一件事"要语义判断，只进 `notes`（建议）。
    """
    expected_sem = OR_GROUP_SEMANTICS.get(track)
    groups: dict[str, list[dict]] = {}
    for o in _iter_items(data):
        gid = o.get("或组")
        if isinstance(gid, str) and gid.strip():
            groups.setdefault(gid.strip(), []).append(o)

    for gid, members in sorted(groups.items()):
        cids = [str(m.get("条件ID")) for m in members]
        if len(members) < 2:
            report["problems"].append(
                f"闸11 `或组` `{gid}` 只有 1 个成员（{cids}）：OR 组至少 2 支——"
                "要么漏拆了其余分支，要么这条本就不是 OR 组、应删除 `或组` 字段"
            )
        # 组内语义必须一致且合法，且与轨匹配
        sems = {str(m.get("或组语义") or "") for m in members}
        if len(sems) > 1:
            report["problems"].append(f"闸11 `或组` `{gid}` 组内 `或组语义` 不一致：{sorted(sems)}（{cids}）")
        for sem in sorted(sems):
            if not sem:
                report["problems"].append(
                    f"闸11 `或组` `{gid}` 缺 `或组语义`（{cids}）：{track} 轨应为「{expected_sem}」"
                )
            elif sem not in OR_GROUP_SEMANTICS.values():
                report["problems"].append(
                    f"闸11 `或组` `{gid}` 的 `或组语义` 取值非法：{sem!r}（{cids}），只允许 {sorted(OR_GROUP_SEMANTICS.values())}"
                )
            elif expected_sem and sem != expected_sem:
                report["problems"].append(
                    f"闸11 `或组` `{gid}` 的 `或组语义` 与轨不符：{sem!r}（{cids}），{track} 轨必须是「{expected_sem}」"
                    "——写反会让整体入排结论反转"
                )
        # 同组必须同出一条原文标准
        clauses = {parsed[1] for cid in cids if (parsed := parse_cid(cid)) is not None}
        if len(clauses) > 1:
            report["problems"].append(
                f"闸11 `或组` `{gid}` 跨了不同原条号 {sorted(clauses)}（{cids}）：同一 OR 组必须来自同一条原文标准"
            )

    # ── `逻辑关系` 枚举 + 「标了 OR分支 却没有 或组」反向检查 ──────────
    # 旧闸只遍历「`或组` 非空」的条目，`逻辑关系` 从不被读取：一条该是 OR 组、却标 AND 且
    # 没有 `或组` 的条目对它完全不可见，只能等第二层语义 QC 报出来（会话 `1fee1395` 的
    # EX 轨 R2 阻断项全是这一类：EX-9-1..6 与 EX-4-1/4-2 的 AND→OR 误标，白吃轮次配额）。
    bad_logic: list[tuple[str, str]] = []
    or_branch_without_group: list[str] = []
    for o in _iter_items(data):
        raw = o.get("逻辑关系")
        if not isinstance(raw, str) or not raw.strip():
            continue  # 缺该字段不由本闸管（旧产物大量缺失），只管写了但写错的
        val = raw.strip()
        if val not in LOGIC_RELATIONS:
            bad_logic.append((str(o.get("条件ID")), val))
        elif val == "OR分支" and not str(o.get("或组") or "").strip():
            or_branch_without_group.append(str(o.get("条件ID")))
    if bad_logic:
        preview = "；".join(f"{cid}={val!r}" for cid, val in bad_logic[:6])
        report["problems"].append(
            f"闸11 `逻辑关系` 取值非法（{len(bad_logic)} 条）：{preview}"
            f"{'…' if len(bad_logic) > 6 else ''}。只允许 {list(LOGIC_RELATIONS)}；"
            f"跨组/跨条的关系说明写进 `{LOGIC_REMARK_FIELD}`（自由文本，不参与校验）——"
            "自由文本会让同一个 OR 组出现相反写法，下游按 AND 汇总即错误淘汰患者"
        )
    if or_branch_without_group:
        report["problems"].append(
            f"闸11 标了 `OR分支` 却没有 `或组`：{sorted(or_branch_without_group)}——"
            "OR 分支必须同时标 `或组` 与 `或组语义`，否则判定侧按 AND 汇总"
            "（入选轨会把「满足其一即可」读成「必须全部满足」）"
        )

    # 建议级：同一原条号下**全部**子条件都标 AND、都无 `或组`，而共享原文含**列举式** OR
    # 连接词 → 疑似 AND/OR 误标。只提示不阻断：判据必有假阳（真 AND 拆分的原文也可能含
    # `或`），而误阻断的代价就是白吃一轮 QC 配额。
    by_clause: dict[tuple[str, int], list[dict]] = {}
    for o in _iter_items(data):
        p = parse_cid(str(o.get("条件ID") or ""))
        if p and p[2] is not None:
            by_clause.setdefault((p[0], p[1]), []).append(o)
    for (prefix, clause), members in sorted(by_clause.items()):
        if len(members) < 2:
            continue
        if any(str(m.get("或组") or "").strip() for m in members):
            continue  # 该原条号已表达过拆分方向（IN-3 那种 AND + OR 混合形态）
        labels = [str(m.get("逻辑关系") or "").strip() for m in members]
        if not all(lbl.startswith("AND") for lbl in labels):
            continue
        quote = " ".join(str(m.get("原文") or "") for m in members)
        markers = [m for m in _OR_ENUMERATING_MARKERS if m in quote]
        if markers:
            cids = sorted(str(m.get("条件ID")) for m in members)
            report["notes"].append(
                f"闸11（建议级）`{prefix}-{clause}` 的 {len(members)} 个子条件全标 AND 且无 `或组`，"
                f"但原文含列举式 OR 连接词 {markers}（{cids}）："
                "若这些分支是**可替代**的（任一成立即整条成立），应改标 `OR分支` + `或组`/`或组语义`；"
                "若确为并列必须同时满足，保持 AND 即正确。⛔ 别把裸 `或` 当判据——"
                "「未接受输血或G-CSF」那种是 AND 拆分"
            )

    # 建议级：多支 `或条件` 却未拆成组，可能是漏拆
    for o in _iter_items(data):
        if o.get("或组"):
            continue
        transform = o.get("转化条件")
        branches = (transform or {}).get("或条件") if isinstance(transform, dict) else None
        if isinstance(branches, list) and len(branches) >= _OR_SPLIT_ADVISORY_MIN_BRANCHES:
            report["notes"].append(
                f"闸11（建议级）{o.get('条件ID')} 的 `或条件` 有 {len(branches)} 支却未拆成 `或组`："
                "若这些分支是**异质替代事实**（需查不同证据位置/匹配字段），应拆成并行子条件并标 `或组`；"
                "若只是同一事实的等价表述（单位换算、参考范围变体、同类列举），保持现状即正确。"
            )


def _check_threshold_executability(data: dict, report: dict) -> None:
    """闸 12（建议级）：运算符不在标准集合内的条目，按三档判据点名。

    动机（会话 `1fee1395`）：IN 轨 R2 的 12 项阻断里 8 项是「阈值不可执行」，其中 5 项
    （IN-5-1/5-2/5-3、IN-7-1/7-2）到 R3 才被中性化成 `upstream_issues` —— 它们依赖 PCWG3 /
    RECIST V1.1 的定义、或本质是相对比较（"与上次骨扫描相比"），在忠实原文的前提下结构化层
    无解。两轮配额纯空转。根因是规范自相矛盾：SKILL 的字段定义允许 `阈值` 写"文字描述"，
    QC 清单却把复合自然语言阈值列为阻断级。

    本闸只做可机械判定的那一步：把非标准运算符点出来 + 附三档判据，让两轨 QC **首轮**
    就按同一口径归档。**不阻断**——第二档（可结构化未结构化）还是第三档（依赖外部标准）
    需要语义判断；误阻断的代价正是本闸要消灭的那种空转。

    实测判别力：该会话 IN 轨非标准运算符恰好是 `进展`×3 + `存在`×2，与那 5 项 upstream
    **完全重合**。
    """
    offenders: list[tuple[str, str, list[str]]] = []
    for o in _iter_items(data):
        if o.get("可从病例获取") is False:
            continue
        transform = o.get("转化条件")
        if not isinstance(transform, dict):
            continue
        op = transform.get("运算符")
        if not isinstance(op, str) or not op.strip():
            continue  # 运算符缺失属「转化条件不完整」，由语义 QC 管，本闸不重复报
        op = op.strip()
        if op in CANONICAL_OPERATORS:
            continue
        blob = " ".join(
            str(transform.get(k) or "") for k in ("阈值", "条件", "或条件", "计算")
        ) + str(o.get("子条件") or "")
        hits = [s for s in _REFERENCE_STANDARDS if s in blob]
        # PCWG3 命中时 PCWG/PCWG2 不再重复列
        hits = [h for h in hits if not any(other != h and other.startswith(h) for other in hits)]
        offenders.append((str(o.get("条件ID")), op, hits))

    if not offenders:
        return

    detail = "；".join(
        f"{cid}（运算符 {op!r}{'，命中 ' + '/'.join(hits) if hits else ''}）" for cid, op, hits in offenders
    )
    with_std = sorted({cid for cid, _, hits in offenders if hits})
    lines = [
        f"闸12（建议级）{len(offenders)} 条的 `运算符` 不在标准集合 {sorted(CANONICAL_OPERATORS)} 内：{detail}。",
        "按 `criteria-qc-checklist.md` 的三档判据**首轮**定档，不要拖到第三轮："
        "① 可执行数值/集合/区间 → 通过；"
        "② 可结构化却没结构化 → 阻断级，本轮修；"
        "③ 判定要件写在**外部评价标准**里、或本质是相对比较（「与上次相比」）→ "
        "直接归 `upstream_issues`（不计入 `blocking_issues`、不占阻断额度、不阻断 `passed`）。",
    ]
    if with_std:
        lines.append(f"其中 {with_std} 已命中引用型标准名，**第三档**的可能性最高。")
    lines.append(
        "⛔ 若这些条目属 `upstream_issues` 的**中性化**产物：`运算符` 必须用 `不限`（在标准集合内），"
        "状态词写进 `备注`（`待核实` / `原文缺词`，闸10 识别），阈值改为不可执行表达。"
        "把 `待核实` 写进 `运算符` 会让本闸每改一条就多报一条，形成「改一条 +1 报警」的正反馈——"
        "thread `3a745b38` 因此逐条中性化 + 逐条校验，误读为「还有问题要改」，烧掉约 100 个 superstep。"
        "详见 `criteria-repair.md` 的 upstream 中性化专节。"
    )
    report["notes"].append("".join(lines))


#: `--qc` 不带值时的哨兵：main 里替换为 {workspace}/criteria_qc_{TRACK}.json。
_QC_DEFAULT = "__default__"

#: 闸 1/13 这类**文件级**判据的前缀。它们讲的是整份产物的形态（JSON 不合法、类目键写错、
#: 容器形态不被识别），不属于任何单条 —— `--only` 必须放行，否则「只查这一条」会把
#: 文件已经坏掉这件事一起过滤掉，比不查更危险。
_FILE_LEVEL_PROBLEM_PREFIXES = ("闸1 ", "闸13 ")


def filter_problems_to_ids(problems: list[str], cids: list[str]) -> list[str]:
    """只保留点名了 *cids* 之一的问题，外加所有文件级问题。

    存在的理由（会话 a7c19ea1）：EX 轨修订子代理在一个任务里执行了 28 次全量闸 —— 改一条、
    跑全闸、在整份报告里找自己那一条。全量闸的输出本身就是大段上下文，于是「改 10 条」变成
    「28 次全量报告的往返」，单任务 901 秒 / 258 万 token。改一条查一条时输出应当只有那一条。
    """
    wanted = {c.strip() for c in cids if c.strip()}
    if not wanted:
        return problems
    kept = []
    for problem in problems:
        if problem.startswith(_FILE_LEVEL_PROBLEM_PREFIXES) or any(cid in problem for cid in wanted):
            kept.append(problem)
    return kept


def check_track(workspace: Path, track: str, qc_path: Path | None, snapshot: bool) -> dict:
    """跑 11 个闸，返回结构化报告（`problems` 非空即未过；`notes` 为建议级）。"""
    report: dict = {"track": track, "problems": [], "notes": []}
    path = workspace / f"criteria_parsed_{track}.json"
    if not path.exists():
        report["skipped"] = f"{path.name} 不存在"
        return report

    # ── 闸 1：JSON 合法 + 顶层结构 ──────────────────────────────────
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report["problems"].append(f"闸1 JSON 不合法：{exc}")
        return report
    if not isinstance(data.get("四分类"), dict):
        report["problems"].append("闸1 缺少 `四分类` 顶层字段")
        return report
    for key in FORBIDDEN_TOP_KEYS:
        if key in data:
            report["problems"].append(f"闸1 单轨文件不得含全篇级字段 `{key}`（由 judge_pack.py assemble 负责）")
    extra_cats = [c for c in (data.get("四分类") or {}) if c not in TRACK_CATEGORIES[track]]
    if extra_cats:
        report["problems"].append(f"闸1 出现非本轨类目：{extra_cats}（{track} 轨只允许 {list(TRACK_CATEGORIES[track])}）")

    # ── 闸 13：类目容器形态 + dict key 与 `条件ID` 一致 ──────────────
    # 必须在其余闸之前跑：形态不对时 `category_entries` 只能返回空列表，后面每道闸都会看到
    # "零条目"并给出误诊（实测：`描述索引` 非空时闸5 报「丢条，必须补回实体」）。
    _check_container_shapes(data, report)

    ids, off_track = collect(data, track)
    report["total"] = len(ids)
    if not ids:
        # 零条目不能算过闸。QC 子代理的开工前置自检只看 `exit_code`，一份被读成空的文件会让它
        # 在"没有任何标准"的视图上正常开工（实测 `描述索引` 恰好为空时输出 `✅ 结构闸全过`）。
        report["problems"].append("闸1 本轨零条目：`四分类` 的本轨类目里读不到任何带 `条件ID` 的条目 → 解析产物缺失、类目键写错、或容器形态不被识别（见闸13）")
    if off_track:
        report["problems"].append(f"闸1 条目落在非本轨类目：{sorted(set(off_track))}")

    # ── 闸 2：条件ID 唯一 ────────────────────────────────────────────
    seen: set[str] = set()
    dup = sorted({c for c in ids if c in seen or seen.add(c)})  # type: ignore[func-returns-value]
    if dup:
        report["problems"].append(f"闸2 条件ID 重复：{dup}")

    # ── 闸 3：前缀与轨一致 + ID 格式合规 ──────────────────────────────
    malformed = sorted({c for c in ids if parse_cid(c) is None})
    if malformed:
        report["problems"].append(f"闸3 条件ID 不符合 `{track}-原条号[-子序号]` 格式：{malformed}")
    bad_prefix = sorted({c for c in ids if (p := parse_cid(c)) and p[0] != track})
    if bad_prefix:
        report["problems"].append(f"闸3 条件ID 前缀与轨不一致：{bad_prefix}")

    # ── 闸 4：子序号规范（不混用 + 连续）─────────────────────────────
    by_parent: dict[int, list[int | None]] = {}
    for cid in ids:
        p = parse_cid(cid)
        if p and p[0] == track:
            by_parent.setdefault(p[1], []).append(p[2])
    for parent in sorted(by_parent):
        subs = by_parent[parent]
        bare, numbered = [s for s in subs if s is None], sorted(s for s in subs if s is not None)
        if bare and numbered:
            report["problems"].append(f"闸4 `{track}-{parent}` 混用：既有不带子序号的条目又有 {[f'{track}-{parent}-{s}' for s in numbered]}")
        if len(bare) > 1:
            report["problems"].append(f"闸4 `{track}-{parent}` 有 {len(bare)} 个不带子序号的条目（应唯一）")
        if numbered and numbered != list(range(1, len(numbered) + 1)):
            report["problems"].append(f"闸4 `{track}-{parent}` 子序号不连续：{numbered}（应为 1..{len(numbered)}）")
    report["原条号"] = sorted(by_parent)

    # ── 闸 5：描述索引双向对齐（按**主条件ID**）──────────────────────
    # `描述索引` 描述的是**原始标准条款**（`EX-4`），不是拆分后的子条件（`EX-4-1/-2`）：
    # 两份 HTML 报告都按主条件ID 查它（criteria_report 的 `DESC[pid]`、
    # screening_report 的 `desc_index.get(pid)`），schema_example 也是主条件键。
    # ⚠️ 本闸曾要求索引键与 `四分类` 子条件ID 一一对应，把正确形态判成「两套 ID 体系」，
    # 修复指引还写着「以四分类实体为准修描述索引」——会话 9a83ccc9 照此产出 64 个子条件键、
    # 0 个主条件键，报告主条件行只能回退成「首个子条件文本」，`IN-2` 显示成「年龄 ≥ 18 周岁」，
    # 静默丢掉了 ≤70 岁的上限。现改为按主条件ID 对齐，仍能抓住原本要抓的「两套 ID 体系」。
    parents = {f"{p[0]}-{p[1]}" for cid in ids if (p := parse_cid(cid))}
    idx_keys = set((data.get("描述索引") or {}).keys())
    miss = sorted(parents - idx_keys)
    extra = sorted(idx_keys - parents)
    report["miss_in_index"] = miss
    report["extra_in_index"] = extra
    if miss:
        report["problems"].append(
            f"闸5 四分类有实体但 `描述索引` 缺键：{miss} → 按**主条件ID**补索引（禁止反向删实体）"
        )
    if extra:
        sub_keyed = sorted(k for k in extra if (p := parse_cid(k)) and p[2] is not None)
        hint = (
            f"其中 {sub_keyed} 是**子条件ID**，应改写成对应主条件ID（索引描述的是原始标准条款，不是拆分后的子条件）；"
            if sub_keyed
            else ""
        )
        report["problems"].append(
            f"闸5 `描述索引` 有键但四分类无实体：{extra} → {hint}"
            "若原文内容已并入另一条则删索引键；若该原文条目找不到任何实体则是**丢条**，必须补回实体"
        )

    # ── 闸 6：原条号全覆盖（无基线丢条探测）──────────────────────────
    meta_path = workspace / "criteria_meta.json"
    last_no = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            last_no = (meta.get("末条号") or {}).get(TRACK_META_KEY[track])
        except json.JSONDecodeError:
            report["notes"].append("criteria_meta.json 不合法，闸6 跳过")
    if isinstance(last_no, int) and last_no > 0:
        missing_parents = [n for n in range(1, last_no + 1) if n not in by_parent]
        report["末条号"] = last_no
        if missing_parents:
            report["problems"].append(f"闸6 原条号无任何实体条目：{[f'{track}-{n}' for n in missing_parents]}（末条号={last_no}）→ 原文标准条目缺失或在修订中丢失，必须补回")
    else:
        report["notes"].append("criteria_meta.json 缺 `末条号`，闸6 跳过（建议补齐以启用无基线丢条探测）")

    # ── 闸 9：原文忠实性（每条 `原文` 必须在 raw.md 中逐字可查）──────
    raw_path = workspace / "eligibility_criteria_raw.md"
    if not raw_path.exists():
        report["notes"].append("eligibility_criteria_raw.md 不存在，闸9 跳过")
    else:
        body = _norm_text(raw_path.read_text(encoding="utf-8"))
        if not body:
            report["problems"].append("闸9 `eligibility_criteria_raw.md` 为空 —— 解析输入不存在，产出必然是凭空生成")
        else:
            unfaithful: list[str] = []
            diagnostics: dict[str, dict] = {}
            or_joined: list[str] = []
            checked = 0
            for cat, items in (data.get("四分类") or {}).items():
                for it in category_entries(items):
                    if not isinstance(it, dict):
                        continue
                    quote = _norm_text(str(it.get("原文") or ""))
                    if not quote:
                        continue
                    checked += 1
                    cid = str(it.get("条件ID"))
                    if quote in body:
                        continue
                    if _segments_match_in_order(quote, body):
                        or_joined.append(cid)  # OR 分支跨行拼接，放行但留痕
                        continue
                    unfaithful.append(cid)
                    if len(diagnostics) < 5:  # 只给前几条，报告不能自己变成上下文炸弹
                        diagnostics[cid] = _mismatch_diagnosis(quote, body)
            report["原文核对"] = {"已核对": checked, "查不到": len(unfaithful)}
            if or_joined:
                report["原文核对"]["OR分段通过"] = or_joined
                report["notes"].append(
                    f"闸9 {or_joined} 的 `原文` 非连续子串，但按 OR 分支逐段命中且顺序一致 → 判为跨行拼接，放行"
                )
            if unfaithful:
                ratio = len(unfaithful) * 100 // max(checked, 1)
                head = unfaithful[:12]
                more = f"…等 {len(unfaithful)} 条" if len(unfaithful) > len(head) else ""
                # ⚠️ 诊断放**独立键**：`原文核对` 是稳定的计数契约（既有测试按字典全等断言），
                # 往里塞可变结构会让消费方每次都要重新适配。
                report["原文失配定位"] = diagnostics
                report["problems"].append(
                    f"闸9 `原文` 在 `eligibility_criteria_raw.md` 中查不到：{head}{more}"
                    f"（{len(unfaithful)}/{checked}，{ratio}%）"
                    " → 解析未基于原文，属**凭空生成/改写**。"
                    "逐条失配位置见报告 `原文核对.失配定位`（含首个失配偏移与 raw 最相近片段），"
                    "⛔ 不必再读脚本源码逆推归一化规则。"
                    "先确认子代理读到的原文非空（`段行号` 是**试验方案.md**的坐标，"
                    "⛔ 不可用于 read_file `eligibility_criteria_raw.md`，须用 `raw段行号`），"
                    "再整轨重做本轨解析；⛔ 禁止逐条改写 `原文` 去迁就已生成的结论"
                )

    # ── 闸 7：QC blocking_issues 的目标实体仍存在 ────────────────────
    if qc_path is not None:
        if not qc_path.exists():
            report["problems"].append(f"闸7 QC 报告不存在：{qc_path}")
        else:
            try:
                qc = json.loads(qc_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                report["problems"].append(f"闸7 QC 报告 JSON 不合法：{exc}")
                qc = {}
            targets: dict[int, list[str]] = {}
            for issue in qc.get("blocking_issues") or []:
                cid = (issue or {}).get("condition_id")
                p = parse_cid(cid) if cid else None
                if p and p[0] == track:
                    targets.setdefault(p[1], []).append(str(cid))
            report["qc_target_原条号"] = sorted(targets)
            gone = [n for n in sorted(targets) if n not in by_parent]
            if gone:
                report["problems"].append(f"闸7 本轮 QC 阻断项涉及的原条号修订后无任何实体：{[f'{track}-{n}（QC 项 {targets[n]}）' for n in gone]} → 修订把条目改丢了")

            # ── 闸 8：QC 原地打转探测（阻断项集合与上一轮完全相同）────
            blocking_ids = sorted({str(i["condition_id"]) for i in (qc.get("blocking_issues") or []) if isinstance(i, dict) and i.get("condition_id") and i.get("status") not in ("fixed", "upstream")})
            rnd = qc.get("round")
            hist_path = workspace / f"criteria_qc_history_{track}.json"
            history: list[dict] = []
            if hist_path.exists():
                try:
                    loaded = json.loads(hist_path.read_text(encoding="utf-8"))
                    history = loaded if isinstance(loaded, list) else []
                except json.JSONDecodeError:
                    report["notes"].append(f"{hist_path.name} 不合法，打转探测重新计数")
            prev = [h for h in history if h.get("round") != rnd]
            last = prev[-1] if prev else None
            report["qc_blocking_ids"] = blocking_ids
            # 被点名条目的内容哈希：用来区分「修订压根没落到目标条目上」与「上游原文真无解」。
            # 闸8 原本只有后一种处方（走 upstream_issues），对前一种是误诊——thread `3a745b38`
            # 就被它推向逐条中性化，闸12 报警随之单调上升，被读成"还有问题要改"，烧掉约 100 个
            # superstep。两者的机械判据只差一个哈希：目标条目根本没变，就不可能是"改了没效果"。
            entity_digests: dict[str, str] = {}
            for _cat, _items in (data.get("四分类") or {}).items():
                for _it in category_entries(_items):
                    _cid = str(_it.get("条件ID") or "")
                    if _cid:
                        entity_digests[_cid] = hashlib.sha256(json.dumps(_it, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
            if last and blocking_ids and last.get("blocking_ids") == blocking_ids:
                prev_digests = last.get("entity_digests") or {}
                untouched = [c for c in blocking_ids if c in prev_digests and prev_digests[c] == entity_digests.get(c)]
                if untouched and len(untouched) == len([c for c in blocking_ids if c in prev_digests]):
                    report["problems"].append(
                        f"闸8 该轨 QC 原地打转，且被点名条目**内容一字未变**：{untouched}"
                        f"（第 {rnd} 轮阻断项与第 {last.get('round')} 轮完全一致）"
                        " → 这不是「改了没效果」，是**修订没落到目标条目上**。"
                        "⛔ 先核对 pointer：`四分类` 的类目是以 `条件ID` 为键的 dict，"
                        "pointer 必须写 `/四分类/{类目}/{条件ID}/...`；用 `--show {条件ID}` 确认目标存在。"
                        "⛔ 此时**不要**走 upstream_issues 路径——那会把一个定位缺陷当成原文缺陷处理。"
                    )
                else:
                    report["problems"].append(
                        f"闸8 该轨 QC 原地打转：第 {rnd} 轮阻断项 {blocking_ids} 与第 {last.get('round')} 轮完全一致"
                        "（被点名条目**内容确实改过**，说明改法无效而非没改）"
                        " → 问题多半在上游原文（`eligibility_criteria_raw.md` 句内缺词等）。"
                        "⛔ 禁止再消耗轮次重复同样的修订：改走 QC 的 `upstream_issues` 路径"
                        "（回原始方案文档核实该句 / 无法核实则 ask_clarification 请用户裁定）"
                    )
            hist_path.write_text(
                json.dumps([*prev, {"round": rnd, "blocking_ids": blocking_ids, "entity_digests": {c: entity_digests[c] for c in blocking_ids if c in entity_digests}}], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # ── 闸 10：`upstream_issues` 点名的条目必须已「中性化」──────
            upstream_cids = [str(i["condition_id"]) for i in (qc.get("upstream_issues") or []) if isinstance(i, dict) and i.get("condition_id")]
            if upstream_cids:
                entity_of: dict[str, dict] = {}
                for _cat, _items in (data.get("四分类") or {}).items():
                    for _it in category_entries(_items):
                        if isinstance(_it, dict) and _it.get("条件ID") is not None:
                            entity_of[str(_it["条件ID"])] = _it
                not_isolated = []
                for cid in upstream_cids:
                    ent = entity_of.get(cid)
                    if ent is None:
                        continue  # 条目已不存在（被合并/删除）→ 已处置
                    if ent.get("可从病例获取") is False:
                        continue  # 已降级为不可从病例判定 → 已中性化
                    if any(m in json.dumps(ent, ensure_ascii=False) for m in UPSTREAM_PENDING_MARKS):
                        continue  # 已标待核实 → 已中性化
                    not_isolated.append(cid)
                report["upstream_未中性化"] = not_isolated
                if not_isolated:
                    report["problems"].append(
                        f"闸10 `upstream_issues` 点名的条目仍以确定性可执行条件存在（未中性化）：{not_isolated}"
                        " → 上游原文缺陷未核实前，把它留作「可从病例获取 + 确定阈值」等于超出原文补写；"
                        "下一轮 QC 必然把它升级为 blocking，两档之间振荡、白烧轮次配额。"
                        " **本轮就要中性化**（三选一）：① `可从病例获取` 改 false 并在备注说明原因；"
                        f"② 在备注写明原文缺什么（含 {UPSTREAM_PENDING_MARKS[0]} 等标记词）；"
                        "③ 条目确实多余则删除并同步 `描述索引`。"
                        " ⛔ 中性化 ≠ 放弃：仍要按 `criteria-qc-checklist.md` 第三档回 `uploads/` 原文核实该句"
                        "（允许破例，仅该句、必要时 `view_image` 看原页），核实成功后再恢复可执行表达。"
                    )

    # ── 闸 11：OR 分支拆分与 `或组` 语义 ─────────────────────────────
    _check_or_groups(data, track, report)
    _check_threshold_executability(data, report)
    _check_cross_entry_pollution(data, report)
    _check_qualitative_quantitative_mix(data, report)
    _check_obtainability_split(data, report)

    # ── 基线快照 / 差异（可选，供条数守恒记账）────────────────────────
    base_path = workspace / f"criteria_structure_baseline_{track}.json"
    if snapshot:
        base_path.write_text(
            json.dumps({"track": track, "total": len(ids), "ids": sorted(set(ids))}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report["notes"].append(f"基线已写入 {base_path.name}（total={len(ids)}）")
    elif base_path.exists():
        try:
            base = json.loads(base_path.read_text(encoding="utf-8"))
            before = set(base.get("ids") or [])
            report["基线差异"] = {
                "before_total": base.get("total"),
                "after_total": len(ids),
                "消失": sorted(before - set(ids)),
                "新增": sorted(set(ids) - before),
            }
        except json.JSONDecodeError:
            report["notes"].append(f"{base_path.name} 不合法，基线差异跳过")
    return report


def write_gate_artifact(workspace: Path, report: dict) -> Path | None:
    """落盘本轨闸产物，供 QC 子代理在开工前自检前置（D 层硬化）。

    含 `exit_code` 与被检文件的内容哈希：QC 子代理读到 `exit_code != 0`，或哈希与它当下
    读到的 `criteria_parsed_{TRACK}.json` 不一致（说明闸跑完后文件又被改过），
    必须立即返回「前置闸未过，拒绝执行」——不依赖主代理守规矩。
    """
    track = report["track"]
    if report.get("skipped"):
        return None
    src = workspace / f"criteria_parsed_{track}.json"
    digest = None
    if src.exists():
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    out = workspace / f"criteria_structure_gate_{track}.json"
    out.write_text(
        json.dumps(
            {
                "track": track,
                "exit_code": 2 if report["problems"] else 0,
                "checked_file": src.name,
                "content_sha256_16": digest,
                "total": report.get("total"),
                "problems": report["problems"],
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out


def summarize(reports: list[dict]) -> str:
    lines: list[str] = []
    for r in reports:
        track = r["track"]
        if r.get("skipped"):
            lines.append(f"[{track}] 跳过：{r['skipped']}")
            continue
        lines.append(f"[{track}] total={r.get('total')} 原条号={len(r.get('原条号') or [])} 个 miss_in_index={r.get('miss_in_index')} extra_in_index={r.get('extra_in_index')}")
        diff = r.get("基线差异")
        if diff:
            lines.append(f"[{track}] 基线差异 {diff['before_total']}→{diff['after_total']} 消失={diff['消失']} 新增={diff['新增']}")
            if diff["消失"]:
                lines.append(f"[{track}] ℹ️ 消失的条件ID 必须都能由本轮 QC 要求的合并解释；无法解释的即为丢条（闸6/闸7 只能抓整条原条号消失，子条件消失需人工对账）")
        for note in r.get("notes") or []:
            lines.append(f"[{track}] · {note}")
        for prob in r["problems"]:
            lines.append(f"[{track}] ⛔ {prob}")
        if not r["problems"]:
            lines.append(f"[{track}] ✅ 结构闸全过")
    failed = [r["track"] for r in reports if r.get("problems")]
    if failed:
        lines.append(f"⛔ 未过闸的轨：{failed} —— 禁止发 task(quality-control)、禁止进入收尾，先修结构。")
    return "\n".join(lines)


def show_entities(workspace: Path, track: str, cids: list[str]) -> str:
    """按条件ID 精确打印实体条目 JSON，供修订时构造 `str_replace` 的 old_str。

    为什么需要它（token 账，历史会话 `69612125`）：修订时为看清一条要改的条目，
    主代理用 `read_file(start_line, end_line)` 读 300-400 行区间 ≈ 4,300 字符；
    行号猜不准还要反复读——该会话 `criteria_parsed_EX.json` 被读 **33 次**、
    `_IN.json` **20 次**，同一区间最多重复 3 遍，`read_file` 总量的 **63% 是重复读**。
    本函数按 ID 直取，单条 ≈ 300-800 字符，且不会读错位置。

    输出保持与文件中**逐字一致**的缩进与内容，可直接截取作为 `old_str`。
    """
    parsed = workspace / f"criteria_parsed_{track}.json"
    if not parsed.exists():
        return f"⛔ 不存在：{parsed}"
    raw = parsed.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"⛔ JSON 不合法（先修语法再取条目）：{exc}"

    wanted = {c.strip() for c in cids if c.strip()}
    out: list[str] = []
    found: set[str] = set()
    for cat, items in (data.get("四分类") or {}).items():
        for it in category_entries(items):
            if not isinstance(it, dict) or str(it.get("条件ID")) not in wanted:
                continue
            cid = str(it.get("条件ID"))
            found.add(cid)
            block = json.dumps(it, ensure_ascii=False, indent=2)
            # 定位该条目在文件中的行号，便于必要时核对（不必据此 read_file）
            anchor = f'"条件ID": "{cid}"'
            line_no = next(
                (i + 1 for i, ln in enumerate(raw.splitlines()) if anchor in ln),
                None,
            )
            out.append(f"── {cid}｜类目 {cat}｜文件第 {line_no} 行附近 ──\n{block}")
            desc = (data.get("描述索引") or {}).get(cid)
            if desc is not None:
                out.append(f'   描述索引["{cid}"] = {json.dumps(desc, ensure_ascii=False)}')
    missing = sorted(wanted - found)
    if missing:
        out.append(f"⛔ 未找到这些条件ID（可能已被合并/删除，属丢条）：{missing}")
    if found:
        # 一行足矣：`--show` 的全部价值是比行区间读省 token（本文件的 size 不变量由
        # tests/skills 锁定），完整的 pointer 纪律写在 criteria-repair.md。
        out.append("pointer: /四分类/{类目}/{条件ID}/… 按 key 定位，勿用下标")
    return "\n".join(out) if out else "（无匹配条目）"


def contract_text() -> str:
    """本闸强制的字段与取值集合，从常量现取现印。

    存在的理由（会话 a7c19ea1）：修订子代理用 `sed`/`grep` **读了 9 次本脚本的源码**去问
    "闸 10 认哪些标记词"、"`逻辑关系` 允许什么"、"`运算符` 的标准集合是什么" ——
    需要的全是这里几行常量，却要读实现来猜，而 grep 到的行还是散落的定义片段。
    从常量生成而不是手写文案：手写的那份一定会和代码漂移，那时它比没有更糟。
    """
    return "\n".join(
        [
            "check_track_structure 强制的字段与取值（唯一权威：本脚本常量）",
            f"  转化条件.运算符      标准集合 {sorted(CANONICAL_OPERATORS)}",
            f"  逻辑关系             取值 {list(LOGIC_RELATIONS)}（跨组/跨条说明写进自由文本字段 `{LOGIC_REMARK_FIELD}`，不校验）",
            "  或组语义             按轨取值 " + "；".join(f"{t}={v}" for t, v in OR_GROUP_SEMANTICS.items()),
            f"  备注（闸10 标记词）  {list(UPSTREAM_PENDING_MARKS)}",
            "  upstream 中性化      运算符=`不限` + 状态词写进 `备注`；⛔ 状态词写进 `运算符` 会形成「改一条 +1 报警」",
            "  pointer 纪律         /四分类/{类目}/{条件ID}/… 按 key 定位，勿用下标",
            "",
            "判据的正式出处（⛔ 不要读本脚本源码）：",
            "  references/criteria-repair.md          修订动作与 pointer 纪律",
            "  references/criteria-qc-checklist.md    三档分级与 QC 判据",
        ]
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="单轨标准结构闸（QC 前置 + 修订后守恒）")
    # 非必填只为让 `--contract` 能单独跑（它不读 workspace）；其余路径下面显式要求。
    ap.add_argument("--workspace", help="workspace 目录")
    ap.add_argument("--track", choices=["IN", "EX"], help="只检查该轨；省略则检查存在的两轨")
    # 2026-08-13（会话 a7c19ea1）：原为 `--qc <路径>`，漏值时 argparse 直接 exit 2 打印用法 ——
    # lead 与子代理各撞一次，每次白烧一轮完整的模型往返。约定路径是唯一合理取值，故设为
    # 可选值：`--qc` 不带值即取 {workspace}/criteria_qc_{TRACK}.json。
    ap.add_argument(
        "--qc",
        nargs="?",
        const=_QC_DEFAULT,
        help="本轮 criteria_qc_{TRACK}.json，启用闸7（按 blocking_issues 点名核对）；不带值即取 {workspace}/criteria_qc_{TRACK}.json",
    )
    ap.add_argument(
        "--only",
        help="只校验这些条件ID（逗号分隔），只报这些条目的结论；改一条查一条用它，别跑全量闸",
    )
    ap.add_argument(
        "--contract",
        action="store_true",
        help="打印本闸强制的字段与取值集合（⛔ 用它替代 grep 本脚本源码）",
    )
    ap.add_argument("--snapshot", action="store_true", help="写基线快照（修订前跑），供修订后看条件ID 消失/新增")
    ap.add_argument("--json", help="可选：把完整报告写到该路径")
    ap.add_argument(
        "--show",
        help="按条件ID 精确打印条目（逗号分隔），供修订取 old_str；⛔ 修订时用它替代 read_file 读行区间",
    )
    args = ap.parse_args(argv)

    if args.contract:
        print(contract_text())
        return 0

    # `--workspace` 对除 `--contract` 以外的每条路径都是必需的（argparse 层放开了它，
    # 只为让 `--contract` 免于填一个它不会读的目录）。
    if not args.workspace:
        ap.error("--workspace 是必需的（除 --contract 外）")

    if args.show:
        if not args.track:
            print("⛔ --show 必须同时指定 --track", file=sys.stderr)
            return 2
        print(show_entities(Path(args.workspace), args.track, args.show.split(",")))
        return 0

    workspace = Path(args.workspace)
    tracks = [args.track] if args.track else ["IN", "EX"]
    if args.qc and not args.track:
        ap.error("--qc 必须配合 --track 使用（QC 报告是按轨的）")
    if args.only and not args.track:
        ap.error("--only 必须配合 --track 使用（条件ID 是按轨的）")

    def _qc_path(track: str) -> Path | None:
        if not args.qc:
            return None
        return workspace / f"criteria_qc_{track}.json" if args.qc == _QC_DEFAULT else Path(args.qc)

    reports = [check_track(workspace, t, _qc_path(t), args.snapshot) for t in tracks]

    if args.only:
        # 只查这几条：过滤输出，但**不写** gate 产物。产物是「本轨已过闸」的凭据，下游
        # （闸 7 的历史、收尾前置）都读它；用一次单条校验去覆盖它等于伪造全轨结论。
        only_ids = args.only.split(",")
        for r in reports:
            r["problems"] = filter_problems_to_ids(r.get("problems") or [], only_ids)
            r["notes"] = filter_problems_to_ids(r.get("notes") or [], only_ids)
            r["only"] = [c.strip() for c in only_ids if c.strip()]
        print(summarize(reports))
        asked = ", ".join(c.strip() for c in only_ids if c.strip())
        print(f"（--only 模式：只报 {asked} 的结论，未写 gate 产物；全轨结论仍需跑一次完整闸）")
        return 2 if any(r.get("problems") for r in reports) else 0

    for r in reports:
        write_gate_artifact(workspace, r)
    print(summarize(reports))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{out}")
    return 2 if any(r.get("problems") for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
