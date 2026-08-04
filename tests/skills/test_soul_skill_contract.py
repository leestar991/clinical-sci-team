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
    """
    line_count = len(SOUL.splitlines())
    assert line_count <= 750, f"SOUL.md {line_count} 行，超出编排骨架上限（实测基准 733，上限 750）；新增内容请先确认不属于某个 skill"


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
    ),
    "pdf-image-extractor": ("ocr-delegation.md",),
    "patient-separator": ("aggregate-ocr.md",),
    "eligibility-judgment": (
        "judge-delegation.md",
        "qc-delegation.md",
        "reasons-delegation.md",
    ),
}


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
