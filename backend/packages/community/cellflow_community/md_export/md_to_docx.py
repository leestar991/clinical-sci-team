"""Convert a markdown report in the sandbox outputs directory to a .docx file.

Design notes:
- Sits alongside `present_file_tool`. Path handling is done via the Sandbox
  abstraction (NOT host-side `Path()`) because in object-storage sandbox modes
  (AgentRun / VeFaas) the outputs directory is mounted through OSS/TOS and is
  NOT visible on the gateway host filesystem — a `Path("/mnt/user-data/outputs/...")`
  from the gateway process would silently miss the agent's files.
- Reads the .md via `Sandbox.read_file` (str), converts on the gateway side
  via `pypandoc-binary` into a tempfile, then uploads the .docx bytes back
  through `Sandbox.update_file(path, bytes)`. Both LocalSandbox and
  AgentRunSandbox implement `update_file`; providers that do not will get an
  explicit error message.
- `pypandoc-binary` ships the pandoc executable inside the wheel, so no
  system-level pandoc install is required.
- The sync-IO parts (`read_file`, `pypandoc.convert_text`, `update_file`) are
  wrapped in `asyncio.to_thread` per the gateway's blocking-IO contract.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import pypandoc
from langchain_core.tools import tool

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.tools.types import Runtime

# NOTE: `deerflow.sandbox.tools` is imported LAZILY inside md_to_docx() to
# avoid a circular import — sandbox.tools → deerflow.tools.builtins →
# view_image_tool → deerflow.sandbox.tools would break module load when
# md_to_docx is first imported.

logger = logging.getLogger(__name__)

OUTPUTS_VIRTUAL_PREFIX = f"{VIRTUAL_PATH_PREFIX}/outputs"


def _validate_basename(name: str, field: str) -> str:
    """Reject anything that is not a bare basename."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"{field}: expected a bare filename, got {name!r}")
    return name


@tool("md_to_docx", parse_docstring=True)
async def md_to_docx(
    runtime: Runtime,
    source_filename: str = "final_report.md",
    output_filename: str = "final_report.docx",
) -> str:
    """Convert a markdown file in the outputs directory to a .docx file.

    Use this to produce a downloadable Word document from a markdown report.
    The generated .docx is placed alongside the source .md under
    `/mnt/user-data/outputs/`, and its virtual path is returned. Follow up
    with `present_files` to surface it to the user.

    Both arguments must be bare filenames (no directory components); they are
    always resolved under `/mnt/user-data/outputs/`. Only .md → .docx is
    supported by this tool; future targets (pdf / xlsx / pptx) will be
    exposed as separate tools in the `md_export` group.

    Args:
        source_filename: Basename of the source markdown file inside `/mnt/user-data/outputs/`. Must end with `.md`. Defaults to `final_report.md`.
        output_filename: Basename of the target .docx file inside `/mnt/user-data/outputs/`. Must end with `.docx`. Defaults to `final_report.docx`.

    Returns:
        The virtual path to the generated `.docx` file (e.g.
        `/mnt/user-data/outputs/final_report.docx`), or an error message
        prefixed with `Error:` on failure.
    """
    try:
        source_filename = _validate_basename(source_filename, "source_filename")
        output_filename = _validate_basename(output_filename, "output_filename")
        if not source_filename.lower().endswith(".md"):
            return f"Error: source_filename must end with .md, got {source_filename!r}"
        if not output_filename.lower().endswith(".docx"):
            return f"Error: output_filename must end with .docx, got {output_filename!r}"

        # Lazy import — see module-level NOTE about the circular import.
        from deerflow.sandbox.tools import ensure_sandbox_initialized_async

        sandbox = await ensure_sandbox_initialized_async(runtime)

        source_virtual = f"{OUTPUTS_VIRTUAL_PREFIX}/{source_filename}"
        output_virtual = f"{OUTPUTS_VIRTUAL_PREFIX}/{output_filename}"

        # 1. Read the source markdown through the sandbox abstraction so
        #    OSS/TOS-mounted outputs directories work identically to local.
        try:
            md_content = await asyncio.to_thread(sandbox.read_file, source_virtual)
        except FileNotFoundError:
            return f"Error: source file not found: {source_virtual}"
        except Exception as exc:  # pragma: no cover — sandbox-specific errors
            return f"Error: cannot read {source_virtual}: {exc}"

        # 2. pypandoc requires a file path for binary output — convert into a
        #    gateway-side tempfile, then read the bytes back for upload.
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            await asyncio.to_thread(
                pypandoc.convert_text,
                md_content,
                "docx",
                format="md",
                outputfile=str(tmp_path),
            )
            docx_bytes = tmp_path.read_bytes()
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

        # 3. Upload the docx bytes back into the sandbox outputs directory.
        update_fn = getattr(sandbox, "update_file", None)
        if update_fn is None:
            return f"Error: sandbox provider {type(sandbox).__name__} does not support binary upload (missing update_file). Cannot deliver docx."
        await asyncio.to_thread(update_fn, output_virtual, docx_bytes)

        return output_virtual
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # pragma: no cover — pypandoc / IO failures
        logger.exception("md_to_docx failed")
        return f"Error: md_to_docx failed: {exc}"
