"""``apply_json_patches`` —— 一次加锁、一次版本校验、一次写入的批量编辑
(criteria-token-saving-v1.2 Task 5)。

动机:改判一个患者一轨的判定 JSON 往往要改 5-15 处(每条 blocking_issue 一处)。
当前只能一处一次 ``str_replace``,于是形成
``read_file -> str_replace -> read_file -> str_replace -> …`` 的循环 ——
每次写都让 read-before-write 闸失效,必须重读整份文件,N 处改动 = N 次全文读 + N 次写。

本工具把这段压成:传入 N 个 patch + 一个 ``expected_hash``,校验一次、全应用、写一次。

不变量(测试逐条锁定):
- **原子性**:任一 patch 不适用 → 全部不写。半应用的判定文件比不改更危险,
  因为结构闸看不出「只改了一半」。
- **版本校验**:``expected_hash`` 与当前内容不符 → 拒绝。这是并发/陈旧读的唯一防线。
- **歧义拒绝**:与 ``str_replace`` 收紧后的语义一致 —— ``old_str`` 多处出现即拒绝,
  不猜该改哪一处。
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from deerflow.sandbox import tools as sandbox_tools

apply_json_patches_tool = getattr(sandbox_tools, "apply_json_patches_tool", None)

pytestmark = pytest.mark.skipif(apply_json_patches_tool is None, reason="apply_json_patches 尚未实现")

PATH = "/mnt/user-data/workspace/patients/M018/judgments_draft_M018_IN.json"

DOC = json.dumps(
    {
        "documents": {
            "rec": {
                "judgments": {
                    "IN-1": {"conclusion": "无法判断", "reason": "缺知情同意记录"},
                    "IN-2": {"conclusion": "符合", "reason": "年龄 62 岁"},
                    "IN-3": {"conclusion": "存疑", "reason": "病理分型未明"},
                }
            }
        }
    },
    ensure_ascii=False,
    indent=1,
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


def _run(monkeypatch, sandbox, runtime, patches, expected_hash) -> str:
    monkeypatch.setattr(sandbox_tools, "ensure_sandbox_initialized", lambda r: sandbox)
    return apply_json_patches_tool.func(runtime, "batch repair", PATH, expected_hash, patches)


# --- 主路径 -----------------------------------------------------------------


def test_applies_all_patches_in_one_write(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    patches = [
        {"old_str": '"缺知情同意记录"', "new_str": '"已签署知情同意书"'},
        {"old_str": '"病理分型未明"', "new_str": '"腺癌，已除外小细胞癌"'},
    ]
    result = _run(monkeypatch, sandbox, runtime, patches, _hash(DOC))

    assert result.startswith("OK"), result
    assert len(sandbox.writes) == 1, "N 个 patch 必须只写一次"
    assert "已签署知情同意书" in sandbox.content
    assert "腺癌，已除外小细胞癌" in sandbox.content
    assert "2" in result, "结果应回报应用了几处"


def test_result_stays_valid_json(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    _run(monkeypatch, sandbox, runtime, [{"old_str": '"符合"', "new_str": '"不符合"'}], _hash(DOC))
    json.loads(sandbox.content)  # 不得写出坏 JSON


def test_reports_new_hash_for_chaining(monkeypatch, runtime):
    """回报新哈希,便于连续批量改判无需重读整份文件。"""
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"old_str": '"符合"', "new_str": '"不符合"'}], _hash(DOC))
    assert _hash(sandbox.content)[:12] in result


# --- 版本校验 ---------------------------------------------------------------


def test_rejects_stale_expected_hash(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"old_str": '"符合"', "new_str": '"不符合"'}], _hash("something else"))

    assert result.startswith("Error:")
    assert "hash" in result.lower()
    assert sandbox.writes == [], "版本不符时不得写入"
    assert sandbox.content == DOC


def test_stale_hash_error_reports_actual_hash(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"old_str": '"符合"', "new_str": '"x"'}], "0" * 64)
    assert _hash(DOC)[:12] in result, "须给出当前实际哈希，便于重读后重试"


# --------------------------------------------------------------------------
# 哈希不匹配的**恢复路径**（thread `93d8a2c6` seq 1220-1224）
# --------------------------------------------------------------------------
#
# 实测故障：expected_hash 无法从任何文件工具取得（`read_file` 不回报哈希），于是模型
# **凭空编了一个**（`d90a6bb44b04` 在整个 run 里从未出现过）→ 被拒 → 再花一次
# `bash sha256sum` 去算这条报错**刚刚已经给出**的值 → 才重试成功。
# 该会话 13 次带 expected_hash 的调用里 2 次被拒（15%），并为取哈希跑了 6 次 sha256sum。
# 每一步都要重传整段上下文（实测 18×~30×），所以"三步恢复一步的问题"很贵。


def test_pointer_form_mismatch_says_the_reported_hash_is_retryable(monkeypatch, runtime):
    """pointer 形态：指针不受文件他处改动影响，可直接用报错里的哈希重试。"""
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": "/documents/d/judgments/IN-1/conclusion", "op": "replace", "value": "不符合"}], "0" * 12)
    actual12 = _hash(DOC)[:12]
    assert f"expected_hash={actual12}" in result, "必须明说可以拿这个哈希直接重试"
    assert "SAME patches" in result, "必须明说 patches 不用重算"


def test_pointer_form_mismatch_still_flags_when_a_reread_is_needed(monkeypatch, runtime):
    """⛔ 不能变成无条件"直接重试"：值若由旧内容推算而来，仍须重读。"""
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": "/documents/d/judgments/IN-1/conclusion", "op": "replace", "value": "x"}], "0" * 12)
    assert "computed from the previous content" in result
    assert "no longer exist" in result, "replace 目标可能已不存在，这个前提要讲清"


def test_text_form_mismatch_still_requires_a_reread(monkeypatch, runtime):
    """text 形态：old_str 是按文本匹配的，文件变了就可能真的匹配不上 → 必须重读。"""
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"old_str": '"符合"', "new_str": '"不符合"'}], "0" * 12)
    assert "Re-read the file" in result
    assert "rebuild the patches" in result
    assert "SAME patches" not in result, "text 形态不得建议原样重试"


def test_missing_hash_is_reported_as_missing_not_as_stale(monkeypatch, runtime):
    """空 expected_hash 是**少传参数**，不是读陈旧了；建议"重读"会把人带错方向。"""
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"pointer": "/documents/d/judgments/IN-1/conclusion", "op": "replace", "value": "x"}], "")
    assert "No expected_hash was supplied" in result
    assert "sha256sum" in result, "必须给出取哈希的具体命令"
    assert sandbox.writes == []


def test_docstring_tells_the_agent_where_the_hash_comes_from(monkeypatch, runtime):
    """根因是**取不到**这个必填参数，文档里必须写清获取方式并禁止猜。"""
    doc = apply_json_patches_tool.func.__doc__ or ""
    assert "sha256sum" in doc, "docstring 未给出获取 expected_hash 的方式"
    assert "Never guess" in doc, "必须明确禁止凭空编造哈希"


def test_accepts_short_hash_prefix(monkeypatch, runtime):
    """允许传 12 位前缀:与 textin.artifacts 的 sha256[:12] 口径一致。"""
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"old_str": '"符合"', "new_str": '"不符合"'}], _hash(DOC)[:12])
    assert result.startswith("OK"), result


# --- 原子性 -----------------------------------------------------------------


def test_missing_old_str_aborts_entire_batch(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    patches = [
        {"old_str": '"缺知情同意记录"', "new_str": '"已签署"'},  # 可用
        {"old_str": "这段文本不存在", "new_str": "x"},  # 不可用
    ]
    result = _run(monkeypatch, sandbox, runtime, patches, _hash(DOC))

    assert result.startswith("Error:")
    assert sandbox.writes == [], "任一 patch 失败即全部不写"
    assert sandbox.content == DOC
    assert "已签署" not in sandbox.content


def test_error_names_the_failing_patch_index(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    patches = [{"old_str": '"符合"', "new_str": '"x"'}, {"old_str": "缺失", "new_str": "y"}]
    result = _run(monkeypatch, sandbox, runtime, patches, _hash(DOC))
    assert "2" in result, "须指明第几个 patch 失败"


def test_ambiguous_old_str_aborts_entire_batch(monkeypatch, runtime):
    """与收紧后的 str_replace 语义一致:多处出现即拒绝,不猜。"""
    doc = json.dumps({"a": "未触发该排除条件", "b": "未触发该排除条件"}, ensure_ascii=False)
    sandbox = FakeSandbox(doc)
    result = _run(monkeypatch, sandbox, runtime, [{"old_str": "未触发该排除条件", "new_str": "触发该排除条件"}], _hash(doc))

    assert result.startswith("Error:")
    assert "2" in result
    assert sandbox.writes == []


def test_patch_applied_earlier_in_batch_is_visible_to_later_patches(monkeypatch, runtime):
    """顺序语义:patch 依次作用于累积结果,不是各自对原文本。"""
    sandbox = FakeSandbox(DOC)
    patches = [
        {"old_str": '"年龄 62 岁"', "new_str": '"年龄 62 岁（已核）"'},
        {"old_str": '"年龄 62 岁（已核）"', "new_str": '"年龄 62 岁（已核对身份证）"'},
    ]
    result = _run(monkeypatch, sandbox, runtime, patches, _hash(DOC))
    assert result.startswith("OK"), result
    assert "已核对身份证" in sandbox.content


# --- 输入校验 ---------------------------------------------------------------


def test_rejects_empty_patch_list(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [], _hash(DOC))
    assert result.startswith("Error:")
    assert sandbox.writes == []


def test_rejects_patch_missing_keys(monkeypatch, runtime):
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"old_str": '"符合"'}], _hash(DOC))
    assert result.startswith("Error:")
    assert sandbox.writes == []


def test_rejects_empty_old_str(monkeypatch, runtime):
    """空 old_str 会在每个字符间插入,必须拒绝。"""
    sandbox = FakeSandbox(DOC)
    result = _run(monkeypatch, sandbox, runtime, [{"old_str": "", "new_str": "x"}], _hash(DOC))
    assert result.startswith("Error:")
    assert sandbox.writes == []


# ---------------------------------------------------------------------------
# JSON Pointer + op 形态（Phase 2 / Task 10）
# ---------------------------------------------------------------------------
#
# 为什么在字符串替换之外还要对象级操作：改判是**一个对象上的多字段联动**
# （`conclusion` + `reason` + `evidence` + `exclusion_triggered`）。字符串定位从原理上
# 保证不了跨字段一致性 —— 会话 `d393714d` 里 `str_replace` 用了 67 次、
# `apply_json_patches` 用了 10 次，task10 仍跑了 161 步 / 5.21M token，反复被门禁抓出
# 「改了 reason 漏改 conclusion」再修补。
#
# 旧形态（`old_str`/`new_str`）必须原样保留：既有 13 项断言、`sandbox_audit_middleware`
# 的报错文案、`config.yaml` 的工具注册都指着同一个工具名。


def _pointer(pointer: str, op: str, **kw) -> dict:
    patch = {"pointer": pointer, "op": op}
    patch.update(kw)
    return patch


def _load(sandbox) -> dict:
    return json.loads(sandbox.content)


IN1 = "/documents/rec/judgments/IN-1"


class TestPointerReplace:
    def test_multi_field_update_in_one_atomic_call(self, monkeypatch, runtime):
        """一次调用改完同一条目的三个字段 —— 这正是字符串替换做不到的一致性。"""
        sandbox = FakeSandbox(DOC)
        patches = [
            _pointer(f"{IN1}/conclusion", "replace", value="符合"),
            _pointer(f"{IN1}/reason", "replace", value="筛选期病历载明知情同意书签署=2026-04-15 16:21"),
            _pointer(f"{IN1}/exclusion_triggered", "add", value=False),
        ]
        result = _run(monkeypatch, sandbox, runtime, patches, _hash(DOC))

        assert result.startswith("OK"), result
        assert len(sandbox.writes) == 1, "多字段联动必须只写一次"
        entry = _load(sandbox)["documents"]["rec"]["judgments"]["IN-1"]
        assert entry["conclusion"] == "符合"
        assert entry["reason"].startswith("筛选期病历载明")
        assert entry["exclusion_triggered"] is False

    def test_untouched_entries_are_preserved(self, monkeypatch, runtime):
        """⛔ 对象级编辑不得变成变相全量重写：没点名的条目必须逐字不变。"""
        sandbox = FakeSandbox(DOC)
        _run(monkeypatch, sandbox, runtime, [_pointer(f"{IN1}/conclusion", "replace", value="符合")], _hash(DOC))
        judgments = _load(sandbox)["documents"]["rec"]["judgments"]
        assert set(judgments) == {"IN-1", "IN-2", "IN-3"}, "条目数守恒"
        assert judgments["IN-2"] == {"conclusion": "符合", "reason": "年龄 62 岁"}
        assert judgments["IN-3"] == {"conclusion": "存疑", "reason": "病理分型未明"}

    def test_missing_pointer_aborts_entire_batch(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        patches = [
            _pointer(f"{IN1}/conclusion", "replace", value="符合"),
            _pointer("/documents/rec/judgments/IN-99/conclusion", "replace", value="符合"),
        ]
        result = _run(monkeypatch, sandbox, runtime, patches, _hash(DOC))
        assert result.startswith("Error"), result
        assert "IN-99" in result
        assert sandbox.writes == [], "任一 patch 不适用 → 全部不写"

    def test_replace_requires_existing_target(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer(f"{IN1}/evidence", "replace", value=[])], _hash(DOC))
        assert result.startswith("Error"), "replace 不得凭空创建字段（那是 add 的语义）"
        assert sandbox.writes == []


class TestPointerAddRemove:
    def test_add_creates_a_new_entry(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        new_entry = {"conclusion": "不符合", "reason": "活动性肺结核，触发该排除条件"}
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/documents/rec/judgments/IN-4", "add", value=new_entry)], _hash(DOC))
        assert result.startswith("OK"), result
        assert _load(sandbox)["documents"]["rec"]["judgments"]["IN-4"] == new_entry

    def test_add_rejects_missing_parent(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/documents/nope/judgments/IN-4", "add", value={})], _hash(DOC))
        assert result.startswith("Error"), "父路径不存在必须拒绝，而不是凭空造出中间层"
        assert sandbox.writes == []

    def test_remove_deletes_only_the_named_entry(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer(IN1, "remove")], _hash(DOC))
        assert result.startswith("OK"), result
        judgments = _load(sandbox)["documents"]["rec"]["judgments"]
        assert set(judgments) == {"IN-2", "IN-3"}

    def test_remove_of_missing_pointer_is_rejected(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/documents/rec/judgments/IN-99", "remove")], _hash(DOC))
        assert result.startswith("Error"), "remove 不是幂等的许可证：删不存在的条目说明定位错了"
        assert sandbox.writes == []


class TestPointerGet:
    def test_get_returns_the_object_without_writing(self, monkeypatch, runtime):
        """`get` 让「只看一条」不必读全文 —— 这是把单 task 16 次读降到 1-2 次的关键。"""
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer(IN1, "get")], _hash(DOC))
        assert sandbox.writes == [], "纯 get 批次不得写文件"
        assert "无法判断" in result and "缺知情同意记录" in result
        assert "OK" in result or "get" in result.lower()

    def test_get_alongside_mutation_still_writes_once(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        patches = [
            _pointer("/documents/rec/judgments/IN-2", "get"),
            _pointer(f"{IN1}/conclusion", "replace", value="符合"),
        ]
        result = _run(monkeypatch, sandbox, runtime, patches, _hash(DOC))
        assert result.startswith("OK"), result
        assert len(sandbox.writes) == 1
        assert "年龄 62 岁" in result, "get 的取值必须回给模型"

    def test_get_of_missing_pointer_is_an_error(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/documents/rec/judgments/IN-99", "get")], _hash(DOC))
        assert result.startswith("Error")


class TestPointerArraysAndEscaping:
    ARRAY_DOC = json.dumps({"items": [{"id": "a"}, {"id": "b"}], "we/ird~key": 1}, ensure_ascii=False, indent=1)

    def test_numeric_token_indexes_a_list(self, monkeypatch, runtime):
        sandbox = FakeSandbox(self.ARRAY_DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/items/1/id", "replace", value="B")], _hash(self.ARRAY_DOC))
        assert result.startswith("OK"), result
        assert _load(sandbox)["items"][1]["id"] == "B"

    def test_dash_appends_to_a_list(self, monkeypatch, runtime):
        sandbox = FakeSandbox(self.ARRAY_DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/items/-", "add", value={"id": "c"})], _hash(self.ARRAY_DOC))
        assert result.startswith("OK"), result
        assert [i["id"] for i in _load(sandbox)["items"]] == ["a", "b", "c"]

    def test_out_of_range_index_is_rejected(self, monkeypatch, runtime):
        sandbox = FakeSandbox(self.ARRAY_DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/items/9/id", "replace", value="x")], _hash(self.ARRAY_DOC))
        assert result.startswith("Error")
        assert sandbox.writes == []

    def test_escaped_tokens_are_decoded(self, monkeypatch, runtime):
        """RFC 6901: `~1` = `/`，`~0` = `~`。判定 JSON 的 document 键含中文与斜杠时用得上。"""
        sandbox = FakeSandbox(self.ARRAY_DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/we~1ird~0key", "replace", value=2)], _hash(self.ARRAY_DOC))
        assert result.startswith("OK"), result
        assert _load(sandbox)["we/ird~key"] == 2


class TestFormAndFormattingGuards:
    def test_mixing_the_two_forms_is_rejected(self, monkeypatch, runtime):
        """一个批次里混用两种形态会让「文本替换」与「对象编辑」交替作用于同一份内容，
        中间必须反复序列化，产出格式不可预期。拒绝并提示拆成两次调用。"""
        sandbox = FakeSandbox(DOC)
        patches = [
            {"old_str": '"缺知情同意记录"', "new_str": '"已签署"'},
            _pointer(f"{IN1}/conclusion", "replace", value="符合"),
        ]
        result = _run(monkeypatch, sandbox, runtime, patches, _hash(DOC))
        assert result.startswith("Error"), result
        assert sandbox.writes == []

    def test_unknown_op_is_rejected(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer(IN1, "move", value=1)], _hash(DOC))
        assert result.startswith("Error")
        assert "move" in result

    def test_replace_and_add_require_a_value(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [{"pointer": f"{IN1}/conclusion", "op": "replace"}], _hash(DOC))
        assert result.startswith("Error")
        assert "value" in result

    def test_pointer_must_be_rooted(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer("documents/rec", "replace", value=1)], _hash(DOC))
        assert result.startswith("Error")

    def test_invalid_json_document_is_rejected_for_pointer_form(self, monkeypatch, runtime):
        """对象级编辑必须先能解析；非 JSON 文件请继续用 old_str 形态。"""
        sandbox = FakeSandbox("not json at all")
        result = _run(monkeypatch, sandbox, runtime, [_pointer("/a", "replace", value=1)], _hash("not json at all"))
        assert result.startswith("Error")
        assert sandbox.writes == []

    def test_original_indent_is_preserved(self, monkeypatch, runtime):
        """判定 JSON 用 indent=1 落盘；重排缩进会让整份文件在 diff 里全变，掩盖真实改动。"""
        sandbox = FakeSandbox(DOC)
        _run(monkeypatch, sandbox, runtime, [_pointer(f"{IN1}/conclusion", "replace", value="符合")], _hash(DOC))
        assert '\n "documents"' in sandbox.content, f"缩进应保持 1 空格：{sandbox.content[:60]!r}"

    def test_non_ascii_is_not_escaped(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        _run(monkeypatch, sandbox, runtime, [_pointer(f"{IN1}/conclusion", "replace", value="符合")], _hash(DOC))
        assert "符合" in sandbox.content
        assert "\\u" not in sandbox.content


class TestPointerVersionCheck:
    def test_stale_hash_is_rejected_for_pointer_form_too(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer(f"{IN1}/conclusion", "replace", value="符合")], _hash("something else"))
        assert result.startswith("Error")
        assert "expected_hash" in result
        assert sandbox.writes == []

    def test_new_hash_is_reported_for_chaining(self, monkeypatch, runtime):
        sandbox = FakeSandbox(DOC)
        result = _run(monkeypatch, sandbox, runtime, [_pointer(f"{IN1}/conclusion", "replace", value="符合")], _hash(DOC))
        assert _hash(sandbox.content)[:12] in result
