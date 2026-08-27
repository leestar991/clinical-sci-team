"""``parse_image_batch`` —— 批量图片 OCR 工具(criteria-token-saving-v1.2 Task 6)。

动机:基线会话里 28 张页图走的是「拆图 → 对每张图调一次 ``parse_document`` → 逐张
``read_file`` → 逐张 ``write_file``」的 Agent 循环,每一步都要一轮 AI step。批量工具把 N 页
压成**一次工具调用**、0 次 read/write。

契约要点(测试锁定):
- ``input_dir`` 下的图片 → ``output_dir`` 下**同名** ``.md``(``x_page_001.jpg`` → ``x_page_001.md``)。
- **首行是来源标注** ``（来源图片：{虚拟路径}）``。这是 ``ocr_records.md`` 页块结构的唯一来源
  (``patient-separator`` 的 ``references/aggregate-ocr.md``),下游判定的 ``evidence[].page`` /
  ``screenshot_ref`` 全靠它定位。以前它由 OCR 子代理手写(``pdf-image-extractor/SKILL.md``),
  本工具接管落盘后就没人写了 —— 真实故障 thread ``1fee1395``:7 页 OCR 全无来源行,
  聚合脚本(对 ``.md`` 分支原样拼接)照实拼出无页块的 ``ocr_records.md``,该会话判定产物里
  ``screenshot_ref`` / ``page`` 出现 **0** 次(对照 thread ``9a83ccc9`` 为 78/54)。
  故改为由工具确定性写入,不再依赖模型顺手补。
- 路径必须是 ``/mnt/user-data/...`` **虚拟路径**,不得写宿主机绝对路径(历史产物里写的是
  ``/Users/...``,换部署/容器即失效)。
- 返回**紧凑索引**,绝不返回 OCR 正文 —— 正文进对话即等于放弃本次优化(与 ``parse_document``
  同一哲学:一份 232 页报告的 markdown 约 40 万字符)。
- 并发 ≤3(OCR 外部服务并发约束)。
- 幂等:已存在且非空**且带来源行**的同名 ``.md`` 默认跳过,0 次外部调用;``overwrite=True`` 才重跑。
- 遗留产物(非空但缺来源行)就地补写来源行并计入 ``repaired``,**0 次外部调用** ——
  重跑 OCR 会对已解析过的页重复计费。
- 单张失败不中断整批,失败页记入索引可单独重试。
- 所有 IO 走 Sandbox API(对象存储部署下 gateway 无本地 /mnt/user-data)。
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from cellflow_community.textin import tools as textin_tools
from cellflow_community.textin.client import TextInError
from cellflow_community.textin.types import ParsedDoc

IN_DIR = "/mnt/user-data/workspace/images/M018"
OUT_DIR = "/mnt/user-data/workspace/ocr/M018"

# `ocr_records.md` 页块起始行的唯一契约（见 patient-separator/references/aggregate-ocr.md）。
PROVENANCE_RE = re.compile(r"^（来源图片：/mnt/user-data/.+）$")


def _provenance_of(written: str) -> str:
    """取首行——来源标注必须是**首行**，聚合脚本靠它切页块。"""
    return written.split("\n", 1)[0]


class FakeSandbox:
    """记录调用的最小 Sandbox。"""

    def __init__(self, files: dict[str, bytes], existing_text: dict[str, str] | None = None):
        self._files = dict(files)
        self._text = dict(existing_text or {})
        self.writes: dict[str, str] = {}
        self.downloads: list[str] = []
        self.glob_calls: list[tuple[str, str]] = []

    @property
    def id(self) -> str:
        return "sandbox-test"

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200):
        self.glob_calls.append((path, pattern))
        hits = [p for p in self._files if p.startswith(path.rstrip("/") + "/")]
        return sorted(hits), False

    def list_dir(self, path: str, max_depth=2) -> list[str]:
        return sorted(p for p in self._files if p.startswith(path.rstrip("/") + "/"))

    def download_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        self.downloads.append(path)
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    def read_file(self, path: str) -> str:
        if path in self.writes:
            return self.writes[path]
        if path in self._text:
            return self._text[path]
        raise FileNotFoundError(path)

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        self.writes[path] = content


@pytest.fixture
def runtime() -> SimpleNamespace:
    return SimpleNamespace(state={}, context={"thread_id": "t1"}, config={})


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    monkeypatch.setattr(
        textin_tools,
        "_tool_settings",
        lambda: {
            "app_id": "id",
            "secret_code": "code",
            "base_url": "https://api.textin.com",
            "timeout": 10.0,
            "max_bytes": 1024 * 1024,
        },
    )


def _install(monkeypatch, sandbox: FakeSandbox, parse_impl):
    async def _ensure(_runtime):
        return sandbox

    monkeypatch.setattr(textin_tools, "_ensure_sandbox", _ensure)
    monkeypatch.setattr(textin_tools, "parse_via_textin", parse_impl)


def _three_images() -> dict[str, bytes]:
    return {
        f"{IN_DIR}/M018_page_001.jpg": b"img1",
        f"{IN_DIR}/M018_page_002.jpg": b"img2",
        f"{IN_DIR}/M018_page_003.jpg": b"img3",
    }


def _run(runtime, **kw) -> str:
    # async @tool exposes the callable as `.coroutine` (`.func` is None) — same
    # convention as tests/test_browserless_client.py.
    return asyncio.run(textin_tools.parse_image_batch_tool.coroutine(runtime, **kw))


# --- 主路径 -----------------------------------------------------------------


def test_writes_one_same_named_markdown_per_image(monkeypatch, runtime):
    sandbox = FakeSandbox(_three_images())
    calls: list[str] = []

    async def fake_parse(data, filename, **kw):
        calls.append(filename)
        return ParsedDoc(markdown=f"# {filename}\ncontent", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    result = _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    assert sorted(sandbox.writes) == [
        f"{OUT_DIR}/M018_page_001.md",
        f"{OUT_DIR}/M018_page_002.md",
        f"{OUT_DIR}/M018_page_003.md",
    ], "每张图必须产出同名 .md 到 output_dir"
    assert len(calls) == 3, "每张图恰好调用一次 TextIn"
    assert "3" in result


def test_index_does_not_leak_ocr_text(monkeypatch, runtime):
    """返回值绝不能含 OCR 正文——否则批量工具省下的 token 又从返回值漏回对话。"""
    secret = "患者姓名张三，血红蛋白121g/L，这是不该进对话的正文"
    sandbox = FakeSandbox({f"{IN_DIR}/p1.jpg": b"i"})

    async def fake_parse(data, filename, **kw):
        return ParsedDoc(markdown=secret, pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    result = _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    assert secret not in result
    assert "血红蛋白" not in result
    written = sandbox.writes[f"{OUT_DIR}/p1.md"]
    assert secret in written, "正文必须落盘，只是不回传"
    assert PROVENANCE_RE.match(_provenance_of(written)), "落盘内容首行仍须是来源标注"


# --- 来源标注（ocr_records.md 页块契约） ------------------------------------


def test_first_line_is_virtual_path_provenance(monkeypatch, runtime):
    """每个 .md 首行必须是 `（来源图片：{虚拟路径}）`，且路径指向该页原图。

    故障 thread `1fee1395`：本工具接管落盘后没人写这一行，聚合出的 `ocr_records.md`
    无页块，判定产物里 `screenshot_ref` / `page` 归零。
    """
    sandbox = FakeSandbox(_three_images())

    async def fake_parse(data, filename, **kw):
        return ParsedDoc(markdown="正文", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    for page in ("M018_page_001", "M018_page_002", "M018_page_003"):
        written = sandbox.writes[f"{OUT_DIR}/{page}.md"]
        head = _provenance_of(written)
        assert PROVENANCE_RE.match(head), f"{page} 首行不是来源标注：{head!r}"
        assert head == f"（来源图片：{IN_DIR}/{page}.jpg）", "来源路径必须指向该页原图"


def test_provenance_never_uses_host_absolute_path(monkeypatch, runtime):
    """历史产物写的是 /Users/... 宿主机路径，换部署即失效；只允许虚拟路径。"""
    sandbox = FakeSandbox({f"{IN_DIR}/p1.jpg": b"i"})

    async def fake_parse(data, filename, **kw):
        return ParsedDoc(markdown="x", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    written = sandbox.writes[f"{OUT_DIR}/p1.md"]
    assert "/Users/" not in written
    assert ".deer-flow" not in written


def test_provenance_precedes_body_and_tables(monkeypatch, runtime):
    """顺序固定：来源行 → 正文 → 表格。聚合脚本按首行切页块，顺序错了页块就错。"""
    sandbox = FakeSandbox({f"{IN_DIR}/p1.jpg": b"i"})

    async def fake_parse(data, filename, **kw):
        return ParsedDoc(markdown="prose", tables=["<table>A</table>"], pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    written = sandbox.writes[f"{OUT_DIR}/p1.md"]
    assert written.index("（来源图片：") < written.index("prose") < written.index("<table>A</table>")


def test_tables_are_appended_to_markdown(monkeypatch, runtime):
    """表格不在 result.markdown 里(client.py 契约#2)，必须一并写入，否则静默丢表。"""
    sandbox = FakeSandbox({f"{IN_DIR}/p1.jpg": b"i"})

    async def fake_parse(data, filename, **kw):
        return ParsedDoc(markdown="prose", tables=["<table>A</table>"], pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    written = sandbox.writes[f"{OUT_DIR}/p1.md"]
    assert "<table>A</table>" in written


# --- 幂等 -------------------------------------------------------------------


def test_skips_images_whose_markdown_already_exists(monkeypatch, runtime):
    sandbox = FakeSandbox(
        _three_images(),
        existing_text={f"{OUT_DIR}/M018_page_002.md": f"（来源图片：{IN_DIR}/M018_page_002.jpg）\n\nalready ocred"},
    )
    calls: list[str] = []

    async def fake_parse(data, filename, **kw):
        calls.append(filename)
        return ParsedDoc(markdown="x", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    result = _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    assert sorted(calls) == ["M018_page_001.jpg", "M018_page_003.jpg"], "已有输出的页不得再调外部服务"
    assert f"{OUT_DIR}/M018_page_002.md" not in sandbox.writes
    assert "skipped" in result.lower() or "跳过" in result


def test_legacy_markdown_without_provenance_is_repaired_without_provider_call(monkeypatch, runtime):
    """遗留产物（本工具早期版本写的，无来源行）就地补写，绝不重跑 OCR。

    重跑等于对已解析过的页向 TextIn 重复计费；而丢着不管，聚合出的 `ocr_records.md`
    依旧无页块。所以走第三条路：补行，0 次外部调用。
    """
    legacy = "2025.11.17 PSA 2.84\n\n就诊时\n\n<!-- table 001 -->\n<table>旧表</table>"
    sandbox = FakeSandbox(
        {f"{IN_DIR}/M018_page_001.jpg": b"img1"},
        existing_text={f"{OUT_DIR}/M018_page_001.md": legacy},
    )
    calls: list[str] = []

    async def fake_parse(data, filename, **kw):  # pragma: no cover - 不应被调用
        calls.append(filename)
        raise AssertionError("repair path must not call the OCR provider")

    _install(monkeypatch, sandbox, fake_parse)
    result = _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    assert calls == [], "补来源行不得调用外部 OCR 服务"
    assert sandbox.downloads == [], "补来源行不需要下载原图"
    written = sandbox.writes[f"{OUT_DIR}/M018_page_001.md"]
    assert _provenance_of(written) == f"（来源图片：{IN_DIR}/M018_page_001.jpg）"
    assert legacy in written, "原有 OCR 内容一字不得丢失"
    assert "repaired" in result.lower() or "补" in result


def test_repair_is_idempotent(monkeypatch, runtime):
    """补过一次之后再跑就该走 skipped，不能每次都重复 prepend。"""
    already = f"（来源图片：{IN_DIR}/p1.jpg）\n\nbody"
    sandbox = FakeSandbox({f"{IN_DIR}/p1.jpg": b"i"}, existing_text={f"{OUT_DIR}/p1.md": already})

    async def fake_parse(data, filename, **kw):  # pragma: no cover
        raise AssertionError("must not call provider")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    assert sandbox.writes == {}, "已带来源行的产物必须原样跳过"


def test_overwrite_true_reprocesses_existing(monkeypatch, runtime):
    sandbox = FakeSandbox(
        {f"{IN_DIR}/p1.jpg": b"i"},
        existing_text={f"{OUT_DIR}/p1.md": "old"},
    )
    calls: list[str] = []

    async def fake_parse(data, filename, **kw):
        calls.append(filename)
        return ParsedDoc(markdown="new", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR, overwrite=True)

    assert calls == ["p1.jpg"]
    written = sandbox.writes[f"{OUT_DIR}/p1.md"]
    assert "new" in written
    assert "old" not in written, "overwrite 必须整体重写，不得与旧内容拼接"
    assert PROVENANCE_RE.match(_provenance_of(written))


def test_empty_existing_markdown_is_not_treated_as_done(monkeypatch, runtime):
    """空 .md 说明上次失败，必须重跑，不能当成已完成、也不能只补来源行。"""
    sandbox = FakeSandbox({f"{IN_DIR}/p1.jpg": b"i"}, existing_text={f"{OUT_DIR}/p1.md": "   "})
    calls: list[str] = []

    async def fake_parse(data, filename, **kw):
        calls.append(filename)
        return ParsedDoc(markdown="real", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)
    assert calls == ["p1.jpg"], "空产物必须真正重跑 OCR，不能走补行捷径"
    assert "real" in sandbox.writes[f"{OUT_DIR}/p1.md"]


# --- 并发上限 ---------------------------------------------------------------


def test_concurrency_never_exceeds_three(monkeypatch, runtime):
    sandbox = FakeSandbox({f"{IN_DIR}/p{i}.jpg": b"i" for i in range(9)})
    state = {"cur": 0, "peak": 0}

    async def fake_parse(data, filename, **kw):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await asyncio.sleep(0.01)
        state["cur"] -= 1
        return ParsedDoc(markdown="x", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    assert state["peak"] <= 3, f"并发峰值 {state['peak']} 超过 OCR 外部服务上限 3"


def test_concurrency_argument_is_clamped(monkeypatch, runtime):
    sandbox = FakeSandbox({f"{IN_DIR}/p{i}.jpg": b"i" for i in range(6)})
    state = {"cur": 0, "peak": 0}

    async def fake_parse(data, filename, **kw):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await asyncio.sleep(0.01)
        state["cur"] -= 1
        return ParsedDoc(markdown="x", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR, concurrency=99)
    assert state["peak"] <= 3, "concurrency 参数必须被夹到 3 以内，不能由模型放大到打爆外部服务"


# --- 部分失败 ---------------------------------------------------------------


def test_one_failure_does_not_abort_the_batch(monkeypatch, runtime):
    sandbox = FakeSandbox(_three_images())

    async def fake_parse(data, filename, **kw):
        if filename == "M018_page_002.jpg":
            raise TextInError("code=40301 图片类型不支持")
        return ParsedDoc(markdown="ok", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    result = _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)

    assert sorted(sandbox.writes) == [f"{OUT_DIR}/M018_page_001.md", f"{OUT_DIR}/M018_page_003.md"]
    assert "M018_page_002.jpg" in result, "失败页必须点名，便于单独重试"
    assert "40301" in result, "失败原因必须回传"


def test_failed_page_writes_nothing(monkeypatch, runtime):
    sandbox = FakeSandbox({f"{IN_DIR}/p1.jpg": b"i"})

    async def fake_parse(data, filename, **kw):
        raise TextInError("boom")

    _install(monkeypatch, sandbox, fake_parse)
    result = _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)
    assert sandbox.writes == {}, "失败页不得写出空/占位 .md，否则幂等跳过会掩盖失败"
    assert "0" in result


# --- 输入校验 ---------------------------------------------------------------


def test_rejects_non_virtual_input_dir(runtime):
    result = _run(runtime, input_dir="/etc", output_dir=OUT_DIR)
    assert result.startswith("Error:")
    assert "/mnt/user-data" in result


def test_rejects_non_virtual_output_dir(runtime):
    result = _run(runtime, input_dir=IN_DIR, output_dir="/tmp/out")
    assert result.startswith("Error:")


def test_non_image_files_are_ignored(monkeypatch, runtime):
    sandbox = FakeSandbox(
        {
            f"{IN_DIR}/p1.jpg": b"i",
            f"{IN_DIR}/M018_manifest.json": b"{}",
            f"{IN_DIR}/notes.txt": b"x",
        }
    )
    calls: list[str] = []

    async def fake_parse(data, filename, **kw):
        calls.append(filename)
        return ParsedDoc(markdown="x", pages=1, parser="textin")

    _install(monkeypatch, sandbox, fake_parse)
    _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)
    assert calls == ["p1.jpg"], "manifest.json / txt 不是图片，不得送 OCR"


def test_empty_input_dir_reports_clearly(monkeypatch, runtime):
    sandbox = FakeSandbox({})

    async def fake_parse(data, filename, **kw):  # pragma: no cover - 不应被调用
        raise AssertionError("must not call TextIn for an empty dir")

    _install(monkeypatch, sandbox, fake_parse)
    result = _run(runtime, input_dir=IN_DIR, output_dir=OUT_DIR)
    assert "no image" in result.lower() or "未找到" in result
