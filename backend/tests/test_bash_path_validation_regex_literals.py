"""Absolute-path validation must not mistake ``sed``/``awk`` regex literals for host paths.

Session ``c2518bc7``, EX 轨解析 task, three consecutive wasted AI steps:

    AI#13  awk -F: 'NR>=5 && /^[0-9]+:/'   -> Error: Unsafe absolute paths in command: /^[0-9]+:/
    AI#17  ... | sed 's/．//'               -> Error: Unsafe absolute paths in command: //
    AI#21  ... | sed 's/．//' | sort -n     -> Error: Unsafe absolute paths in command: //

The scan runs over the raw command string, so a slash-delimited regex body reads as
``/segment``. The existing literal exemption only covers non-ASCII fragments and single
identifier ``{placeholder}`` braces, so an ASCII regex body is reported as a host-path
violation. The model cannot act on that message — the command has no host path — so it
reshuffles the pipeline and pays a full context re-send per attempt.

These tests pin the three observed shapes as allowed **and** keep every real host-path
rejection in place; the negative cases here are deliberately the ones a widened
exemption would break.
"""

import pytest

from deerflow.sandbox.tools import validate_local_bash_command_paths

_THREAD_DATA = {
    "workspace_path": "/tmp/deerflow-thread/workspace",
    "uploads_path": "/tmp/deerflow-thread/uploads",
    "outputs_path": "/tmp/deerflow-thread/outputs",
}

_RAW = "/mnt/user-data/workspace/eligibility_criteria_raw.md"


@pytest.mark.parametrize(
    "command",
    [
        # Observed verbatim in the session.
        f"sed -n '61,153p' {_RAW} | awk -F: 'NR>=5 && /^[0-9]+:/'",
        f"sed -n '61,153p' {_RAW} | grep -Eo '^[0-9]+．' | sed 's/．//'",
        f"sed -n '61,153p' {_RAW} | grep -oE '[0-9]+．' | sed 's/．//' | sort -n",
        # Same shapes, generalised.
        f"awk '/^## /{{print NR}}' {_RAW}",
        f"sed 's/foo//' {_RAW}",
        f"awk -F'\\t' '/^EX-/ {{print $1}}' {_RAW}",
    ],
)
def test_regex_literals_are_not_reported_as_host_paths(command: str) -> None:
    validate_local_bash_command_paths(command, _THREAD_DATA)


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd",
        "cat /etc/shadow",
        "ls /",
        "rm -rf /",
        # A slash-run as a real token still addresses the root; only an *embedded*
        # slash-run (a sed replacement body) is treated as text.
        "rm -rf //",
        "cp -r // /mnt/user-data/workspace",
        # Balanced-but-not-identifier braces stay a brace-expansion bypass, not a literal.
        "cat /{etc,var}/passwd",
        "python3 -c \"open('/etc/passwd').read()\"",
    ],
)
def test_real_host_paths_stay_blocked(command: str) -> None:
    with pytest.raises(PermissionError):
        validate_local_bash_command_paths(command, _THREAD_DATA)
