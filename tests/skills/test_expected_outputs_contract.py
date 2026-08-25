"""委派模板必须声明 `expected_outputs`(产物后置校验的接线契约)。

`expected_outputs` 是 `task` 工具的可选参数:声明了才校验。也就是说 harness 侧的护栏
(`SubagentExecutor._verify_expected_outputs`)只有在**委派模板真的传了参数**时才生效 ——
模板一旦被"顺手精简"掉这一行,护栏就静默失效,而所有测试仍然是绿的。

本文件就是那道机械保障。它必须存在于 `tests/`(受版本控制),因为语料本身不受:
`skills/custom` 与 `backend/.deer-flow/agents` 都是 gitignored 的本地目录。

被守护的故障:
* 会话 `88df83a8` —— EX 判定子代理写出自创的 `qc_review_report.json` 而不是
  `judgments_draft_MCRC-2150006_EX.json`,却以 `completed` 返回;主代理 8 分钟后才靠结构闸
  发现,重派时 run 已结束,整轨作废。
* 会话 `9a83ccc9`(记录在 `judge-delegation.md` 顶部)—— 主代理把委派模板压缩成自述版,
  闸命令整条消失。同一类漂移,同一类后果。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOUL_PATH = REPO_ROOT / "backend" / ".deer-flow" / "agents" / "eligibility-screener" / "SOUL.md"
SKILLS_ROOT = REPO_ROOT / "skills" / "custom"

# (模板路径, 该模板必须一并出现的产物路径片段)
# 产物片段刻意用带占位符的形态:模板里写的是 `{id}` / `{SHARD}` / `{TRACK}`,
# 断言它们同时在场,才能证明"声明的是这个任务真正的产物",而不是抄了一句空话。
DELEGATION_CONTRACTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "eligibility-judgment/references/judge-delegation.md",
        ("judgments_draft_{id}_{SHARD}.json",),
    ),
    (
        "eligibility-judgment/references/qc-delegation.md",
        ("qc_report_{id}_{SHARD}.json",),
    ),
    (
        "eligibility-judgment/references/judgment-repair.md",
        ("judgments_draft_{id}_{SHARD}.json",),
    ),
    (
        "criteria-parser/references/parse-delegation.md",
        ("criteria_parsed_IN.json", "criteria_qc_{TRACK}.json"),
    ),
)

if not SOUL_PATH.exists() or not SKILLS_ROOT.exists():
    pytest.skip(
        "eligibility-screener agent 或 skills/custom 未安装（本地 gitignored 目录）",
        allow_module_level=True,
    )


def _read(relative: str) -> str:
    path = SKILLS_ROOT / relative
    assert path.exists(), f"委派模板缺失: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("template", "artifacts"), DELEGATION_CONTRACTS, ids=[c[0] for c in DELEGATION_CONTRACTS])
def test_delegation_template_declares_expected_outputs(template: str, artifacts: tuple[str, ...]) -> None:
    text = _read(template)
    assert "expected_outputs" in text, (
        f"{template} 没有要求委派时传 expected_outputs。"
        "少了这一行,harness 的产物后置校验对该委派完全不生效(参数未声明 = 不校验)。"
    )
    for artifact in artifacts:
        assert artifact in text, f"{template} 提到了 expected_outputs 却没给出产物路径 {artifact}"


@pytest.mark.parametrize(("template", "_artifacts"), DELEGATION_CONTRACTS, ids=[c[0] for c in DELEGATION_CONTRACTS])
def test_declared_paths_live_under_user_data(template: str, _artifacts: tuple[str, ...]) -> None:
    """声明必须落在 sandbox 的 user-data 前缀下,否则工具边界会直接拒收整个调用。"""
    text = _read(template)
    for line in text.splitlines():
        if "expected_outputs=" not in line:
            continue
        assert "/mnt/user-data/" in line, f"{template} 的 expected_outputs 示例不是 /mnt/user-data/ 下的绝对路径: {line.strip()}"


def test_soul_phase3_requires_the_declaration() -> None:
    """编排层(SOUL)也必须写明这条纪律,否则主代理只在读到技能模板时才知道。"""
    soul = SOUL_PATH.read_text(encoding="utf-8")
    assert "expected_outputs" in soul, "SOUL.md 未要求判定委派携带 expected_outputs"
    assert "judgments_draft_{id}_{SHARD}.json" in soul


def test_failure_evidence_is_preserved() -> None:
    """证据链不丢:88df83a8 必须仍能在语料里找到,否则下一次重构会把这条护栏当冗余删掉。"""
    corpus = SOUL_PATH.read_text(encoding="utf-8")
    for template, _ in DELEGATION_CONTRACTS:
        corpus += _read(template)
    assert "88df83a8" in corpus, "会话 88df83a8(产物缺失却回报 completed)的故障编号已从语料中消失"


# --------------------------------------------------------------------------- #
# 结构来源唯一(会话 7512ebd2)                                                  #
# --------------------------------------------------------------------------- #
#
# 声明了 expected_outputs 并不足以拿到产物:7512ebd2 里 6 次判定尝试的声明全部正确,
# 失败在**子代理写错了结构、也写错了文件名**。它把输入包 `criteria_judge_*.json` 的
# 「四分类」形态当成输出模板(顶层键写成中文 `患者`/`轨`/`判定`),并存成自创路径
# `eligibility_judgment_IN_MCRC-2150006.json`;主代理清理时漏了 workspace 根下的残留,
# 下一轮重派的子代理读到它、又照抄了一遍。9.87M token / 76 分钟 / 零产物。
#
# 上下文压缩会把开局读到的 schema 删掉(harness 侧已修:空摘要不再净删除、dedup 不再
# 给悬空引用),但语料侧也必须写明「结构只有一个出处」,否则模型凭记忆时会去抄手边的
# 任何 JSON —— 而手边最像的那个,恰好就是输入包和上次的错误产物。


_JUDGE_DELEGATION = "eligibility-judgment/references/judge-delegation.md"


def test_template_forbids_treating_the_input_package_as_the_output_shape() -> None:
    text = _read(_JUDGE_DELEGATION)
    assert "不是输出模板" in text, "模板未写明「输入包不是输出模板」。7512ebd2 的 6 次尝试全部照着 criteria_judge 的 四分类 形态落盘。"
    assert "criteria_judge_{SHARD}.json" in text, "禁令必须点名输入包本身,否则读不出指的是哪个文件"


def test_template_forbids_copying_leftover_judgment_files() -> None:
    text = _read(_JUDGE_DELEGATION)
    assert "*judgment*.json" in text, "模板未禁止拿 workspace 下已有的 *judgment*.json 当结构参考"
    assert "错误产物" in text, "禁令必须说明理由(残留可能是上一次失败尝试的错误产物),否则会被当作冗余删掉"


def test_template_gives_a_mechanical_self_check_on_the_top_level_keys() -> None:
    """自检判据必须是**机械可判**的:「顶层键出现中文就是抄错了源」不依赖模型的自我评估。"""
    text = _read(_JUDGE_DELEGATION)
    assert "patient_id" in text and "judgment_date" in text and "judgments" in text
    assert "顶层键" in text, "模板未给出落盘前的顶层键自检"


def test_7512ebd2_evidence_is_preserved() -> None:
    assert "7512ebd2" in _read(_JUDGE_DELEGATION), "会话 7512ebd2 的故障编号已从语料中消失"
