"""What the agent reads must match what is on disk — session ``a7c19ea1``.

The local sandbox translated container paths in both directions, but on mismatched
criteria: ``write_file`` always rewrote ``/mnt/...`` -> host path in content, while
``read_file`` only rewrote back for files it had itself written
(``_agent_written_paths``, PR #1935). Any file written by another tool therefore read
back with host paths, and any file written through ``write_file`` read back as ``/mnt``
regardless of what the bytes said.

Cost of that asymmetry: ``parse_image_batch`` wrote 39 OCR pages whose provenance line is
documented as virtual-path-only; every page landed on disk with a ``/Users/...`` prefix.
The agent then spent 17 consecutive lead turns (750s, 1.59M tokens) trying to normalise
them, verifying with ``read_file`` (showed ``/mnt`` — looks fixed) and ``bash grep``
(showed ``/Users`` — looks broken). Both observations were correct. The loop could not
converge, and it ended only because the user cancelled the run.

Two rules pinned here:
  * Reverse resolution is by *location* (uploads excepted), never by authorship.
  * Forward resolution applies to executable content only; data files are byte-verbatim.
"""

from pathlib import Path

import pytest

from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping

_VIRTUAL_WORKSPACE = "/mnt/user-data/workspace"
_VIRTUAL_UPLOADS = "/mnt/user-data/uploads"


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSandbox:
    workspace = tmp_path / "workspace"
    uploads = tmp_path / "uploads"
    workspace.mkdir()
    uploads.mkdir()
    return LocalSandbox(
        id="test",
        path_mappings=[
            PathMapping(container_path=_VIRTUAL_WORKSPACE, local_path=str(workspace)),
            PathMapping(container_path=_VIRTUAL_UPLOADS, local_path=str(uploads)),
        ],
    )


def _host(sandbox: LocalSandbox, virtual_root: str) -> str:
    mapping = next(m for m in sandbox.path_mappings if m.container_path == virtual_root)
    return str(Path(mapping.local_path).resolve())


# ── forward direction: data verbatim, scripts translated ────────────────────────


def test_markdown_keeps_virtual_paths_on_disk(sandbox: LocalSandbox, tmp_path: Path):
    """The a7c19ea1 regression, in one assertion.

    This is the OCR provenance line verbatim. It must survive to disk unchanged, or the
    artifact is host-specific and the agent is told a different story on read than on grep.
    """
    line = f"（来源图片：{_VIRTUAL_WORKSPACE}/images/筛选期病历/筛选期病历_page_001.jpg）"
    sandbox.write_file(f"{_VIRTUAL_WORKSPACE}/ocr/page_001.md", f"{line}\n\nbody text\n")

    on_disk = (tmp_path / "workspace" / "ocr" / "page_001.md").read_text(encoding="utf-8")
    assert line in on_disk
    assert str(tmp_path) not in on_disk, "a data file must not be rewritten to host paths"


@pytest.mark.parametrize("name", ["notes.md", "data.json", "table.csv", "out.txt", "conf.yaml", "page.html"])
def test_data_files_are_written_verbatim(sandbox: LocalSandbox, tmp_path: Path, name: str):
    payload = f'{{"src": "{_VIRTUAL_WORKSPACE}/x.jpg"}}'
    sandbox.write_file(f"{_VIRTUAL_WORKSPACE}/{name}", payload)

    assert (tmp_path / "workspace" / name).read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("name", ["run.py", "run.sh", "run.js", "run.ts", "run.ps1", "runner"])
def test_executable_content_still_resolves_to_host_paths(sandbox: LocalSandbox, tmp_path: Path, name: str):
    """A script must keep working: it runs on the host, so its paths must be host paths."""
    sandbox.write_file(f"{_VIRTUAL_WORKSPACE}/{name}", f'open("{_VIRTUAL_WORKSPACE}/x.json")')

    on_disk = (tmp_path / "workspace" / name).read_text(encoding="utf-8")
    assert _host(sandbox, _VIRTUAL_WORKSPACE) in on_disk
    assert _VIRTUAL_WORKSPACE not in on_disk


# ── reverse direction: by location, not authorship ──────────────────────────────


def test_read_file_masks_host_paths_written_by_another_tool(sandbox: LocalSandbox, tmp_path: Path):
    """The core fix: authorship must not decide whether the agent sees the truth.

    Writing directly with ``open()`` stands in for every writer that is not
    ``sandbox.write_file`` — ``parse_image_batch`` in the real incident.
    """
    target = tmp_path / "workspace" / "external.md"
    host_root = _host(sandbox, _VIRTUAL_WORKSPACE)
    target.write_text(f"（来源图片：{host_root}/images/p1.jpg）\n", encoding="utf-8")

    assert f"{_VIRTUAL_WORKSPACE}/images/p1.jpg" in sandbox.read_file(f"{_VIRTUAL_WORKSPACE}/external.md")


def test_uploads_are_never_rewritten(sandbox: LocalSandbox, tmp_path: Path):
    """The case PR #1935 actually protected — kept, but keyed on the directory."""
    host_root = _host(sandbox, _VIRTUAL_UPLOADS)
    (tmp_path / "uploads" / "provided.md").write_text(f"user wrote {host_root}/a.pdf\n", encoding="utf-8")

    content = sandbox.read_file(f"{_VIRTUAL_UPLOADS}/provided.md")
    assert f"{host_root}/a.pdf" in content, "user-supplied text is evidence; report it as-is"
    assert _VIRTUAL_UPLOADS not in content


def test_uploads_exemption_survives_a_write_through_the_sandbox(sandbox: LocalSandbox):
    """Authorship must not re-enter through the back door: location alone decides."""
    host_root = _host(sandbox, _VIRTUAL_UPLOADS)
    sandbox.write_file(f"{_VIRTUAL_UPLOADS}/note.md", f"see {host_root}/a.pdf\n")

    assert host_root in sandbox.read_file(f"{_VIRTUAL_UPLOADS}/note.md")


# ── the property the incident actually violated ─────────────────────────────────


def test_read_file_and_disk_agree_after_a_write(sandbox: LocalSandbox, tmp_path: Path):
    """Round-trip: what the agent writes, reads back, and greps must be one story.

    In a7c19ea1 these three disagreed, so every verification contradicted every fix.
    """
    line = f"（来源图片：{_VIRTUAL_WORKSPACE}/images/p1.jpg）"
    sandbox.write_file(f"{_VIRTUAL_WORKSPACE}/records.md", line)

    read_back = sandbox.read_file(f"{_VIRTUAL_WORKSPACE}/records.md")
    on_disk = (tmp_path / "workspace" / "records.md").read_text(encoding="utf-8")

    assert read_back == line
    assert on_disk == line, "grep on the host must see what read_file reported"


def test_script_round_trip_still_reads_back_as_virtual(sandbox: LocalSandbox):
    """Scripts remain translated on disk, yet read back in the agent's own vocabulary."""
    source = f'open("{_VIRTUAL_WORKSPACE}/x.json")'
    sandbox.write_file(f"{_VIRTUAL_WORKSPACE}/run.py", source)

    assert sandbox.read_file(f"{_VIRTUAL_WORKSPACE}/run.py") == source
