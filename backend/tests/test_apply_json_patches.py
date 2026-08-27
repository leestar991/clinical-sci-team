"""``apply_json_patches`` 的 pointer 定位安全性 —— `criteria_parsed` 四分类 list→dict 的前提。

背景（thread `3a745b38`，见 `docs/plans/fix-criteria-parsing-json.md`）：
`四分类/{类目}` 原本是 list，pointer 只能用数字下标定位。一次 `add /21` 之后**跨调用**沿用
旧下标，24 笔单字段写入全部落到前一条条目上（乙肝阈值写进 CNS 转移条目），27 次调用全返回
`OK` 无一报错，两轮 QC 白烧，最终撞 `recursion_limit=420`。

治本手段是把容器改成以条件ID 为 key 的 dict。之所以敢直接改（而不做 list 过渡期的
`[条件ID=...]` 语法扩展），是因为 dict 上三个 op 里已经有两个是安全的"偏函数"——命中即命中、
不命中即报错。本文件把这个前提逐条锁死：

- `replace` / `remove` 指向不存在的 key → **必须拒绝**（现有行为，防回归）
- `replace` 指向已存在 key 的某个字段 → 应用且**兄弟字段完整保留**（dict 方案的核心保证）
- `add` 指向**已存在**的 key → **必须拒绝**。这是 dict 化唯一引入的新风险：原实现是
  `parent[last] = value` 无存在性检查，会把整条条目静默替换、丢掉所有未提及字段。它比下标
  漂移更难发现——下标漂移至少留下 `同义词`/`原文` 当指纹（事故取证正是靠这个），整条覆盖
  连指纹都没有。
- `add` 到 list（`/-` 追加、`/N` 插入）→ 行为不变，本次改动不得波及。
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from deerflow.sandbox import tools as sandbox_tools

apply_json_patches_tool = getattr(sandbox_tools, "apply_json_patches_tool", None)

pytestmark = pytest.mark.skipif(apply_json_patches_tool is None, reason="apply_json_patches 尚未实现")

PATH = "/mnt/user-data/workspace/criteria_parsed_EX.json"

CAT = "排除_可从病例获取"


def _entry(cid: str, *, 匹配字段: list[str], 阈值: object, 同义词: list[str]) -> dict:
    return {
        "条件ID": cid,
        "来源标准": f"排除标准 第{cid.split('-')[1]}条",
        "原文": f"{cid} 原文",
        "子条件": f"{cid} 子条件",
        "逻辑关系": "单条件",
        "可从病例获取": True,
        "转化条件": {"匹配字段": 匹配字段, "运算符": "in", "阈值": 阈值, "同义词": 同义词},
        "日期维度": None,
    }


DOC = json.dumps(
    {
        "四分类": {
            CAT: {
                "EX-12-1": _entry("EX-12-1", 匹配字段=["HBsAg", "HBV-DNA"], 阈值="乙肝阈值", 同义词=["乙肝", "HBV"]),
                "EX-12-2": _entry("EX-12-2", 匹配字段=["HCV抗体"], 阈值="丙肝阈值", 同义词=["丙肝", "HCV"]),
            },
            "排除_不可从病例获取": {},
        },
        "描述索引": {"EX-12": "病毒性肝炎/HIV/梅毒"},
    },
    ensure_ascii=False,
    indent=2,
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeSandbox:
    def __init__(self, content: str):
        self.content = content
        self.writes: list[str] = []

    @property
    def id(self) -> str:
        return "sandbox-test"

    def read_file(self, path: str) -> str:
        return self.content

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        self.content = content
        self.writes.append(content)


@pytest.fixture
def runtime() -> SimpleNamespace:
    return SimpleNamespace(state={}, context={"thread_id": "t1"}, config={})


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(sandbox_tools, "ensure_thread_directories_exist", lambda runtime: None)
    monkeypatch.setattr(sandbox_tools, "is_local_sandbox", lambda runtime: False)


def _run(monkeypatch, sandbox, runtime, patches, expected_hash=None) -> str:
    monkeypatch.setattr(sandbox_tools, "ensure_sandbox_initialized", lambda r: sandbox)
    if expected_hash is None:
        expected_hash = _hash(sandbox.content)
    return apply_json_patches_tool.func(runtime, "criteria revision", PATH, expected_hash, patches)


def _cat(sandbox: FakeSandbox) -> dict:
    return json.loads(sandbox.content)["四分类"][CAT]


# --- dict 上的 replace：命中即命中，兄弟字段不动 ------------------------------


def test_replace_field_keeps_sibling_fields(monkeypatch, runtime):
    """dict 方案的核心保证：改一个字段不会碰到同条目的其它字段。

    事故里 `匹配字段`/`阈值` 被换而 `同义词`/`原文` 原封不动，正是单字段 pointer 的指纹；
    这个行为本身是对的，必须守住。
    """
    sandbox = FakeSandbox(DOC)
    result = _run(
        monkeypatch,
        sandbox,
        runtime,
        [{"pointer": f"/四分类/{CAT}/EX-12-1/转化条件/阈值", "op": "replace", "value": "HBsAg阳性且HBV-DNA>10³IU/ml"}],
    )

    assert result.startswith("OK"), result
    entry = _cat(sandbox)["EX-12-1"]
    assert entry["转化条件"]["阈值"] == "HBsAg阳性且HBV-DNA>10³IU/ml"
    assert entry["转化条件"]["同义词"] == ["乙肝", "HBV"], "兄弟字段必须完整保留"
    assert entry["原文"] == "EX-12-1 原文"
    assert entry["转化条件"]["匹配字段"] == ["HBsAg", "HBV-DNA"]


def test_replace_missing_key_is_rejected(monkeypatch, runtime):
    """key 不命中必须报错——这是 dict 取代下标寻址的全部意义。"""
    sandbox = FakeSandbox(DOC)
    result = _run(
        monkeypatch,
        sandbox,
        runtime,
        [{"pointer": f"/四分类/{CAT}/EX-99-9/转化条件/阈值", "op": "replace", "value": "x"}],
    )

    assert result.startswith("Error:"), result
    assert "EX-99-9" in result
    assert sandbox.writes == [], "定位失败不得写入"
    assert sandbox.content == DOC


def test_remove_missing_key_is_rejected(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": f"/四分类/{CAT}/EX-99-9", "op": "remove"}])

    assert result.startswith("Error:"), result
    assert sandbox.writes == []


# --- dict 上的 add：只能新建 -------------------------------------------------


def test_add_new_key_succeeds(monkeypatch, runtime):
    """拆分出新子条件（如 EX-11-4）的正常路径。"""
    sandbox = FakeSandbox(DOC)
    new = _entry("EX-12-3", 匹配字段=["HIV抗体"], 阈值="阳性", 同义词=["HIV"])
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": f"/四分类/{CAT}/EX-12-3", "op": "add", "value": new}])

    assert result.startswith("OK"), result
    assert _cat(sandbox)["EX-12-3"]["转化条件"]["阈值"] == "阳性"
    assert set(_cat(sandbox)) == {"EX-12-1", "EX-12-2", "EX-12-3"}


def test_add_existing_key_is_rejected_and_writes_nothing(monkeypatch, runtime):
    """本次改动的核心：`add` 到已存在的 key 必须拒绝，否则整条被静默替换。"""
    sandbox = FakeSandbox(DOC)
    partial = {"条件ID": "EX-12-1", "转化条件": {"阈值": "新阈值"}}
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": f"/四分类/{CAT}/EX-12-1", "op": "add", "value": partial}])

    assert result.startswith("Error:"), result
    assert "EX-12-1" in result
    assert sandbox.writes == [], "被拒绝时不得写入"
    assert sandbox.content == DOC, "文件必须逐字不变"


def test_add_existing_key_error_points_at_replace(monkeypatch, runtime):
    """报错要一次给出可行改写，否则 agent 只能试错——恢复成本必须是一次调用。"""
    sandbox = FakeSandbox(DOC)
    result = _run(
        monkeypatch,
        sandbox,
        runtime,
        [{"pointer": f"/四分类/{CAT}/EX-12-1", "op": "add", "value": {"条件ID": "EX-12-1"}}],
    )

    assert "replace" in result, result
    assert "Nothing was written" in result


def test_add_existing_key_does_not_lose_fields(monkeypatch, runtime):
    """回归锁：曾经的实现是 `parent[last] = value`，会把 同义词/原文 一起吃掉。"""
    sandbox = FakeSandbox(DOC)
    _run(
        monkeypatch,
        sandbox,
        runtime,
        [{"pointer": f"/四分类/{CAT}/EX-12-1", "op": "add", "value": {"条件ID": "EX-12-1"}}],
    )
    entry = _cat(sandbox)["EX-12-1"]
    assert entry["转化条件"]["同义词"] == ["乙肝", "HBV"]
    assert entry["原文"] == "EX-12-1 原文"


# --- list 上的 add：行为不得改变 ---------------------------------------------


def test_add_to_list_append_unchanged(monkeypatch, runtime):
    """`/-` 追加是 RFC 6901 语义，本次改动只针对 dict，不得波及 list。"""
    doc = json.dumps({"conditions": [{"条件ID": "EX-1"}]}, ensure_ascii=False, indent=2)
    sandbox = FakeSandbox(doc)
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": "/conditions/-", "op": "add", "value": {"条件ID": "EX-2"}}])

    assert result.startswith("OK"), result
    assert [i["条件ID"] for i in json.loads(sandbox.content)["conditions"]] == ["EX-1", "EX-2"]


def test_add_to_list_insert_unchanged(monkeypatch, runtime):
    """数字下标插入仍然合法（且仍然会挤偏后续下标——这正是改用 dict 的原因）。"""
    doc = json.dumps({"conditions": [{"条件ID": "EX-1"}, {"条件ID": "EX-3"}]}, ensure_ascii=False, indent=2)
    sandbox = FakeSandbox(doc)
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": "/conditions/1", "op": "add", "value": {"条件ID": "EX-2"}}])

    assert result.startswith("OK"), result
    assert [i["条件ID"] for i in json.loads(sandbox.content)["conditions"]] == ["EX-1", "EX-2", "EX-3"]


# --- 批量原子性 -------------------------------------------------------------


def test_batch_is_atomic_when_one_patch_is_rejected(monkeypatch, runtime):
    """半应用的 criteria 文件比不改更危险：结构闸看不出「只改了一半」。"""
    sandbox = FakeSandbox(DOC)
    patches = [
        {"pointer": f"/四分类/{CAT}/EX-12-1/转化条件/阈值", "op": "replace", "value": "改得对"},
        {"pointer": f"/四分类/{CAT}/EX-12-2", "op": "add", "value": {"条件ID": "EX-12-2"}},
    ]
    result = _run(monkeypatch, sandbox, runtime, patches)

    assert result.startswith("Error:"), result
    assert sandbox.writes == []
    assert sandbox.content == DOC
    assert "改得对" not in sandbox.content, "第一条 patch 不得留下痕迹"


def test_batch_neutralization_applies_in_one_write(monkeypatch, runtime):
    """批量中性化：多条 `/{条件ID}/` patch 一次调用写完（把 ~100 superstep 压到 ~10）。"""
    sandbox = FakeSandbox(DOC)
    patches = [
        {"pointer": f"/四分类/{CAT}/EX-12-1/转化条件/运算符", "op": "replace", "value": "不限"},
        {"pointer": f"/四分类/{CAT}/EX-12-1/备注", "op": "add", "value": "待核实"},
        {"pointer": f"/四分类/{CAT}/EX-12-2/转化条件/运算符", "op": "replace", "value": "不限"},
    ]
    result = _run(monkeypatch, sandbox, runtime, patches)

    assert result.startswith("OK"), result
    assert len(sandbox.writes) == 1, "批量必须只写一次"
    assert _cat(sandbox)["EX-12-1"]["备注"] == "待核实"
    assert _cat(sandbox)["EX-12-2"]["转化条件"]["运算符"] == "不限"


# --- expected_hash 的指引必须按 pointer 形态分流 -----------------------------


def test_hash_mismatch_hint_for_dict_pointer_says_retry_same(monkeypatch, runtime):
    """纯 dict 路径的 pointer 不受他处增删影响，重试同样 patch 是有效恢复。"""
    sandbox = FakeSandbox(DOC)
    result = _run(
        monkeypatch,
        sandbox,
        runtime,
        [{"pointer": f"/四分类/{CAT}/EX-12-1/转化条件/阈值", "op": "replace", "value": "x"}],
        expected_hash=_hash("stale"),
    )

    assert result.startswith("Error:"), result
    assert "SAME" in result, "dict 路径应告知可重试同样 patch"
    assert sandbox.writes == []


def test_hash_mismatch_hint_for_numeric_index_says_reconfirm(monkeypatch, runtime):
    """含数字下标的 pointer 在该 list 长度变化后会指向别的条目，不能说"直接重试"。

    旧文案对所有 pointer 一律说 "unaffected by edits elsewhere ... retry the SAME patches"，
    对数组下标是错的（1.3.1）。
    """
    doc = json.dumps({"conditions": [{"条件ID": "EX-1"}]}, ensure_ascii=False, indent=2)
    sandbox = FakeSandbox(doc)
    result = _run(
        monkeypatch,
        sandbox,
        runtime,
        [{"pointer": "/conditions/0/条件ID", "op": "replace", "value": "EX-2"}],
        expected_hash=_hash("stale"),
    )

    assert result.startswith("Error:"), result
    assert "SAME" not in result, "数字下标不得建议直接重试同样 patch"
    assert "index" in result.lower()
    assert sandbox.writes == []


# --- get 只读语义（迁移期用于确认 key 是否存在） ------------------------------


def test_get_is_read_only(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": f"/四分类/{CAT}/EX-12-1/转化条件/阈值", "op": "get"}])

    assert "乙肝阈值" in result
    assert sandbox.writes == [], "只读批次不得写盘"
