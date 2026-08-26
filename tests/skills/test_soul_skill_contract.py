"""SOUL / SKILL 职责分离契约测试。

守护 `docs/plans/eligibility-screener-soul-skill-split-dual-track-plan.md` 的三条不变量：

1. **下沉不回流** —— SOUL.md 不得再出现属于某个 skill 的执行细节（命令、模板正文、技能内规则）。
2. **技能不引用编排阶段号** —— SKILL.md / references 只按「输入产物 → 输出产物」描述，
   不写 `Phase 2` / `P2.5`。历史漂移：`criteria-parser` 曾写「步骤 2: QC 校验（Phase 2.5）」，
   而 SOUL 的 Phase 2.5 是患者拆分，两处规则已经对不上。
3. **证据链不丢** —— 被搬迁的硬规则措辞与历史故障 thread 编号必须仍存在于语料中的某处
   （SOUL 或任一 SKILL/references），防止「重构顺手简化」。

语料 = SOUL.md + 5 个 SKILL.md + 各 skill 的 references/*.md。
`skills/custom` 与 `backend/.deer-flow/agents` 都是 gitignored 的本地目录，缺失时整体跳过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOUL_PATH = REPO_ROOT / "backend" / ".deer-flow" / "agents" / "eligibility-screener" / "SOUL.md"
SKILLS_ROOT = REPO_ROOT / "skills" / "custom"

SKILL_NAMES = (
    "criteria-parser",
    "eligibility-judgment",
    "patient-separator",
    "pdf-image-extractor",
    "screening-report-generator",
)

if not SOUL_PATH.exists() or not SKILLS_ROOT.exists():
    pytest.skip(
        "eligibility-screener agent 或 skills/custom 未安装（本地 gitignored 目录）",
        allow_module_level=True,
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _skill_md(name: str) -> Path:
    return SKILLS_ROOT / name / "SKILL.md"


def _skill_docs(name: str) -> list[Path]:
    """某 skill 的 SKILL.md + references/*.md。"""
    paths = [_skill_md(name)]
    refs = SKILLS_ROOT / name / "references"
    if refs.is_dir():
        paths.extend(sorted(refs.glob("*.md")))
    return [p for p in paths if p.exists()]


def _corpus() -> dict[Path, str]:
    docs: dict[Path, str] = {SOUL_PATH: _read(SOUL_PATH)}
    for name in SKILL_NAMES:
        for path in _skill_docs(name):
            docs[path] = _read(path)
    return docs


CORPUS = _corpus()
SOUL = CORPUS[SOUL_PATH]


# ---------------------------------------------------------------------------
# 不变量 1：下沉的执行细节不得留在 SOUL
# ---------------------------------------------------------------------------

# 关键词 → 应该拥有它的 skill（断言失败信息里直接给出去处）
SUNK_FROM_SOUL: dict[str, str] = {
    # Task 4 · pdf-image-extractor
    "classify_uploads.py \\": "pdf-image-extractor（脚本用法）",
    "pdf_to_image.py \\": "pdf-image-extractor（拆页命令）",
    "ocr_coverage.py \\": "pdf-image-extractor（覆盖率门禁命令）",
    "parse_document 调用铁律": "pdf-image-extractor（技能是执行规则唯一权威）",
    "route_a_failed": "pdf-image-extractor（OCR 委派模板 references/ocr-delegation.md）",
    # Task 3 · criteria-parser
    "eligibility_criteria_raw.md`；随后": "criteria-parser（章节提取与完整性自检）",
    "边界锚定提取": "criteria-parser（references/criteria-extraction.md）",
    "逐字完整提取": "criteria-parser（references/criteria-extraction.md）",
    "Schema：/mnt/skills/custom/criteria-parser": "criteria-parser（references/parse-delegation.md）",
    # Task 5 · patient-separator
    "out_dir.mkdir(parents=True": "patient-separator（references/aggregate-ocr.md 聚合脚本）",
    "patient['source_files']": "patient-separator（references/aggregate-ocr.md 聚合脚本）",
    # Task 6 · eligibility-judgment
    "请按 /eligibility-judgment 技能规则，对患者": "eligibility-judgment（references/judge-delegation.md）",
    "uncertain_recheck.py --criteria": "eligibility-judgment（references/judge-delegation.md）",
    "exclusion_direction_check.py --judgments": "eligibility-judgment（references/judge-delegation.md 与 qc-delegation.md）",
    "取证索引": "eligibility-judgment（原 SOUL 原则 10 整块下沉）",
    # 2026-08-26 · P2 解析编排下沉(parse-orchestration.md + render/summary 脚本)
    "criteria_qc_bundle.py \\": "criteria-parser（取证包装配命令，parse-orchestration.md）",
    "ocr_md_count": "criteria-parser（phase2_summary 字段清单，write_phase2_summary.py）",
    # Task 7 · screening-report-generator
    "build_reports.py \\": "screening-report-generator（构建/校验命令）",
}


@pytest.mark.parametrize("needle,owner", sorted(SUNK_FROM_SOUL.items()))
def test_soul_does_not_keep_skill_owned_detail(needle: str, owner: str):
    assert needle not in SOUL, f"SOUL.md 仍含应下沉到 {owner} 的内容：{needle!r}"


def test_soul_stays_an_orchestration_skeleton():
    """SOUL 是常驻 system prompt，重构目标是「只留编排骨架」。

    上限 750 行的来历（每次上调都必须在此记录理由）：
    - 重构前 964 行 → 下沉后实测 662 行。
    - thread `03a496cc`（`ask_clarification` 与其他 tool_call 同轮导致 `goto=END` 失效，
      用户看到 3 遍选择）→ 「`ask_clarification` 独占一轮」+ 按同一原则的「`write_todos`
      独占一轮」，Phase 1 轮次相应拆开 → 691 行。
    - P4 改判规范（可委派子代理 + 结构闸/守恒闸时序）→ 710 行。
    - thread `345f2bf4`（① P2 QC 连续 3 轮阻断项完全相同、空转触顶两次；② 两轨判定子代理
      各自自创 document 键，`merge-judgments` 并列成假文档而非合并）→ QC 第三档
      `upstream_issues` + 判定委派须显式给定 document 键 + 结构闸产物让子代理自行拒工 → 713 行。
    - thread `ec37dc7d`（Phase 1 该 present 分类结果与入排原文，但 SOUL 给的是 `workspace/` 路径，
      而 `present_files` 只接受 `outputs/` 下的路径且不校验存在性 → 模型改 present 了尚不存在的
      `outputs/criteria_parsed.json`，工具报成功、用户什么也没拿到）→ 原则 9 新增「present 三步法」
      （先 cp+ls 确认、下一轮再 present `outputs/` 路径）+ 四处 present 站点修正 +
      交付清单与目录规范补 P1/P2.5 产物 → 733 行。
    - thread `ec24d087`（方案 .md 有 131 个 `\f`，`grep -n` 与 `read_file` 的 `splitlines()`
      行号错位 → 只提取到排除 1..11（真实 20 条），且自检脚本在同一错误坐标系里数源末条号也得 11，
      `n == N` 空过）→ P1 行号定位与完整性自检改为强制调 `locate_criteria_sections.py` → 735 行。
    - thread `6e5ac7c1`（委派模板把 `段行号`（试验方案.md 坐标）用于 read_file
      `eligibility_criteria_raw.md` → 越界切片，`read_file` 静默返回空串 → 两个解析子代理
      凭空编造 54 条中的 50 条，结构闸 1-8 因条件ID 体系自洽而全过）→ 新增 `raw段行号`
      与结构闸 9（原文忠实性）+ 子代理开工自检 → 738 行。
    - thread `69612125`（`pdf_to_image.py` 对文本层页只写 `.txt` 不渲染图片，OCR 子代理只处理
      图片，而覆盖率分母只算 `scanned` 页 → 26 页里 11 页文本层证据（含 KRAS 基因检测报告）
      静默丢失却报 `covered=True`，`IN-4-1` 因此被判「无法判断：缺基因检测报告」）
      → 分母改为全部页 + 新增 `collect_text_pages.py` 归集脚本 → 739 行。

    计划里最初写的 ~400 行是在双轨改造定案前估的。逐段核过后确认剩下的都是承重内容：
    严禁跳步 9 条 + 10 条公共原则 + 8 个 Phase 的编排 + 目录规范/Todolist 契约。
    再压就要删规则，而「不遗漏、不简化」是硬要求，因此把闸设在实测值 + 少量余量，
    用来挡「细节回流」而不是逼迫删规则。

    - thread `88df83a8`（EX 轨判定子代理在任务内被压缩 4 次后改写目标，写出自创的
      `qc_review_report.json` 而不是 `judgments_draft_MCRC-2150006_EX.json`，却以 `completed`
      回报；主代理 8 分钟后才靠结构闸发现产物不存在，重派时 run 已结束，整轨作废）
      → Phase 3 新增「每个判定 `task` 必须带 `expected_outputs`」4 行。
      这是**编排层**规则而非技能执行细节：`expected_outputs` 是 `task` 调用的参数，
      只有派任务的人能传；故障叙事与产物清单留在 `judge-delegation.md`，SOUL 只留一句纪律
      + 指针 → 755 行，上限 750 → 760。
    - 会话 `5aa5d6d6`（两轨 QC 都报告取证包 `原条号 → raw 原文` 映射缺陷；主代理正确判断出这是
      真 bug、也正确推断出 EX-6 是假阳性，但把「修脚本」与「派修订」**串成一条链**：花 10 分钟、
      两次全量重写 ~300 行脚本（第二次只改 4 个字符），13 条真阻断项一条未修，会话被取消）
      → 原则 7 新增「发现工具/脚本缺陷时」9 行 + Phase 2 QC 循环 3 行指针 → 769 行，上限 760 → 772。
      这是**编排层**规则：`skill_manage` 是主代理独有的工具（子代理拿不到），而"该派谁做"本身
      就是派发决策。故障叙事进 `criteria-parser/references/failure-archive.md`
      「取证包段定位静默失败」，SOUL 只留纪律 + 判据（结构闸已过而 QC 报原文不符 → 错的是素材）
      + 指针。⚠️ 余量只剩 3 行，⛔ 不是给下次随手加字用的。

    ── 2026-08-19：**下调** 772 → 610（实测 589）────────────────────────────────
    本次是压缩，不是扩容，所以闸随实测值一起降。做了四件事，都不删规则：

    1. **bad case 叙事全部移出**。原 SOUL 里 22 处 thread 号/统计数字，其中 5 个会话
       （`ab76d625` / `459951c1` / `03a496cc` / `d1ce04c0` / `ec37dc7d`）此前**只存在于 SOUL**，
       现已落到新建的三个 failure-archive（`pdf-image-extractor` 4 个 +
       `screening-report-generator` 1 个），另建 `patient-separator` 的档案收纳其两处内联叙事。
       规则句与后果句留在 SOUL，考古留在 archive —— `EVIDENCE_MUST_SURVIVE` 保证不丢。
    2. **领域执行细节删重复留指针**。34 条受保护措辞里 30 条本来就已在 skill 中存在，
       SOUL 那份是复述（解析手法、聚合规则、三条机械闸判据、从严判断整段等）。
    3. **屏障去重**：新增「阶段推进检查表」作为屏障的**单一真相表**，各 Phase 原先各自重复的
       「⛔ 出口屏障」指针行随之删除。
    4. **强化流程控制**（用户要求，压缩的同时加）：新增「本轮该发什么」五步决策序，
       把"禁止空等"从口号变成可执行步骤；Phase 小节统一为
       入口 → 调度 → 屏障 → 产出 → todos 的固定格式。

    为什么落在 589 而不是更激进的 ~400：逐段核过编排特征占比后，剩下的低占比段是
    **严禁跳步 9 条 + 目录规范 + Todolist 模板** —— 用户明确要求保留目录结构与流程控制，
    且 `criteria-qc-checklist.md:346` 明写「达轮次上限的处置**由编排层决定**」，
    那 5 步请示流程不能下沉。再压就要删规则，而「不遗漏、不简化」是硬要求。
    余量 21 行，⛔ 同样不是给随手加字用的。
    """
    line_count = len(SOUL.splitlines())
    assert line_count <= 750, f"SOUL.md {line_count} 行，超出编排骨架上限（实测基准 733，上限 750）；新增内容请先确认不属于某个 skill"
    # 2026-08-26 修正：一段未落地的 589 行收缩草稿曾把上限压到 610，与实际 702 行的
    # SOUL.md 长期不匹配，制造了一个假的「既有失败」。上限回到 750（基准 733）；
    # 若重启 SOUL 收缩，请以届时实测行数重新定档，不要沿用草稿数字。


# ---------------------------------------------------------------------------
# 不变量 2：技能不引用编排阶段号
# ---------------------------------------------------------------------------

PHASE_REF_PATTERNS = (
    "Phase 1",
    "Phase 2",
    "Phase 3",
    "Phase 4",
    "Phase 5",
    "P2.5",
    "P1.5",
)


@pytest.mark.parametrize("skill", SKILL_NAMES)
def test_skill_docs_do_not_reference_orchestration_phases(skill: str):
    offenders: list[str] = []
    for path in _skill_docs(skill):
        text = _read(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in PHASE_REF_PATTERNS:
                if pattern in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} [{pattern}] {line.strip()[:90]}")
    assert not offenders, "技能文档引用了 SOUL 的阶段编号（改为按输入/输出产物描述）：\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 不变量 2b：references 层齐备且被 SKILL.md 索引
# ---------------------------------------------------------------------------

EXPECTED_REFERENCES: dict[str, tuple[str, ...]] = {
    "criteria-parser": (
        "criteria-extraction.md",
        "parse-delegation.md",
        "criteria-qc-checklist.md",
        "failure-archive.md",
    ),
    # 2026-08-19：SOUL 压缩把编排层 bad case 叙事迁到各 skill 的 failure-archive。
    # 这三个此前没有档案文件，是新建的收纳点——列在这里，让「必须被 SKILL.md 索引」
    # 与「不得成为孤儿文件」两条既有断言一起覆盖它们。
    "pdf-image-extractor": ("ocr-delegation.md", "mode-selection.md", "failure-archive.md"),
    "patient-separator": ("aggregate-ocr.md", "failure-archive.md"),
    "screening-report-generator": ("failure-archive.md",),
    "eligibility-judgment": (
        "judge-delegation.md",
        "qc-delegation.md",
        "judgment-repair.md",
        "judgment-schema.md",
        # 判定规则本体（2026-08-10 从 SKILL.md 搬入）。SKILL.md 必须索引它，否则主代理无从
        # 得知规则在哪，委派模板也就无从指路。
        "judgment-principles.md",
        "failure-archive.md",
    ),
}


# ---------------------------------------------------------------------------
# 不变量 1b：SOUL 是流程控制骨架，固定结构必须在位（2026-08-19 压缩后新增）
# ---------------------------------------------------------------------------
#
# 压缩会让文档"读起来像清单"，但主代理需要的是**可照着走的状态机**。这三条断言把压缩时
# 新建的流程控制结构钉住，防止下次重构把它们改回散文——散文没有可核对的缺项。


def test_soul_has_a_per_turn_dispatch_decision_order():
    """「本轮该发什么」把"禁止空等"从口号变成可执行步骤。"""
    assert "本轮该发什么" in SOUL, "缺少每轮开始的派发决策序（流程控制入口）"
    section = SOUL.split("本轮该发什么", 1)[1][:900]
    for marker, why in (
        ("write_todos", "第 1 步：状态变更独占一轮"),
        ("ask_clarification", "第 2 步：中断调用独占一轮"),
        ("补派到 3", "第 3 步：把在途 task 补满并发预算"),
        ("禁止空等", "第 5 步：没有可发的就推进阶段，不许干等"),
    ):
        assert marker in section, f"派发决策序缺少 {marker}：{why}"


def test_soul_has_a_single_source_barrier_checklist():
    """屏障此前分散在原则、各 Phase 末尾与 P3 启动闸三处，有重复也有缺口。"""
    assert "阶段推进检查表" in SOUL, "缺少屏障的单一真相表"
    # 按**标题**切，不按词切——"阶段推进检查表"也出现在派发决策序的指针里。
    heading = "## ⛔ 阶段推进检查表"
    assert heading in SOUL, f"检查表标题应为 {heading!r}（便于按标题定位）"
    table = SOUL.split(heading, 1)[1][:2600]
    # 每一次跨阶段都必须在表里有一行
    for transition in ("1 → 1.5", "1.5 → 2", "2 → 2.5", "2.5 → 3", "3 → 4", "4 → 4.5", "4.5 → 5"):
        assert transition in table, f"检查表缺少 {transition} 这一跨阶段行"
    assert "不成立时" in table, "检查表未给出条件不成立时的动作列"


def test_soul_keeps_no_failure_narrative_of_its_own():
    """bad case 叙事一律在 skill 的 failure-archive；SOUL 只留规则与后果。

    允许出现的例外是**指针**（"叙事见 …failure-archive.md"），不是叙事本身。
    判据：SOUL 不得再出现「历史故障 thread `xxxxxxxx`」这类考古句式。
    """
    offenders = [line.strip()[:100] for line in SOUL.splitlines() if "历史故障 thread" in line or "真实故障 会话" in line]
    assert not offenders, "SOUL 仍保留故障叙事（应移入对应 skill 的 references/failure-archive.md）：\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "skill,ref",
    [(skill, ref) for skill, refs in sorted(EXPECTED_REFERENCES.items()) for ref in refs],
)
def test_expected_reference_file_exists(skill: str, ref: str):
    path = SKILLS_ROOT / skill / "references" / ref
    assert path.exists(), f"缺少 {path.relative_to(REPO_ROOT)}（长模板应下沉到 references 层，SKILL.md 只留索引）"


@pytest.mark.parametrize(
    "skill,ref",
    [(skill, ref) for skill, refs in sorted(EXPECTED_REFERENCES.items()) for ref in refs],
)
def test_expected_reference_is_indexed_by_skill_md(skill: str, ref: str):
    """references 必须被 SKILL.md 提及，否则渐进加载时子代理不知道去读它。"""
    md = _skill_md(skill)
    if not md.exists():
        pytest.fail(f"缺少 {md.relative_to(REPO_ROOT)}")
    assert ref in _read(md), f"{skill}/SKILL.md 未索引 references/{ref}"


def test_no_orphan_reference_files():
    """references 下的每个 .md 都要被 SKILL.md 索引，避免搬过去却没人读。"""
    orphans: list[str] = []
    for skill in SKILL_NAMES:
        refs_dir = SKILLS_ROOT / skill / "references"
        if not refs_dir.is_dir():
            continue
        md = _skill_md(skill)
        index = _read(md) if md.exists() else ""
        for ref in sorted(refs_dir.glob("*.md")):
            if ref.name not in index:
                orphans.append(f"{ref.relative_to(REPO_ROOT)}")
    assert not orphans, "以下 references 未被对应 SKILL.md 索引：\n" + "\n".join(orphans)


# ---------------------------------------------------------------------------
# 不变量 2c：references 下的 JSON 样例必须是合法 JSON 且形态自洽
# ---------------------------------------------------------------------------
#
# criteria-parser 的 schema_example.json 曾长期是**非法 JSON**（`"OR（"或"整体保留）"`
# 内层引号未转义，json.load 直接抛错），没有任何测试发现——样例文件是给子代理抄的，
# 坏样例会被原样抄进产物。

EXPECTED_JSON_EXAMPLES: tuple[str, ...] = (
    "criteria-parser",
    "eligibility-judgment",
)


@pytest.mark.parametrize("skill", EXPECTED_JSON_EXAMPLES)
def test_schema_example_is_valid_json(skill: str):
    path = SKILLS_ROOT / skill / "references" / "schema_example.json"
    if not path.exists():
        pytest.fail(f"缺少 {path.relative_to(REPO_ROOT)}（结构样例应作为 references 提供）")
    json.loads(_read(path))  # 非法即抛 JSONDecodeError


@pytest.mark.parametrize("skill", EXPECTED_JSON_EXAMPLES)
def test_schema_example_is_indexed_by_skill_md(skill: str):
    md = _skill_md(skill)
    assert "schema_example.json" in _read(md), f"{skill}/SKILL.md 未索引 references/schema_example.json"


def test_judgment_schema_example_evidence_is_always_an_array():
    """样例里每条 evidence 都必须是对象数组 —— 样例本身就是子代理的形态参照物。

    故障 thread `dfbb4554`：IN 轨 26 条 evidence 被写成对象，报告证据栏静默变「—」。
    """
    path = SKILLS_ROOT / "eligibility-judgment" / "references" / "schema_example.json"
    if not path.exists():
        pytest.skip("eligibility-judgment 技能未安装")
    data = json.loads(_read(path))
    offenders: list[str] = []
    for doc_key, doc in (data.get("documents") or {}).items():
        for cid, entry in (doc.get("judgments") or {}).items():
            if "evidence" not in entry:
                continue
            ev = entry["evidence"]
            if not isinstance(ev, list) or any(not isinstance(x, dict) for x in ev):
                offenders.append(f"{doc_key}/{cid}")
    assert not offenders, f"样例中这些条目的 evidence 不是对象数组：{offenders}"


def test_criteria_schema_example_marks_non_screening_reference_events():
    """时间窗条件的 `参考事件` 必须照标准原文标（而不是统一压成「筛选」）。

    样例是解析子代理照抄的形态参照物：里面必须真的出现一条非「筛选」基准的日期维度，
    否则子代理没有可参照的正例，会把「签署知情同意书前 6 个月内」这类条件的基准压成筛选日。
    ICF 签署日与筛选日在真实病历里可能相差数周到数月，基准错位后判定阶段拿不到方案原文、
    无法复核——错误会一路带到交付。
    """
    path = SKILLS_ROOT / "criteria-parser" / "references" / "schema_example.json"
    if not path.exists():
        pytest.skip("criteria-parser 技能未安装")
    data = json.loads(_read(path))
    events: list[str] = []
    defaults: list[str] = []
    for items in (data.get("四分类") or {}).values():
        # 类目是以 条件ID 为键的对象（旧形态是数组，一并兼容）
        entries = list(items.values()) if isinstance(items, dict) else (items or [])
        for item in entries:
            dim = item.get("日期维度")
            if not isinstance(dim, dict):
                continue
            if dim.get("参考事件"):
                events.append(str(dim["参考事件"]))
            ref = dim.get("参考日期")
            if isinstance(ref, dict) and ref.get("默认值"):
                defaults.append(str(ref["默认值"]))
    assert events, "样例里没有任何带 `参考事件` 的日期维度"
    assert any("知情同意" in e for e in events), f"样例缺少「知情同意书签署」为参考事件的正例（现有：{sorted(set(events))}）"
    assert defaults and all("当天" in d for d in defaults), f"`参考日期.默认值` 必须是判定当天兜底（现有：{sorted(set(defaults))}）"


def test_judgment_schema_example_ex_direction_pairs():
    """样例里排除项的 exclusion_triggered 必须与 conclusion 配对（闸4 口径）。"""
    path = SKILLS_ROOT / "eligibility-judgment" / "references" / "schema_example.json"
    if not path.exists():
        pytest.skip("eligibility-judgment 技能未安装")
    data = json.loads(_read(path))
    expected = {"符合": False, "不符合": True}
    offenders: list[str] = []
    for doc in (data.get("documents") or {}).values():
        for cid, entry in (doc.get("judgments") or {}).items():
            if not cid.startswith("EX-"):
                continue
            concl = entry.get("conclusion")
            trig = entry.get("exclusion_triggered")
            if concl in expected:
                if trig is not expected[concl]:
                    offenders.append(f"{cid}: conclusion={concl} 应配 {expected[concl]}，实为 {trig}")
            elif trig is not None:
                offenders.append(f"{cid}: conclusion={concl} 不应带 exclusion_triggered")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# 不变量 3：证据链不丢（禁止重构时简化掉硬规则与故障记录）
# ---------------------------------------------------------------------------

# 每条证据必须仍存在于语料中的某处（SOUL 或任一 SKILL/references）
EVIDENCE_MUST_SURVIVE: tuple[str, ...] = (
    # 历史故障 thread 编号
    "ab76d625",  # ask_clarification 出现 0 次，伪造用户确认
    "459951c1",  # 用普通文本提问，TodoMiddleware 注入后模型自行继续
    "03a496cc",  # ask_clarification 与其他 tool_call 同轮 → goto=END 失效 → 用户看到 3 遍选择
    "d1ce04c0",  # 反复读 0 字节文件卡死
    "31c168d2",  # 单次全量 write_file 触发子代理看门狗超时
    "5a1c8d95",  # QC 未收敛即切分 + 主代理自我放行 QC 结论
    "5d987e97",  # QC 后修订用单次全量 write_file 重写 → EX-7 实体条目静默丢失
    "345f2bf4",  # ① P2 QC 原地打转空转 3 轮 ② 两轨自创 document 键 → 合并成假文档
    "ec37dc7d",  # present 了不存在的 outputs 文件，工具不校验存在性 → 静默吞掉
    "ec24d087",  # \f 导致 grep -n 与 read_file(splitlines) 行号错位 → 静默丢 9 条排除标准
    "6e5ac7c1",  # 段行号(方案坐标)用于读 raw.md → 越界静默空串 → 子代理凭空编造 92% 条目
    "69612125",  # 覆盖率分母只算 scanned 页 → 11 页文本层证据静默丢失却报覆盖完整
    "afb85bcd",  # upstream 项「不动」→ 下轮升级 blocking 振荡；两轨共享暂停；结构收敛吃语义轮次
    "9a83ccc9",  # 委派 prompt 转述时漏掉结构闸 → 子代理自创 schema 且两条闸真空通过 → 主代理转码修复反而清零数据 + 撞循环保护
    # 硬规则措辞
    "超发会被静默丢弃",
    "禁止手写 HTML 报告",
    "解析去重铁律",
    "符合=符合入组",
    "禁止伪",  # 禁止伪"无法判断"
    "严禁 bash 脚本做语义修订",
    "带建议放行",
    "blocked_round_limit",
    "force-qc-unconverged",
    "必须单独占一轮",  # ask_clarification 不得与其他 tool_call 同轮
    "禁止单次全量",  # 解析阶段：数万 token 单轮生成触发看门狗
    "`write_file` 一律禁止",  # 修订阶段：只允许 str_replace
    "不得用 `quality-control`",  # 修订子代理选型：该子代理没有 str_replace
    "条数守恒",  # 修订后必须按 blocking_issues 记账
    "无操作改判",  # P4 闸8：QC 点名却三字段全未动
    "连带误伤",  # P4 闸8：QC 未点名却被改了结论
    "splitlines",  # P1 行号坐标系：read_file 与 grep -n 不同源
    "循环论证",  # P1 自检：源末条号不得取自自己的提取结果
    "raw段行号",  # P2 双轨解析：读 raw.md 必须用 raw.md 自己的坐标
    "凭空生成",  # P2 闸9：原文须在 raw.md 中逐字可查
    "collect_text_pages",  # P2 OCR：文本层页必须归集进 ocr/
    "不需要进证据库",  # 覆盖率分母口径：不需要 OCR ≠ 不需要进证据库
    "中性化",  # 闸10：upstream_issues 点名条目本轮必须降级，不是放着不动
    "只计语义 QC",  # 轮次口径：结构闸修复不占轮次
    "按轨独立",  # IN/EX 轮次额度不共享
    "upstream_issues",  # QC 第三档：原文缺陷、结构化层无解
    "原地打转",  # 结构闸：本轮阻断项与上一轮完全一致
    "假文档",  # merge-judgments 跨分片 document 键闸
    "禁止转述",  # 判定委派模板必须原样复制（漏一条闸命令就会产出自创 schema 的判定文件）
    "回派重判",  # 结构闸不过的唯一处置
    "转码修复",  # ⛔ 禁止把畸形判定产物转码成合规形态（猜字段名 + 脚本不幂等 + 烧轮次）
    "真空通过",  # 两条机械闸读不到条目时不得报「全过」
    "5aa5d6d6",  # 取证包段定位静默失败 → QC 假阳性；主代理把「修脚本」与「派修订」串成一条链
    "假阳性",  # 闸/装配脚本缺陷会让 QC 报出假阳性阻断项，修订 prompt 必须点名不要改
    "没有依赖",  # 修脚本与派修订是两件并发的事，⛔ 不得串成一条链
    # 关键约束数值
    "queue",  # 占位：见下方 test_concurrency_budget_is_documented
)


@pytest.mark.parametrize("evidence", [e for e in EVIDENCE_MUST_SURVIVE if e != "queue"])
def test_evidence_survives_somewhere_in_corpus(evidence: str):
    holders = [str(p.relative_to(REPO_ROOT)) for p, text in CORPUS.items() if evidence in text]
    assert holders, f"证据链丢失：{evidence!r} 在 SOUL 与所有 skill 文档中均已不存在（禁止重构时简化）"


def test_concurrency_budget_is_documented():
    """`task` 每批 3 个 + 超发静默丢弃，是编排层最容易踩的坑，必须留在 SOUL。"""
    assert "每批" in SOUL and "3" in SOUL, "SOUL 未记载 task 每批并发预算"
    assert "SubagentLimitMiddleware" in SOUL, "SOUL 未记载超发被 SubagentLimitMiddleware 静默丢弃的机制"


# ---------------------------------------------------------------------------
# 双轨改造的产物契约（Task 2 / Task 8）
# ---------------------------------------------------------------------------

DUAL_TRACK_ARTIFACTS: tuple[str, ...] = (
    "criteria_parsed_IN.json",
    "criteria_parsed_EX.json",
    "criteria_qc_IN.json",
    "criteria_qc_EX.json",
    "criteria_meta.json",
    "criteria_judge_IN.json",
    "criteria_judge_EX.json",
)


@pytest.mark.parametrize("artifact", DUAL_TRACK_ARTIFACTS)
def test_soul_declares_dual_track_artifact(artifact: str):
    assert artifact in SOUL, f"SOUL 的目录规范/流程未声明双轨产物 {artifact}"


def test_soul_declares_dual_track_phases_and_gates():
    for marker, why in (
        ("Phase 4.5", "合并汇总阶段（承接原 P3 3.3/3.4 与 P4 回填终检）"),
        ("assemble", "两轨合并成全量 criteria_parsed.json 的闸门命令"),
        ("slim", "按轨切分判定输入包的闸门命令"),
    ):
        assert marker in SOUL, f"SOUL 未声明 {marker}：{why}"


def test_split_subcommand_is_retired():
    """`judge_pack.py split` 退役后，SOUL 不得再引用它（应改用 slim + assemble）。"""
    assert "judge_pack.py split" not in SOUL, "SOUL 仍在调用已退役的 judge_pack.py split"


# ---------------------------------------------------------------------------
# 资源上限失败不得盲目重派（Phase 1 / Task 6）
# ---------------------------------------------------------------------------
#
# 代码层已把 `stop_reason ∈ {recursion_limit, token_budget}` 的失败改为默认不重试
# （`task_tool._is_retryable_failure`），但主代理拿到 `Task failed` 之后**怎么做**是编排
# 规则的事：必须先读产物、只补跑未完成的条目，而不是整轨重来。
# 故障：会话 `d393714d` 6.36M 失败 + 5.21M 重试 = 11.57M，两次撞同一个 recursion_limit。

JUDGE_DELEGATION = SKILLS_ROOT / "eligibility-judgment" / "references" / "judge-delegation.md"
FAILURE_ARCHIVE = SKILLS_ROOT / "eligibility-judgment" / "references" / "failure-archive.md"


def test_soul_forbids_blind_redispatch_on_resource_ceiling():
    assert "Stop reason" in SOUL, "SOUL 未提到 task 回报里的 Stop reason，主代理无从识别额度耗尽"
    assert "recursion_limit" in SOUL and "token_budget" in SOUL, "SOUL 未点名两种资源上限"
    assert "定向补跑" in SOUL or "只把未完成" in SOUL, "SOUL 未给出「定向补跑」这一替代动作"


def test_judge_delegation_documents_the_three_step_handling():
    text = _read(JUDGE_DELEGATION)
    assert "禁止盲目重派" in text, "judge-delegation 未声明禁止盲目重派"
    for marker, why in (
        ("先读产物", "第 1 步：读初稿与门禁产物确认卡点"),
        ("只补跑", "第 2 步：仅重派还能推进的条目"),
        ("gate_escalated", "第 3 步：卡点无法自证时标记并转人工"),
    ):
        assert marker in text, f"judge-delegation 缺少 {marker}：{why}"


def test_failure_archive_keeps_the_evidence_chain():
    """证据链不丢：会话号与两次消耗必须留在语料里，否则规则会在下次重构中被「顺手简化」。"""
    text = _read(FAILURE_ARCHIVE)
    assert "d393714d" in text, "故障档案未记录会话号"
    assert "6.36M" in text and "5.21M" in text, "故障档案未记录两次失败的真实消耗"
    assert "retry_resource_ceiling_failures" in text, "故障档案未记录恢复旧行为的配置开关"


# ---------------------------------------------------------------------------
# 改判只允许对象级编辑（Phase 2 / Task 11）
# ---------------------------------------------------------------------------
#
# 字符串替换保证不了「同一条目多字段一起改」，于是形成
# 「改 reason → 漏改 conclusion → 门禁抓出 → 再补一处」的修补链。规则改为对象级
# `apply_json_patches`（pointer + op），但 `write_file` 的原始禁令必须原样保留 ——
# 它防的是另一回事（LLM 凭记忆重写导致条目静默消失，thread `5d987e97`）。

JUDGMENT_SKILL = _skill_md("eligibility-judgment")
CRITERIA_SKILL = _skill_md("criteria-parser")
JUDGMENT_REPAIR = SKILLS_ROOT / "eligibility-judgment" / "references" / "judgment-repair.md"
CRITERIA_REPAIR = SKILLS_ROOT / "criteria-parser" / "references" / "criteria-repair.md"

REPAIR_RULE_DOCS = (JUDGMENT_SKILL, CRITERIA_SKILL, JUDGMENT_REPAIR, CRITERIA_REPAIR)


@pytest.mark.parametrize("path", REPAIR_RULE_DOCS, ids=lambda p: p.name)
def test_repair_rules_require_object_level_edits(path):
    text = _read(path)
    assert "apply_json_patches" in text, f"{path.name} 未把改判/修订工具改为 apply_json_patches"
    assert "pointer" in text, f"{path.name} 未说明 JSON Pointer 定位方式"


@pytest.mark.parametrize("path", REPAIR_RULE_DOCS, ids=lambda p: p.name)
def test_repair_rules_no_longer_allow_str_replace_as_the_only_tool(path):
    """不得再出现「只允许 str_replace」这类把字符串替换钉成唯一写入方式的措辞。"""
    text = _read(path)
    for forbidden in ("只允许 `str_replace`", "只允许 str_replace"):
        assert forbidden not in text, f"{path.name} 仍规定「{forbidden}」"


@pytest.mark.parametrize("path", REPAIR_RULE_DOCS, ids=lambda p: p.name)
def test_write_file_ban_is_preserved(path):
    """⛔ 换手段不等于放松禁令：`write_file` 仍必须被禁。"""
    text = _read(path)
    assert "`write_file`" in text and "禁止" in text, f"{path.name} 丢掉了 write_file 禁令"


def test_remove_op_is_scoped_to_qc_named_entries():
    """`remove` 不受限就是绕开 write_file 禁令的后门。"""
    for path in (JUDGMENT_SKILL, CRITERIA_SKILL, JUDGMENT_REPAIR, CRITERIA_REPAIR):
        text = _read(path)
        if "remove" in text:
            assert "点名" in text, f"{path.name} 未限制 remove 只能作用于 QC 点名的条目"


def test_str_replace_remains_the_escape_hatch_for_broken_json():
    """语法坏掉的 JSON 解析不出来，对象级编辑必然拒绝——这条逃生阀必须写明。"""
    text = _read(JUDGMENT_REPAIR)
    assert "JSON 语法错误" in text
    assert "str_replace" in text, "修语法的唯一手段仍是 str_replace，不能连它一起删掉"


# ---------------------------------------------------------------------------
# 禁止用 bash 内联脚本生成结构化产物（Phase 4 / Task 16）
# ---------------------------------------------------------------------------
#
# `python3 -c` / heredoc / `echo >` 写 `.json` 会同时绕开三样东西：read-before-write 版本闸、
# `apply_json_patches` 的原子性、以及工具调用审计轨。半路抛异常还会留下截断的 JSON，
# 下一步就变成"先修语法再干活"。


def _skill_corpus(skill: str) -> str:
    """技能全集（SKILL.md + references/*.md）。

    禁令既约束**解析/判定子代理**（规则住 references）又约束主代理（编排住 SKILL.md），
    所以按技能全集校验。只查 SKILL.md 会在规则合法搬家后误报。
    """
    root = SKILLS_ROOT / skill
    parts = [_read(root / "SKILL.md")] if (root / "SKILL.md").exists() else []
    refs = root / "references"
    if refs.is_dir():
        parts += [_read(p) for p in sorted(refs.glob("*.md"))]
    return "\n".join(parts)


@pytest.mark.parametrize("skill", ("eligibility-judgment", "criteria-parser"))
def test_skills_ban_inline_scripts_for_json_artifacts(skill):
    text = _skill_corpus(skill)
    assert "python3 -c" in text, f"{skill} 未点名 python3 -c"
    assert "heredoc" in text, f"{skill} 未点名 heredoc"
    assert "echo >" in text, f"{skill} 未点名 echo 重定向"


@pytest.mark.parametrize("path", (JUDGMENT_SKILL, CRITERIA_SKILL), ids=lambda p: p.parent.name)
def test_inline_script_ban_still_allows_bash_for_gate_scripts(path):
    """⛔ 禁的是"用 bash 生产产物"，不是禁 bash —— 闸脚本必须继续用它跑。"""
    text = _read(path)
    assert "闸" in text and "python3 /mnt/skills" in text, f"{path.parent.name} 不应把跑闸脚本一起禁掉"


@pytest.mark.parametrize(
    "path",
    (
        SKILLS_ROOT / "eligibility-judgment" / "references" / "failure-archive.md",
        SKILLS_ROOT / "criteria-parser" / "references" / "failure-archive.md",
    ),
    ids=lambda p: p.parent.parent.name,
)
def test_failure_archive_records_the_inline_script_failure(path):
    text = _read(path)
    assert "内联脚本生成 JSON" in text
    for reason in ("read-before-write", "原子性", "审计"):
        assert reason in text, f"{path.parent.parent.name} 的故障档案缺少理由「{reason}」"


# ---------------------------------------------------------------------------
# 取证素材包必须在派 QC 之前装配（会话 `93d8a2c6` 复盘）
# ---------------------------------------------------------------------------
#
# 成本模型：计费 input ≈ (AI 步数 / 2) × 累积内容量。QC 逐条 grep+read 取证会把核验拆成
# 几十步，每步重传全部历史（实测 18×~30×）。治的是**步数**，不是每次读多少行。

QC_DELEGATION = SKILLS_ROOT / "eligibility-judgment" / "references" / "qc-delegation.md"
EVIDENCE_BUNDLE = SKILLS_ROOT / "eligibility-judgment" / "scripts" / "evidence_bundle.py"


def test_evidence_bundle_script_exists():
    assert EVIDENCE_BUNDLE.exists(), "规则引用的脚本必须真实存在，否则 QC 会卡在找不到文件"


def test_skill_mandates_bundle_before_dispatching_qc():
    text = _read(JUDGMENT_SKILL)
    assert "evidence_bundle.py" in text
    assert "不装配就不许派 QC" in text, "缺少硬性前置，装配就会被跳过"


def test_qc_delegation_reads_the_bundle_first_and_bans_per_item_grep():
    text = _read(QC_DELEGATION)
    assert "evidence_bundle_{id}_{SHARD}.md" in text, "QC 输入清单里必须有证据包"
    assert "取证的默认入口" in text
    assert "禁止" in text and "逐条" in text and "grep" in text, "必须硬禁逐条 grep 取证"
    assert "定点补读" in text, "必须给出证据包不足时的正确做法（按行号补读），否则会退回整篇重读"


def test_qc_checklist_has_the_quote_traceability_item():
    """引文可溯源是**确定性**判断，必须由证据包给结论、QC 只读表，不再自己复核。"""
    text = _read(QC_DELEGATION)
    assert "引文可溯源" in text
    assert "不要自己 grep" in text


CRITERIA_QC_CHECKLIST = SKILLS_ROOT / "criteria-parser" / "references" / "criteria-qc-checklist.md"


def test_criteria_qc_second_tier_must_report_a_specific_subtype():
    """第二档禁笼统 `转化条件不可执行`：会话 881e7ba8 两轮 11 条全报它，修订只改措辞
    （digest 全变而 运算符=不限/阈值=null 不动），3 轮不收敛。type 必须是四子类型之一
    且 action 指向具体操作与 parsing-rules 的拆分规则。"""
    text = _read(CRITERIA_QC_CHECKLIST)
    assert "禁止报笼统" in text, "第二档禁令缺失"
    for subtype in ("拆分不充分", "与或逻辑错误", "证据定位缺失", "字段不可执行"):
        assert subtype in text, f"第二档子类型 {subtype} 缺失"
    assert "parsing-rules.md` §拆分原则" in text, "拆分不充分的 action 必须指向拆分规则"
    assert '"type": "拆分不充分"' in text, "报告样例必须演示子类型 + 具体 action"


def test_criteria_repair_maps_split_insufficient_to_split_rules():
    """修订手册必须把「拆分不充分」映射到 parsing-rules 拆分规则，并写明改措辞不构成修复
    （881e7ba8 的拉锯形态），否则修订方仍会把结构问题当文字问题处理。"""
    text = _read(CRITERIA_REPAIR)
    assert "拆分不充分" in text, "处置手册缺「拆分不充分」行"
    assert "改写文字不构成修复" in text
    assert "parsing-rules.md` §拆分原则" in text, "拆分修复必须指向拆分规则而非自由发挥"


# ---------------------------------------------------------------------------
# 规则必须到达它的执行者（thread `e3c15416`）
# ---------------------------------------------------------------------------
#
# `skills=[]` 的子代理只读两样东西：委派模板（原样复制进 prompt）与模板里给出绝对路径的文件。
# 写在 SKILL.md 里的规则对它**不存在** —— 串轨与绕过版本闸就是这么发生的。

JUDGMENT_PRINCIPLES_REF = SKILLS_ROOT / "eligibility-judgment" / "references" / "judgment-principles.md"


def test_inline_script_ban_reaches_the_judging_subagent():
    """禁令必须在子代理读得到的文件里，不能只在 SKILL.md。"""
    body = _read(JUDGMENT_PRINCIPLES_REF)
    assert "python3 -c" in body, "内联脚本禁令没进判定规则文件，子代理读不到"
    assert "heredoc" in body and "echo >" in body


def test_track_boundary_is_a_hard_rule_in_the_principles():
    body = _read(JUDGMENT_PRINCIPLES_REF)
    assert "本轨边界" in body
    assert "禁止读写另一轨" in body, "必须明确禁止碰对侧产物，不能只说「只做本轨」"
    assert "不构成理由" in body, "「看到对侧文件存在」必须被明确排除为理由"


def test_version_gate_rejection_must_not_be_bypassed_with_bash():
    """被 read-before-write 拒绝后绕道 bash 是实测发生过的动作，必须点名禁止。"""
    body = _read(JUDGMENT_PRINCIPLES_REF)
    assert "版本闸" in body and "绕过" in body


def test_readonly_inspection_has_a_sanctioned_alternative():
    """只禁不给替代，agent 就会继续现写 python。必须点名 op:get。"""
    body = _read(JUDGMENT_PRINCIPLES_REF)
    assert '"op": "get"' in body or "op\": \"get" in body or "`get`" in body


@pytest.mark.parametrize(
    "ref", ("judge-delegation.md", "qc-delegation.md"), ids=lambda r: r.split("-")[0]
)
def test_delegation_templates_carry_the_track_boundary_as_a_hard_rule(ref):
    """模板是原样复制进 prompt 的，所以边界必须写在模板正文里且带 ⛔。"""
    body = _read(SKILLS_ROOT / "eligibility-judgment" / "references" / ref)
    assert "本轨边界" in body, f"{ref} 模板正文缺本轨边界"
    assert "⛔" in body
    assert "不涉及另一半。" not in body, f"{ref} 仍保留了没有约束力的软措辞"


CRITERIA_QC_CHECKLIST = SKILLS_ROOT / "criteria-parser" / "references" / "criteria-qc-checklist.md"
CRITERIA_QC_BUNDLE = SKILLS_ROOT / "criteria-parser" / "scripts" / "criteria_qc_bundle.py"


def test_criteria_qc_bundle_script_exists():
    assert CRITERIA_QC_BUNDLE.exists(), "规则引用的脚本必须真实存在"


def test_criteria_skill_mandates_bundle_before_dispatching_qc():
    text = _read(CRITERIA_SKILL)
    assert "criteria_qc_bundle.py" in text
    assert "不装配不许派 QC" in text, "缺硬前置，装配就会被跳过"
    # 会话 `c2518bc7` 证明「装配」不足以构成前置：两轨取证包都装配成功了，但 6 个 QC 子代理
    # 一次都没读到 —— 委派 prompt 的输入清单是白名单，漏写等于禁止读。硬前置必须同时覆盖交接。
    assert "照抄模板的输入清单" in text, "只要求装配、不要求照抄模板输入清单，装配收益会归零"


def test_criteria_qc_checklist_bans_per_item_evidence_gathering():
    text = _read(CRITERIA_QC_CHECKLIST)
    assert "criteria_qc_bundle_{TRACK}.md" in text, "QC 侧必须知道取证包在哪"
    assert "取证的默认入口" in text
    assert "禁止逐条" in text and "grep" in text
    assert "python3 -c" in text, "必须点名禁止内联 python 只读自检"
    assert "定点" in text, "必须给出取证包不足时的正确做法，否则会退回整篇重读"


def test_criteria_failure_archive_records_the_step_count_failure():
    text = _read(SKILLS_ROOT / "criteria-parser" / "references" / "failure-archive.md")
    assert "解析 QC 逐条取证耗尽步数额度" in text
    for datum in ("77 步", "max_turns=150", "26 处"):
        assert datum in text, f"缺实测数据「{datum}」"


def test_failure_archive_records_the_unreachable_rule_failure():
    text = _read(FAILURE_ARCHIVE)
    assert "规则写在子代理读不到的地方" in text
    assert "skills=[]" in text or "`skills=[]`" in text
    assert "read-before-write" in text or "版本闸" in text


def test_failure_archive_records_the_step_count_failure():
    text = _read(FAILURE_ARCHIVE)
    assert "QC 逐条取证耗尽步数额度" in text
    for datum in ("1.97M", "98.9%", "18 遍", "max_turns=150"):
        assert datum in text, f"故障档案缺少实测数据「{datum}」"
    assert "反向优化" in text, "必须写明「把 grep 窗口开大」是反向优化，否则下次还会这么试"


# ---------------------------------------------------------------------------
# expected_hash 必须可获取（thread `93d8a2c6` seq 1220-1224）
# ---------------------------------------------------------------------------
#
# 手册原先写「改判前已 read_file，直接算」——`read_file` 不回报哈希，LLM 也算不出 sha256，
# 于是子代理编了一个 `d90a6bb44b04`（整个 run 从未出现过）→ 被拒 → 再花一次 sha256sum 去算
# 报错已经给出的值 → 才成功。该会话 13 次带 expected_hash 的调用里 2 次被拒。

REPAIR_PLAYBOOKS = (JUDGMENT_REPAIR, CRITERIA_REPAIR)


@pytest.mark.parametrize("path", REPAIR_PLAYBOOKS, ids=lambda p: p.name)
def test_repair_playbook_gives_the_command_to_obtain_the_hash(path):
    text = _read(path)
    assert "sha256sum" in text, f"{path.name} 未给出取 expected_hash 的具体命令"
    assert "cut -c1-12" in text, f"{path.name} 未说明只取前 12 位"


@pytest.mark.parametrize("path", REPAIR_PLAYBOOKS, ids=lambda p: p.name)
def test_repair_playbook_bans_fabricating_the_hash(path):
    text = _read(path)
    assert "不回报哈希" in text, f"{path.name} 必须说明 read_file 不给哈希"
    assert "凭印象填" in text, f"{path.name} 必须明禁凭印象编造哈希"


@pytest.mark.parametrize("path", REPAIR_PLAYBOOKS, ids=lambda p: p.name)
def test_repair_playbook_no_longer_claims_the_hash_can_just_be_computed(path):
    """⛔ 不得再出现「直接算」这类不可执行的指令。"""
    assert "直接算" not in _read(path), f"{path.name} 仍保留了不可执行的「直接算」指令"


@pytest.mark.parametrize("path", REPAIR_PLAYBOOKS, ids=lambda p: p.name)
def test_repair_playbook_explains_the_cheap_recovery_path(path):
    """pointer 形态可以直接拿报错里的哈希重试——不写清楚就会又去跑一次 sha256sum。"""
    text = _read(path)
    assert "原样重试" in text, f"{path.name} 未给出「用报错里的哈希直接重试」这条恢复路径"
    assert "不依赖" in text, f"{path.name} 未写明该恢复路径的前提（值不依赖刚读到的内容）"


def test_failure_archive_records_the_string_replacement_failure():
    text = _read(FAILURE_ARCHIVE)
    assert "改判用字符串替换漏改字段" in text, "故障档案缺少本次改动的故障条目"
    assert "67" in text and "161 步" in text, "故障档案未记录实测数据（67 次 str_replace / 161 步）"


# ── 发现工具/脚本缺陷时的处置（会话 `5aa5d6d6`）──────────────────────────────
#
# SOUL 写满了「修订一律委派子代理」，却从没写过「遇到闸/装配脚本缺陷时该怎么办」。
# 于是主代理按「谁发现谁负责」的默认直觉，把「修脚本」与「派修订」串成一条链：
# 花 10 分钟修脚本（两次全量重写，第二次只改 4 个字符），13 条真阻断项一条未修。
# 它修脚本本身是对的（`skill_manage` 是写 /mnt/skills 的唯一通道，子代理拿不到），
# 错的是没有并发。


def test_soul_tells_the_lead_what_to_do_about_a_buggy_gate_script():
    """规则缺失才是根因 —— 这条必须在 SOUL（该派谁做属于派发决策）。"""
    assert "发现工具/脚本缺陷时" in SOUL, "SOUL 缺「闸/装配脚本有缺陷时怎么办」这条规则"
    assert "没有依赖" in SOUL and "并发" in SOUL, "必须写明修脚本与派修订是两件并发的事"
    assert "假阳性" in SOUL, "必须点明脚本缺陷会让 QC 报出假阳性阻断项"


def test_soul_keeps_revision_delegated_even_while_fixing_a_script():
    """最大的破口：因为「已经在动手了」就顺势自己改产物。"""
    section = SOUL.split("发现工具/脚本缺陷时", 1)[1][:1200]
    assert "派修订" in section, "未说明仍要照常派修订子代理"
    assert "criteria_parsed" in section, "未点名禁止主代理顺势自己改标准产物"


def test_soul_points_at_patch_not_full_rewrite_for_script_fixes():
    """一行修复走 write_file 全量覆盖 = 重付一遍全文 token（实测付了两遍）。"""
    section = SOUL.split("发现工具/脚本缺陷时", 1)[1][:1200]
    assert 'action="patch"' in section, "未指向 skill_manage 的 patch（小改动的正确工具）"
    assert "write_file" in section, "未点明不要用 write_file 全量覆盖"


def test_soul_gives_a_criterion_for_spotting_a_false_positive():
    """判据要机械可用：结构闸已过而 QC 报原文不符 → 错的是 QC 读到的素材。"""
    section = SOUL.split("发现工具/脚本缺陷时", 1)[1][:1200]
    assert "闸 9" in section or "闸9" in section, "未给出「结构闸已过」这个可机械判断的判据"
    assert "素材" in section, "未说明错的是素材而不是产物"


def test_phase2_qc_loop_also_carries_the_concurrent_handling():
    """规则要长在流程里，不只长在原则清单里 —— 主代理在 QC 循环那一步才需要它。"""
    # 2026-08-26 重构:循环五步细节下沉 parse-orchestration.md,SOUL 锚点改为调度句
    loop = SOUL.split("QC↔修订循环推进该轨", 1)[1][:600]
    assert "并发" in loop, "Phase 2 QC 循环未提示并发处理脚本缺陷"
    assert "patch" in loop, "Phase 2 QC 循环未给出修脚本的工具"


def test_criteria_failure_archive_records_the_bundle_mapping_failure():
    text = _read(SKILLS_ROOT / "criteria-parser" / "references" / "failure-archive.md")
    assert "取证包段定位静默失败" in text, "故障档案缺少本次故障条目"
    assert "5aa5d6d6" in text and "a7c19ea1" in text, "未记录该形态的两次发作"
    assert "exit 0" in text, "未点明「带着全错的映射 exit 0」这个关键表征"
    assert "并发" in text, "未记录「修脚本与派修订应并发」这条教训"


def test_bundle_script_blocks_instead_of_falling_back_silently():
    """根因修复：段定位失败必须 exit 2，不得静默退回整篇。"""
    text = _read(CRITERIA_QC_BUNDLE)
    assert "MappingUntrusted" in text, "段定位失败仍未升级为异常"
    assert "clause_spans_checked" in text, "缺少带可信度的入口函数"
    assert "(?!#)" in text, "end 探测仍会回溯命中 4 级标题"
    assert "(?:\\d+(?:\\.\\d+)*\\s*)?" in text, "轨段标题仍不认带编号前缀的写法"
