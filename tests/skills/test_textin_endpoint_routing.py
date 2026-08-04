"""TextIn client 回归测试：endpoint 路由与失败语义。

历史故障（thread 0712ffa1）：逐页图片 OCR 全部返回
`code=40007 机器人不存在或未发布`，`workspace/parsed/` 与 `ocr/` 全空，
而整份 PDF 解析正常。根因是把图片路由到了不存在的 `image_to_markdown` 机器人。
2026-07-28 用合成 200x200 PNG 实测：`image_to_markdown` → 40007，
`pdf_to_markdown` → code=200 / total_page_number=1。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_PATH = REPO_ROOT / "backend" / "packages" / "community" / "cellflow_community" / "textin" / "client.py"
TYPES_PATH = CLIENT_PATH.parent / "types.py"

if not CLIENT_PATH.exists():
    pytest.skip("cellflow_community.textin 未安装", allow_module_level=True)

pytest.importorskip("httpx")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# client.py 用相对导入 `.types`，所以先注册一个同名包再加载
types_mod = _load("textin_types_under_test", TYPES_PATH)
pkg = type(sys)("textin_pkg_under_test")
pkg.__path__ = [str(CLIENT_PATH.parent)]
sys.modules["textin_pkg_under_test"] = pkg
sys.modules["textin_pkg_under_test.types"] = types_mod
spec = importlib.util.spec_from_file_location("textin_pkg_under_test.client", CLIENT_PATH)
client = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = client
spec.loader.exec_module(client)


IMAGE_NAMES = ["页_001.jpg", "a.jpeg", "b.PNG", "c.bmp", "d.tiff", "e.tif"]
DOC_NAMES = ["病历.pdf", "入排标准.docx", "x.xlsx", "y.pptx", "z.doc"]


@pytest.mark.parametrize("filename", IMAGE_NAMES + DOC_NAMES)
def test_all_inputs_route_to_pdf_to_markdown(filename):
    """唯一通用 endpoint；图片绝不能再被路由到 image_to_markdown（40007 根因）。"""
    url = client._endpoint("https://api.textin.com", filename)
    assert url == "https://api.textin.com/ai/service/v1/pdf_to_markdown"
    assert "image_to_markdown" not in url


def test_base_url_trailing_slash_normalised():
    assert client._endpoint("https://api.textin.com/", "a.jpg").endswith("/ai/service/v1/pdf_to_markdown")


def test_no_image_endpoint_left_in_source():
    """防回归：源码里不得再出现 image_to_markdown 作为请求路径。"""
    src = CLIENT_PATH.read_text(encoding="utf-8")
    # 仅允许出现在解释根因的注释/docstring 里，不得出现在 f-string 拼接的 leaf 变量中
    assert 'leaf = "image_to_markdown"' not in src
    assert src.count('_MARKDOWN_LEAF = "pdf_to_markdown"') == 1


# --- 失败语义（HTTP 200 + body code != 200）---------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, captured):
        self._payload = payload
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        self._captured["size"] = len(content or b"")
        return _FakeResp(self._payload)


def _patch(monkeypatch, payload, captured):
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **kw: _FakeClient(payload, captured))


@pytest.mark.asyncio
async def test_body_code_40007_raises_textin_error(monkeypatch):
    captured = {}
    _patch(monkeypatch, {"code": 40007, "msg": "机器人不存在或未发布"}, captured)
    with pytest.raises(client.TextInError) as exc:
        await client.parse_via_textin(b"x", "页_001.jpg", app_id="a", secret_code="s", base_url="https://api.textin.com", timeout=5)
    assert "40007" in str(exc.value)
    assert captured["url"].endswith("pdf_to_markdown")


@pytest.mark.asyncio
async def test_success_extracts_markdown_pages_and_html_tables(monkeypatch):
    captured = {}
    payload = {
        "code": 200,
        "msg": "success",
        "result": {
            "markdown": "# 病历",
            "total_page_number": 3,
            "pages": [
                {"structured": [{"type": "table", "text": "<table>1</table>"}, {"type": "paragraph", "text": "忽略"}]},
                {"structured": [{"type": "table", "text": "<table>2</table>"}]},
            ],
        },
    }
    _patch(monkeypatch, payload, captured)
    doc = await client.parse_via_textin(b"xy", "病历.pdf", app_id="a", secret_code="s", base_url="https://api.textin.com", timeout=5)
    assert (doc.markdown, doc.pages, doc.parser) == ("# 病历", 3, "textin")
    assert doc.tables == ["<table>1</table>", "<table>2</table>"]  # 表格只在 structured 里
    assert captured["headers"]["x-ti-app-id"] == "a"


@pytest.mark.asyncio
async def test_missing_credentials_fail_fast(monkeypatch):
    with pytest.raises(client.TextInError, match="credentials"):
        await client.parse_via_textin(b"x", "a.jpg", app_id="", secret_code="s", base_url="https://api.textin.com", timeout=5)
