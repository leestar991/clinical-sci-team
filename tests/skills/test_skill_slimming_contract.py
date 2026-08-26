"""两份 SKILL.md 的**精简契约**：规则留在正文，故障叙述搬到 `references/failure-archive.md`。

为什么要管体积：`criteria-parser` 与 `eligibility-judgment` 的 SKILL.md 是**每个子代理的常驻
上下文**，每一轮 AI step 都要重付一次。基线里五个技能全文 42,758 token × 379 轮 ≈ 16.2M token
固定重复（`docs/plans/criteria-token-saving-v1.1.md` §1）。更要紧的是：文档越长，同一条规则被
写两遍、且两遍互相矛盾的概率越高 —— 会话 `1fee1395` 的 EX-1-3 就是四处说法三种口径。

故障叙述（"thread `xxxxxxxx` 里……结果……"）对**理解**规则有用，但不必占常驻上下文：
规则正文只留一行指针，叙述进 `references/failure-archive.md`，需要时再读。

⛔ **只搬叙述，不动规则**：本测试用「标题数 / ⛔ 规则标记 / 编号约束数」三项守恒来兜住
「搬着搬着把规则一起删了」。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "skills" / "custom"

# 精简前实测基线（见 .deer-flow/task7-baseline/）。规则守恒项**不得低于**这些值。
# 「规则没被删掉」的底线。⚠️ 口径是**技能全集**（SKILL.md + references/*.md），不是 SKILL.md 单文件
# —— 见下方「2026-08-10 推翻」。单文件口径会让任何重组都变成违约，而它本来要防的是**删除**。
# 数值口径必须与下面的 _headings / _stop_marks / _numbered 完全一致（`^#{1,6} ` / `⛔` 计数 /
# `^N. **`），否则底线会因为量法不同而虚高或虚低。2026-08-10 实测值。
BASELINE = {
    "criteria-parser": {"headings": 84, "stop_marks": 103, "numbered": 28},
    "eligibility-judgment": {"headings": 99, "stop_marks": 141, "numbered": 66},
}

# 精简后的体积**棘轮**（ratchet）：防回涨，不是减肥目标。
#
# ⚠️ **2026-08-10 推翻了本文原先的第 ① 条禁令**（原文：「不许把规则搬进 references，因为
# references 按需加载、子代理很可能根本不读，等于把硬规则变成可选项」）。推翻依据是实测，
# 不是偏好：
#   - `subagents.agents.{general-purpose,quality-control}.skills == []` —— 子代理**本来就不会
#     自动加载任何 SKILL.md**。所以"搬进 references"对子代理的可达性**没有影响**：两者它都不
#     自动读。规则到达子代理的唯一通道是**委派模板**（原样复制进 prompt）。
#   - thread `93d8a2c6` 的实测读取记录：判定子代理在**整篇 `read_file`** 80KB 的
#     `eligibility-judgment/SKILL.md`（2 次整篇 + 3 次分段），因为模板里写着"看 SKILL 原则十一 B"
#     却没给它可读路径。规则长在 SKILL.md 里，代价是子代理为拿规则读一整本编排手册。
#   - 因此规则收敛到 `references/judgment-principles.md`（一份），SKILL.md 收敛为**主代理编排
#     手册**并给出逐条指向表；委派模板改为给出该文件的**绝对路径**。
# 原先第 ② 条禁令**仍然有效**：判定约束清单不许删条、编号不许动（delegation 与 QC 按编号引用）。
# 它现在住在 `judgment-principles.md`，由 `test_judgment_constraint_list_is_intact` 守。
# 所以体积的真正大头（Task 2 的 `subagents.agents.*.skills: []`，一次省掉约 16.2M token）
# 不在本任务里；本任务解决的是**自相矛盾**与**叙述常驻**。
#
# ⛔ 抬这两个数的纪律：只有当新增内容是**硬规则**时才允许抬，且必须同时满足 ——
#   ① `test_rule_markers_are_preserved` / `test_numbered_constraints_are_preserved` 的计数
#      相对 BASELINE **上升**（证明加的是规则，不是叙述回流）；
#   ② `test_skill_no_longer_carries_thread_level_narratives` 仍绿（没把故障叙述写回正文）；
#   ③ 在下面记一行「谁、为什么、从多少抬到多少」。
#
# 变更记录：
#   - eligibility-judgment 76_000 → 79_000（新增「日期/时间窗判定」C 条事件零命中短路表、
#     约束 14c ⓪、原则十 `window_moot_absence` 行；⛔ 计数 48 → 55，故障叙述已进档案）
#   - eligibility-judgment 79_000 → 79_500（2026-08-10，门禁循环治理 Phase 2）：两条新硬规则
#     —— ① 改判唯一允许工具改为 `apply_json_patches` 的对象级形态（pointer + op），
#     ② 反查闸 `exit 3` 熔断与 `cross_document_hits` 的处置。⛔ 计数 48 → 57，
#     调用示例与逐字段 pointer 已放 `references/judgment-repair.md`，正文只留规则本身。
#   - 两个技能各 +~200 bytes（2026-08-10，门禁循环治理 Phase 4 / Task 16）：新增「禁止用 bash
#     内联脚本（python3 -c / heredoc / echo >）生成或改写 .json 产物」硬规则。
#     eligibility-judgment 79_500 → 80_000（⛔ 57 → 58）、criteria-parser 38_500 → 39_000
#     （⛔ 28 → 29）。故障叙述在 failure-archive.md，正文只留禁令与指向。
#   - eligibility-judgment 80_000 → 80_500（2026-08-10，会话 `93d8a2c6` 复盘）：新增 **C 闸
#     取证素材包装配**（`evidence_bundle.py`，派 QC 前必跑）与「不装配就不许派 QC」硬规则。
#     ⛔ 58 → 59。该会话实测：input 占 98.9%、独立内容仅 956k（重传 18×~30×），一个 QC 子任务
#     逐条 grep+read 取证烧掉 1.97M 并耗尽 150 步失败 —— 治的是步数。完整叙述与数据已进
#     failure-archive.md「QC 逐条取证耗尽步数额度」，正文只留命令形状与禁令。
#   - eligibility-judgment 80_500 → **31_000**（2026-08-10）：判定规则本体搬去
#     `references/judgment-principles.md`，SKILL.md 收敛为主代理编排手册（80,433 → 约 30.2k，-62%）。
#     这是**下调**，不是上抬——推翻依据见文件开头。
#   - criteria-parser 39_000 → **13_000**（2026-08-10）：解析规则本体搬入
#     `references/parsing-rules.md`，SKILL.md 收敛为主代理编排手册（38,669 → 约 12.4k，-68%）。
#   - criteria-parser 13_000 → 13_500（2026-08-10，会话 `e3c15416` 复盘）：新增「派 QC 之前装配
#     取证素材包」硬前置（`criteria_qc_bundle.py`）。它**必须**在 SKILL.md：执行者是主代理。
#     包的内容清单放在 `criteria-qc-checklist.md`（QC 侧读），正文只留命令与禁令。
#   - criteria-parser/parsing-rules.md 34_000 → 34_500（2026-08-17，会话 `c80c47d9` 复盘）：
#     §拆分原则新增「两种**不得**归入限定性 AND 的 AND」——(a) 跨 `可从病例获取` 边界、
#     (b) 跨字段类型（定性 × 定量），各配一个真实样板（`EX-1` / `EX-12-1`）与对应的闸编号。
#     它**必须**在这里：执行者是解析子代理与修订子代理，而它们的 skills 白名单是 `[]`、
#     只读本文件。该会话 EX 轨三轮 QC 仍 `passed=false`，`EX-12-1`（`HBsAg 阳性` 且
#     `HBV-DNA > 10^3 IU/ml` 混在一条）是唯一未收敛项 —— 修订方因无字段可用而自创
#     schema 外的 `并列条件`。三项纪律：全集 ⛔ 计数上升（新增 3 条禁令）、
#     正文零 thread ID（叙述在 failure-archive.md「定性阈值与定量要件混用」）、
#     §常见拆分错误里 4 条同类「过度拆分」已合并抵掉约 200 bytes，净增即两条规则本身。
#   - criteria-parser 13_500 → 14_100（2026-08-13，闸往返治理）：新增两条硬规则 ——
#     ① 修订往返预算「全量闸每轨每轮 ≤ 2 次」+ `--only` 单条校验的用法；
#     ② ⛔ 禁止 grep 闸源码猜判据，改用 `--contract`。执行者是修订子代理与主代理，
#     必须在 SKILL.md。三项纪律均满足：全集 ⛔ 103 → 129、正文零 thread ID、
#     故障叙述留在 `failure-archive.md`，正文只有规则与命令形状。
#   - eligibility-judgment 31_000 → 32_600（2026-08-18，会话 `09eeaffb` 复盘）：判定改为
#     **轨内 12 条一批**（`judge_pack.py plan-batches`）。新增的都是主代理编排指令，必须在
#     SKILL.md：① `plan-batches` 命令形状；② 批级产物 `_b{N}` 与批级结构闸 `--batch N`；
#     ③ ⛔ 各批合并后必须以**整轨口径**再跑一次闸 2（唯一会因"漏派一整批"报错的地方）；
#     ④ ⛔ 不切标准包 / 不按四分类类目切 / 批次不跨轨；⑤ 新增第 3 类输入
#     `ocr_page_index.json`（页码→行区间）与"先读索引再决定读哪些行"的纪律。
#     治的是：整轨一次派让两个判定子代理各跑 99 个 AI 回合、**0 次 write_file**，双双撞满
#     `recursion_limit=420`，10.02M token / 42 分钟、产物为零（该分支不打捞部分产物）。
#     三项纪律均满足：全集 ⛔ 计数上升（新增禁令 6 条）、正文零 thread ID、故障叙述整节进
#     `failure-archive.md`「整轨一次判定撞 recursion_limit」，正文只留规则与命令形状。
#     净增 2,241 bytes 已无可压（无叙述、表格已按 `{T}`/`{N}` 占位符收敛）；上抬到 32_600 留
#     ~80 bytes 余量，⛔ 不是给下次随手加字用的。
#   - criteria-parser 14_100 → 14_650（2026-08-18，会话 `5aa5d6d6` 复盘）：`criteria_qc_bundle.py`
#     的段定位失败从「静默退回整篇」改成 `exit 2`，SKILL.md 的「派 QC 之前」段随之要写清
#     **两类 exit 2** 与②的唯一处置（修标题/正则后重跑，⛔ 不得跳过取证包直接派 QC）。
#     它必须在 SKILL.md：执行者是主代理（装配取证包是主代理的硬前置）。
#     治的是：段定位两次匹配不上真实 raw.md（标题带 `4.1` 前缀 / end 探测回溯命中段内
#     `#### 小标题`），两次都带着**全错的映射** `exit 0` —— 一轨 QC 绕过、一轨据此产出假阳性
#     阻断项，主代理为查 bug 花掉 10 分钟与两次全量脚本重写，13 条真阻断项一条未修。
#     ⚠️ 抬闸前该文件已是 14_384（超限 284 bytes，非本次引入；本次净增 200 bytes 已是
#     规则+命令的最小表述）。三项纪律：全集 ⛔ 计数上升（新增 2 条禁令）、正文零 thread ID
#     （叙述在 failure-archive.md「取证包段定位静默失败」）、正文只留判据与处置。
#   - criteria-parser 14_650 → 15_000（2026-08-26，完整技能包重组）：用户要求 SKILL.md 自含
#     拆分/质检/修复三域核心判据（此前是指针手册，一个执行者读 SKILL.md 仍要跳 3-4 份
#     references 才能干活）。内联的是**判据与操作**（拆分判定流程/两类必拆 AND/或组枚举/
#     阈值三档/结构闸 16 闸要点/按 type 处置表/中性化三选一/终点线纪律），措辞与
#     parsing-rules 唯一权威逐字对齐（抽句不转述防漂移）；样例/模板/故障仍在 references。
#     三项纪律：全集 ⛔ 计数上升（硬规则密度高于旧版）、正文零 thread ID、
#     故障叙述零回流（全部仍在 failure-archive.md）。
#   - criteria-parser 15_000 → 17_000（2026-08-26，SOUL P2 再下沉）：用户要求把 P2 的
#     拆分/QC/修复/合并流程与并发规则移入本技能。SKILL §4 扩为完整编排节（4.1 入口与
#     路线 / 4.2 并发预算与调度[三轨共享 3、OCR≤2、滑动窗口、OCR 覆盖率门禁] /
#     4.3 双轨与修复执行 / 4.4 切包与收尾），SOUL Phase 2 段 40→14 行（只留 todos、
#     指针、并发处理脚本缺陷的编排决策一句、出口屏障）。内联为编排判据与禁令，
#     非叙述；三项纪律同前（⛔ 上升 / 零 thread ID / 零叙述回流）。
#   - criteria-parser 17_000 → 19_000（2026-08-26，SOUL 原则5 criteria 部分下沉）：§2.2 增补
#     达上限两态处置与触顶五步（冻结/两轨定局一次 ask_clarification 三选一/P3 屏障）、
#     upstream 破例核实、QC 报脚本缺陷时修脚本与派修订并发（含 skill_manage patch 前置）。
#     SOUL 原则5 压为骨架+双域指针；判定部分经核已在 eligibility-judgment SKILL（无增量）。
#     全为硬规则与处置流程，三项纪律同前。
MAX_BYTES = {
    "criteria-parser": 19_000,
    "eligibility-judgment": 32_600,
}

# 按需加载的规则文件也要有上限：判定子代理会**整篇**读它，无上限就会重新长成一本书。
# 比 SKILL.md 宽松，因为它承载的是全部判定规则；但不能无界。
MAX_REFERENCE_BYTES = {
    "eligibility-judgment/judgment-principles.md": 60_000,
    "criteria-parser/parsing-rules.md": 34_500,
}


# thread 级故障叙述的特征：8 位 hex 会话 ID 出现在反引号里。
_THREAD_ID = re.compile(r"`[0-9a-f]{8}`")


def _skill(name: str) -> Path:
    return SKILLS / name / "SKILL.md"


def _corpus(name: str) -> str:
    """技能全集正文：SKILL.md + references/*.md。

    「规则有没有被删掉」是**技能级**问题，不是单文件问题。只量 SKILL.md 的话，把一条规则从
    SKILL.md 挪进 references 会被误判为删除，而真正的删除（从两处都删掉）反而可能漏过。
    """
    parts = [_text(_skill(name))]
    refs = SKILLS / name / "references"
    if refs.is_dir():
        parts += [_text(p) for p in sorted(refs.glob("*.md"))]
    return "\n".join(parts)


def _archive(name: str) -> Path:
    return SKILLS / name / "references" / "failure-archive.md"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _headings(s: str) -> list[str]:
    return [ln for ln in s.splitlines() if re.match(r"^#{1,6} ", ln)]


def _stop_marks(s: str) -> int:
    return s.count("⛔")


def _numbered(s: str) -> int:
    return len([ln for ln in s.splitlines() if re.match(r"^[0-9]+\. \*\*", ln)])


import pytest  # noqa: E402  (放在常量后，便于上面的说明连贯)

NAMES = ("criteria-parser", "eligibility-judgment")


# ── 档案文件本身 ────────────────────────────────────────────────────


@pytest.mark.parametrize("name", NAMES)
def test_failure_archive_exists(name):
    p = _archive(name)
    assert p.exists(), f"缺 {p.relative_to(REPO)}"
    assert len(_text(p)) > 1000, "档案不能是空壳——叙述得真搬进去"


@pytest.mark.parametrize("name", NAMES)
def test_failure_archive_is_indexed_by_the_skill(name):
    """技能正文必须能指到档案，否则没人知道它存在。"""
    assert "failure-archive.md" in _text(_skill(name))


@pytest.mark.parametrize("name", NAMES)
def test_failure_archive_has_anchors(name):
    """每条故障一个 `##` 锚点，指针才能精确到条。"""
    body = _text(_archive(name))
    anchors = [ln for ln in body.splitlines() if ln.startswith("## ")]
    assert len(anchors) >= 5, f"档案锚点太少（{len(anchors)}），指针无法精确定位"


# ── 叙述已搬走 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", NAMES)
def test_skill_no_longer_carries_thread_level_narratives(name):
    """SKILL.md 正文不得再出现 thread 级会话 ID —— 那是叙述而非规则。

    唯一例外：指针行本身可以写「见 failure-archive.md#xxx」，但不带 8 位 hex。
    """
    offenders = [(i, ln.strip()) for i, ln in enumerate(_text(_skill(name)).splitlines(), start=1) if _THREAD_ID.search(ln)]
    assert not offenders, "正文仍有 thread 级叙述：" + "; ".join(f"L{i}:{t[:80]}" for i, t in offenders[:8])


@pytest.mark.parametrize("name", NAMES)
def test_archive_actually_holds_the_thread_ids(name):
    """搬走 ≠ 删掉。会话 ID 必须在档案里找得到。"""
    assert _THREAD_ID.search(_text(_archive(name))), "档案里没有任何会话 ID，叙述可能被直接删了"


# ── 体积 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", NAMES)
def test_skill_is_under_the_size_cap(name):
    size = len(_text(_skill(name)).encode("utf-8"))
    # 失败讯息里原本引 `BASELINE[name]["bytes"]`，而 BASELINE 只有 headings/stop_marks/numbered
    # 三个键（体积基线在这个 dict 里从来不存在）—— 于是一旦超限，断言尚未求值就先 KeyError，
    # 报出来的是 `KeyError: 'bytes'` 而不是「谁超了、超了多少」。
    assert size <= MAX_BYTES[name], f"{name}/SKILL.md {size} bytes 超过上限 {MAX_BYTES[name]}（抬闸纪律见本文件顶部注释）"


@pytest.mark.parametrize("name", NAMES)
def test_narrative_was_really_externalised(name):
    """证明叙述**确实搬出去了** —— 用可持续的判据，不是拿字节数跟旧基线比。

    ⚠️ 这条原本写的是 `size < BASELINE[name]["bytes"]`（「体积没有下降，等于没搬」）。
    那个判据把两件事混在一起：「叙述有没有搬走」与「文件有没有长大」。规则**本来就会**
    合法增长（每修一个故障就多几条硬规则），于是旧判据会在下一次修 bug 时必然变红，
    而红的原因跟它想防的事毫无关系 —— 那种闸只会教人学会绕闸。

    叙述外移的真正证明是：① 正文零 thread ID（`test_skill_no_longer_carries_thread_level_narratives`
    已锁）；② 故障档案存在、有足够多锚点；③ 正文用指针引它。规则增长不会破坏这三条中的任何一条。
    体积则由 `MAX_BYTES` 棘轮单独管。
    """
    archive = _skill(name).parent / "references" / "failure-archive.md"
    assert archive.exists(), f"{name} 缺 references/failure-archive.md"
    anchors = [ln for ln in archive.read_text(encoding="utf-8").splitlines() if ln.startswith("## ")]
    assert len(anchors) >= 5, f"{name} 故障档案只有 {len(anchors)} 个锚点，叙述没真正搬进去"
    # 口径同 BASELINE：规则搬到 references 后，指针会跟着规则一起走，所以按**技能全集**数。
    # 只数 SKILL.md 会把"规则搬家"误判成"指针失联"。
    pointers = _corpus(name).count("failure-archive.md")
    assert pointers >= 12, f"{name} 全集只有 {pointers} 处档案指针，规则与出处失联"


# ── 规则守恒（防止搬着搬着把规则删了）──────────────────────────────


@pytest.mark.parametrize("name", NAMES)
def test_headings_are_preserved(name):
    got = _headings(_corpus(name))
    assert len(got) >= BASELINE[name]["headings"], f"技能全集标题从 {BASELINE[name]['headings']} 掉到 {len(got)} —— 有章节被整段删掉（搬家不会减少总数）"


@pytest.mark.parametrize("name", NAMES)
def test_hard_rule_markers_are_preserved(name):
    got = _stop_marks(_corpus(name))
    assert got >= BASELINE[name]["stop_marks"], f"技能全集 ⛔ 从 {BASELINE[name]['stop_marks']} 掉到 {got} —— 有硬规则被删掉（搬到 references 不会减少总数）"


@pytest.mark.parametrize("name", NAMES)
def test_numbered_constraints_are_preserved(name):
    got = _numbered(_corpus(name))
    assert got >= BASELINE[name]["numbered"], f"技能全集编号约束从 {BASELINE[name]['numbered']} 掉到 {got} —— 有条目被删"


def test_judgment_constraint_list_is_intact():
    """约束清单被 delegation / QC 按**编号**引用，被引用的编号不得消失或错位。

    2026-08-10 起清单住在 `references/judgment-principles.md`（不再在 SKILL.md）。搬家时编号
    一字未动，正是因为它是外部契约。

    实测被引用的编号：2 / 5 / 7 / 10 / 16 / 17 / 18 / 19（`grep -rhoE "约束 ?#?[0-9]+"`）。
    清单里既有 `16. **…**` 也有 `17. …`（无粗体），所以不校验格式，只校验编号 + 关键词。
    """
    body = _text(SKILLS / "eligibility-judgment" / "references" / "judgment-principles.md")
    expected = {
        16: "不可从病例获取",
        17: "任一排除标准",
        18: "按组汇总",
        27: "禁止伪无法判断",
    }
    for num, keyword in expected.items():
        line = next((ln for ln in body.splitlines() if ln.startswith(f"{num}. ")), None)
        assert line is not None, f"约束 {num} 不见了"
        assert keyword in line, f"约束 {num} 错位：应含 {keyword!r}，实为 {line[:80]!r}"


# ── 关键规则逐条点名（最容易被顺手删掉的那些）────────────────────────


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        # ⚠️ criteria-parser 的解析规则 2026-08-10 搬入 references/parsing-rules.md，
        # 所以这里只留主代理仍需的：修订禁令（编排要把它抄进模板）与指向。
        ("criteria-parser", "parsing-rules.md"),  # 指向表必须在
        # 修订禁令。2026-08-10 起唯一允许的写入方式是 apply_json_patches 的对象级形态
        # （pointer + op）；被禁的是 write_file 与 str_replace。改点名工具名而不是措辞，
        # 这样规则再改写也不会让契约失效。
        ("criteria-parser", "apply_json_patches"),
        ("eligibility-judgment", "符合 = 符合入组"),  # 排除项反直觉口诀（留在 4 级判定体系）
        ("eligibility-judgment", "原则十一"),  # 指向表里必须还能查到它去哪了
        ("eligibility-judgment", "uncertain_recheck.py"),
        ("eligibility-judgment", "check_reason_alignment.py"),
    ],
)
def test_key_rules_survive(name, needle):
    """最容易被顺手删掉的规则/指针，必须还在 SKILL.md 里找得到。

    ⚠️ 判定规则本体 2026-08-10 起在 `references/judgment-principles.md`，所以这里留下的是
    **主代理仍需知道的**：反直觉口诀、闸脚本名（编排要跑/要验收）、以及"原则十一去哪查"的指针。
    规则本体的存活由 `test_judgment_rules_live_in_the_principles_reference` 守。
    """
    assert needle in _text(_skill(name)), f"{name}/SKILL.md 丢了关键规则/指针：{needle!r}"


# ── 判定规则本体：搬去 references 之后由这里守 ────────────────────────

JUDGMENT_PRINCIPLES = SKILLS / "eligibility-judgment" / "references" / "judgment-principles.md"


@pytest.mark.parametrize(
    "needle",
    [
        "原则一：统一证据源",
        "原则五",  # 禁止伪「无法判断」
        "原则七",  # 穷尽取证 + 三要素理由
        "原则八",  # uncertain_recheck 兜底闸
        "原则九",  # 排除项方向自检
        "原则十",  # reason 对齐闸
        "原则十一",  # 药物归类三步判据
        "false_absence_claim",
        "判定约束清单",
        "日期/时间窗判定",
        "逻辑关系处理",
    ],
)
def test_judgment_rules_live_in_the_principles_reference(needle):
    """判定规则本体必须在 judgment-principles.md —— 它是判定子代理唯一会读到规则的地方。"""
    assert JUDGMENT_PRINCIPLES.exists(), "judgment-principles.md 不存在：规则无处可读"
    assert needle in _text(JUDGMENT_PRINCIPLES), f"judgment-principles.md 丢了：{needle!r}"


def test_skill_md_points_at_every_moved_topic():
    """SKILL.md 必须给出「规则去哪查」的指向 —— 搬走而不留指针 = 主代理再也找不到。"""
    body = _text(_skill("eligibility-judgment"))
    assert "judgment-principles.md" in body, "SKILL.md 未索引 judgment-principles.md"
    for topic in ("原则七", "原则八", "原则九", "原则十", "原则十一", "判定约束"):
        assert topic in body, f"SKILL.md 的指向表缺「{topic}」，搬走的规则失去入口"


def test_skill_md_is_now_an_orchestration_manual():
    """SKILL.md 只留编排：不得再长回判定规则本体。"""
    body = _text(_skill("eligibility-judgment"))
    for orchestration in ("判定分片与合并", "质控（QC）核验", "交付文件清单"):
        assert orchestration in body, f"SKILL.md 丢了编排内容：{orchestration}"
    assert "### 原则一：统一证据源" not in body, "判定规则本体又长回 SKILL.md 了"


def test_delegation_template_gives_the_readable_path_to_the_rules():
    """子代理 `skills=[]`，只能 read_file。模板必须给**绝对路径**，不能只说「看 SKILL 原则 X」。"""
    body = _text(SKILLS / "eligibility-judgment" / "references" / "judge-delegation.md")
    assert "/mnt/skills/custom/eligibility-judgment/references/judgment-principles.md" in body
    assert "skills" in body and "[]" in body, "必须写明子代理不会自动加载 SKILL.md 的前提"
    assert "SKILL 原则" not in body, "模板里不得再留指向 SKILL.md 原则的悬空引用"


@pytest.mark.parametrize("rel", sorted(MAX_REFERENCE_BYTES))
def test_reference_is_under_its_cap(rel: str):
    skill, name = rel.split("/", 1)
    path = SKILLS / skill / "references" / name
    if not path.exists():
        pytest.skip(f"{rel} 未安装")
    size = len(path.read_bytes())
    assert size <= MAX_REFERENCE_BYTES[rel], f"{rel} 涨到 {size:,} bytes，超过 {MAX_REFERENCE_BYTES[rel]:,}"


# ── criteria-parser 解析规则本体：搬去 references 之后由这里守 ──────────

PARSING_RULES = SKILLS / "criteria-parser" / "references" / "parsing-rules.md"


@pytest.mark.parametrize(
    "needle",
    [
        "四分类体系",
        "拆分原则",
        "条件ID编号规则",
        "条件转化规则",
        "日期维度规则",
        "可获取性判定标准",
        "输出格式",
        "OR分支",  # 逻辑关系枚举
        "三档",  # 阈值可写文字描述的判据
        "禁止读原始方案文档",  # 本轨边界硬规则
        "分片写入",  # 解析阶段分片
        "python3 -c",  # 禁内联脚本生成 JSON
    ],
)
def test_parsing_rules_live_in_the_parsing_reference(needle):
    """解析规则本体必须在 parsing-rules.md —— 它是解析子代理唯一会读到规则的地方。"""
    assert PARSING_RULES.exists(), "parsing-rules.md 不存在：解析规则无处可读"
    assert needle in _text(PARSING_RULES), f"parsing-rules.md 丢了：{needle!r}"


def test_criteria_skill_points_at_every_moved_topic():
    """2026-08-26 起规则核心内联进 SKILL.md(完整技能包),本测试守住的是「入口不丢」:
    主题要么在内联正文、要么在 references 索引——措辞随结构演化,清单同步。"""
    body = _text(_skill("criteria-parser"))
    assert "parsing-rules.md" in body, "SKILL.md 未索引 parsing-rules.md"
    for topic in ("四分类体系", "拆分判定流程", "条件ID 编号", "转化条件", "日期维度", "不拆清单", "分片写入"):
        assert topic in body, f"SKILL.md 缺「{topic}」，规则失去入口"


def test_criteria_skill_is_now_an_orchestration_manual():
    body = _text(_skill("criteria-parser"))
    # 2026-08-26 完整技能包重组:编排四节并入 §0 输入/双轨拆分/质检/修复/编排与交付
    for orchestration in ("章节提取与完整性自检", "双轨拆分", "质检", "修复", "编排与交付"):
        assert orchestration in body, f"SKILL.md 丢了编排内容：{orchestration}"
    assert "### 必须拆分（AND 关系）" not in body, "解析规则本体又长回 SKILL.md 了"


def test_parse_delegation_gives_the_readable_path_to_the_rules():
    """子代理 `skills=[]`，规则必须**到达**它。2026-08-26 起由 render 内嵌:模板带
    {PARSING_RULES} 占位符,渲染时从 parsing-rules.md 抽节嵌入正文(881e7ba8:子代理
    自读 34KB 全文,EX 重做前 4 步 ~200k token 学规则,写产物时上下文耗尽 → 占位符产物)。
    模板本体仍保留路径引用供人读。"""
    body = _text(SKILLS / "criteria-parser" / "references" / "parse-delegation.md")
    assert "{PARSING_RULES}" in body, "模板缺规则内嵌占位符(render 的注入点)"
    assert "/mnt/skills/custom/criteria-parser/references/parsing-rules.md" in body
    assert "SKILL.md（四分类体系" not in body, "模板仍把子代理指向整篇 SKILL.md"
