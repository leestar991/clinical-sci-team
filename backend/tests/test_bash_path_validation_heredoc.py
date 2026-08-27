"""Absolute-path validation must not read a quoted heredoc body as host paths.

Session ``a7c19ea1``, four rejected turns inside a loop that was already stuck:

    Error: Unsafe absolute paths in command: /Users/
    Error: Unsafe absolute paths in command: /pid/, /source
    Error: Unsafe absolute paths in command: /Users
    Error: Unsafe absolute paths in command: /Users, /Users:

Every one of those "paths" was text inside an embedded Python program — a ``re.sub``
replacement, a ``grep`` needle, two dict keys. The command opened none of them. As with
the ``sed``/``awk`` regex bodies in ``c2518bc7``, the model cannot act on the message, so
it reshuffles the command and pays another full context re-send per attempt.

The exemption is deliberately narrow: only ``<<'EOF'`` / ``<<"EOF"``, where bash performs
no expansion, so the body cannot resolve to a host path the caller did not literally
write. Unquoted ``<<EOF`` interpolates ``$VAR`` and stays enforced — the negative cases
below are the ones a wider exemption would break.
"""

import pytest

from deerflow.sandbox.tools import validate_local_bash_command_paths

_THREAD_DATA = {
    "workspace_path": "/tmp/deerflow-thread/workspace",
    "uploads_path": "/tmp/deerflow-thread/uploads",
    "outputs_path": "/tmp/deerflow-thread/outputs",
}


def _validate(command: str) -> None:
    validate_local_bash_command_paths(command, _THREAD_DATA)


# ── the four commands the session actually lost turns to ────────────────────────

_SESSION_NORMALISE_PROVENANCE = """cd /mnt/user-data/workspace && python3 << 'PYEOF'
import re
from pathlib import Path
ws = Path('/mnt/user-data/workspace')
fixed = 0
for md in sorted((ws/'ocr').rglob('*.md')):
    t = md.read_text(encoding='utf-8')
    nt = re.sub(r'（来源图片：[^（）]*?workspace/images/', r'（来源图片：/mnt/user-data/workspace/images/', t)
    if nt != t:
        md.write_text(nt, encoding='utf-8')
        fixed += 1
print('归一化页数:', fixed)
PYEOF"""

_SESSION_HOST_PREFIX_SCAN = """cd /mnt/user-data/workspace && python3 << 'PYEOF'
from pathlib import Path
bad = [p for p in Path('/mnt/user-data/workspace/ocr').rglob('*.md')
       if '/Users/' in p.read_text(encoding='utf-8')]
print('host-prefixed pages:', len(bad))
PYEOF"""

_SESSION_DICT_KEYS = """python3 << 'PYEOF'
index = {'/pid/': 'patient id', '/source': 'document source'}
for key, label in index.items():
    print(key, label)
PYEOF"""

_SESSION_LABELLED_PREFIX = """python3 << 'PYEOF'
for marker in ('/Users', '/Users:'):
    print('checking marker', marker)
PYEOF"""


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_SESSION_NORMALISE_PROVENANCE, id="re.sub-replacement-with-mnt-and-host"),
        pytest.param(_SESSION_HOST_PREFIX_SCAN, id="host-prefix-needle"),
        pytest.param(_SESSION_DICT_KEYS, id="dict-keys-pid-source"),
        pytest.param(_SESSION_LABELLED_PREFIX, id="labelled-prefix-tuple"),
    ],
)
def test_session_commands_are_allowed(command: str):
    _validate(command)


# ── general shapes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("opener", ["<< 'EOF'", "<<'EOF'", '<< "EOF"', '<<"EOF"', "<<-'EOF'"])
def test_quoted_delimiter_forms(opener: str):
    _validate(f"python3 {opener}\nprint('/etc/passwd')\nEOF")


def test_body_ends_at_the_delimiter_line():
    """Text after the closing tag is ordinary command text and stays checked."""
    with pytest.raises(PermissionError, match="/etc/passwd"):
        _validate("python3 << 'EOF'\nprint('ok')\nEOF\ncat /etc/passwd")


def test_indented_closing_delimiter_is_recognised():
    _validate("python3 <<-'EOF'\nprint('/etc/shadow')\n\tEOF\n")


def test_unterminated_heredoc_extends_to_end():
    """Matches how the shell reads it, so nothing outside the body loses its check."""
    _validate("python3 << 'EOF'\nprint('/etc/passwd')\n")


def test_two_heredocs_in_one_command():
    _validate("python3 << 'A'\nprint('/etc/passwd')\nA\npython3 << 'B'\nprint('/root/.ssh')\nB")


def test_virtual_paths_inside_a_heredoc_remain_fine():
    _validate("python3 << 'EOF'\nopen('/mnt/user-data/workspace/x.json')\nEOF")


# ── negatives: what the exemption must NOT swallow ──────────────────────────────


def test_unquoted_delimiter_stays_enforced():
    """``<<EOF`` expands ``$VAR``, so its body can become a real host path at runtime."""
    with pytest.raises(PermissionError, match="/etc/passwd"):
        _validate("cat <<EOF\n/etc/passwd\nEOF")


def test_unquoted_delimiter_with_dash_stays_enforced():
    with pytest.raises(PermissionError, match="/etc/passwd"):
        _validate("cat <<-EOF\n/etc/passwd\nEOF")


def test_host_path_before_the_heredoc_is_still_rejected():
    with pytest.raises(PermissionError, match="/etc/passwd"):
        _validate("cat /etc/passwd && python3 << 'EOF'\nprint('hi')\nEOF")


def test_host_path_on_the_heredoc_command_line_is_still_rejected():
    """The opener's own line is command text, not body."""
    with pytest.raises(PermissionError, match="/etc/passwd"):
        _validate("python3 /etc/passwd << 'EOF'\nprint('hi')\nEOF")


def test_plain_command_without_heredoc_is_unaffected():
    with pytest.raises(PermissionError, match="/Users"):
        _validate("grep -rl '/Users/' /Users/louli/Documents")


def test_redirect_operator_is_not_a_heredoc():
    """``<`` and ``<<<`` must not be mistaken for a heredoc opener."""
    with pytest.raises(PermissionError, match="/etc/passwd"):
        _validate("cat < /etc/passwd")
    with pytest.raises(PermissionError, match="/etc/passwd"):
        _validate("cat <<< /etc/passwd")
