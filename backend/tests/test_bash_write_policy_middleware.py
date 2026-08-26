"""bash 内联脚本不得改写结构化产物(.json)。

**故障**(thread `88df83a8`,IN 判定 task `call_00_VNc46dim3StwwZDYBSh82047`):子代理用
`python3 << 'EOF'` heredoc 两次重写 `judgments_draft_MCRC-2150006_IN.json`(step 861、863)
去修引号问题,而它自己的委派 prompt 第 3 条写着:「产物只能由 `write_file`(首次落盘)或
`apply_json_patches`(改判)写。禁止用 `bash` 内联脚本(`python3 -c`、heredoc、`echo >`)
生成或改写 `.json`」。散文没管住,所以做成机械的。

为什么必须保守:判据要求「内联代码/重定向 + 受管产物路径 + 写意图」**三者同时成立**。
放宽任何一条就会打到主路径 —— 例如 `python3 judge_pack.py --out criteria_judge_IN.json`
(step 787/792)是技能脚本的正常产出方式,拦了它等于把整个 Phase 2 收尾堵死。

测试语料**全部取自该会话真实命令**,seq 号标在每条后面。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.bash_write_policy_middleware import BashWritePolicyMiddleware
from deerflow.config.bash_write_policy_config import BashWritePolicyConfig

DRAFT_IN = "/mnt/user-data/workspace/patients/MCRC-2150006/judgments_draft_MCRC-2150006_IN.json"

# ── 必须拦下(真实命令,seq 见注释)────────────────────────────────────────────
BLOCKED = [
    # seq 861:heredoc 读入 → 字符串替换 → open(...,'w') 回写
    pytest.param(
        f"python3 << 'EOF'\nimport json\nwith open('{DRAFT_IN}', 'r') as f:\n    content = f.read()\ncontent = content.replace('a', 'b')\nwith open('{DRAFT_IN}', 'w') as f:\n    f.write(content)\nEOF\n",
        id="seq861-heredoc-rewrite",
    ),
    # seq 863:同一手法,自定义 heredoc 标签
    pytest.param(
        f"python3 << 'PYEOF'\nimport json\nfixed = open('{DRAFT_IN}').read()\nwith open('{DRAFT_IN}', 'w') as f:\n        f.write(fixed)\nPYEOF\n",
        id="seq863-heredoc-rewrite-custom-tag",
    ),
    # seq 812:内联 python 生成 patient_index.json(write_text + json.dumps)
    pytest.param(
        "cd /mnt/user-data/workspace && python3 -c \"\nimport json\nfrom pathlib import Path\nws = Path('/mnt/user-data/workspace')\n(ws / 'patient_index.json').write_text(json.dumps([{'patient_id': 'MCRC-2150006'}]))\n\"",
        id="seq812-inline-json-generation",
    ),
    pytest.param(f"python3 -c \"import json; json.dump({{}}, open('{DRAFT_IN}','w'))\"", id="inline-json-dump"),
    pytest.param(f"echo '{{}}' > {DRAFT_IN}", id="redirect-overwrite"),
    pytest.param(f"echo '{{}}' >> {DRAFT_IN}", id="redirect-append"),
    pytest.param(f"cat /tmp/new.txt | tee {DRAFT_IN}", id="tee"),
    pytest.param(f"sed -i 's/a/b/' {DRAFT_IN}", id="sed-in-place"),
    # 相对路径 + cd:sandbox 工作目录就在受管前缀下,不能因为少了前缀就放过
    pytest.param(
        "cd /mnt/user-data/workspace && python3 -c \"import json; json.dump({}, open('criteria_parsed_IN.json','w'))\"",
        id="relative-path-after-cd",
    ),
    # 会话 881e7ba8 step 13:重做任务 `rm -f` 初版解析产物绕开 read-before-write 闸,
    # 随后 write_file 畅通无阻地重建出降级版(拆分版被销毁)。删除治理产物同样必须收归工具。
    pytest.param(
        "rm -f /mnt/user-data/workspace/criteria_parsed_EX.json",
        id="881e7ba8-step13-rm-artifact",
    ),
    pytest.param("rm /mnt/user-data/workspace/criteria_parsed_IN.json", id="rm-artifact-no-flag"),
    pytest.param(
        "cd /mnt/user-data/workspace && rm criteria_parsed_IN.json",
        id="rm-relative-after-cd",
    ),
    # 改名逃离治理同样拦(源是治理产物)
    pytest.param(
        "mv /mnt/user-data/workspace/criteria_parsed_EX.json /mnt/user-data/workspace/criteria_parsed_EX.json.bak",
        id="mv-rename-away",
    ),
    # 内联代码里的 os.remove 也算删除意图
    pytest.param(
        f"python3 << 'EOF'\nimport os\nos.remove('{DRAFT_IN}')\nEOF",
        id="inline-os-remove",
    ),
]

# ── 必须放行(同样是真实命令)──────────────────────────────────────────────────
ALLOWED = [
    # seq 787:技能脚本产出判定输入包 —— 主路径,拦了就把 Phase 2 收尾堵死
    pytest.param(
        "cd /mnt/user-data/workspace && python3 /mnt/skills/custom/eligibility-judgment/scripts/judge_pack.py slim --criteria criteria_parsed_IN.json --qc criteria_qc_IN.json --track IN --out criteria_judge_IN.json 2>&1",
        id="seq787-skill-script-out",
    ),
    # seq 792:同上,assemble
    pytest.param(
        "cd /mnt/user-data/workspace && python3 /mnt/skills/custom/eligibility-judgment/scripts/judge_pack.py assemble --in-criteria criteria_parsed_IN.json --meta criteria_meta.json --out criteria_parsed.json 2>&1",
        id="seq792-skill-script-assemble",
    ),
    # seq 843 / 894:只读校验
    pytest.param(f"sha256sum {DRAFT_IN} | cut -c1-12", id="seq843-sha256sum"),
    # seq 855:内联 python 但只 json.load
    pytest.param(f"python3 -c \"import json; json.load(open('{DRAFT_IN}'))\" 2>&1", id="seq855-inline-json-load"),
    # seq 859:内联 python 读行做字符探查,无写操作
    pytest.param(
        f"python3 -c \"\nwith open('{DRAFT_IN}') as f:\n    lines = f.readlines()\nprint(lines[93])\n\" 2>&1 | head -30",
        id="seq859-inline-readlines",
    ),
    # 闸脚本:写的是 .json,但通过脚本文件 + --out,不是内联代码
    pytest.param(
        "python3 /mnt/skills/custom/eligibility-judgment/scripts/uncertain_recheck.py --criteria a.json --judgments b.json --out /mnt/user-data/workspace/patients/MCRC-2150006/uncertain_recheck_x.json",
        id="gate-script-out",
    ),
    pytest.param(f"grep -n 'IN-4-3' {DRAFT_IN}", id="grep-json"),
    pytest.param(f"ls -la {DRAFT_IN}", id="ls-json"),
    # 内联写入的不是受管后缀
    pytest.param("python3 -c \"open('/mnt/user-data/workspace/notes.md','w').write('hi')\"", id="inline-write-md"),
    # 内联写入受管后缀但在 /tmp:不是产物目录
    pytest.param("python3 -c \"import json; json.dump({}, open('/tmp/scratch.json','w'))\"", id="inline-write-tmp"),
    # 删除/改名不受管对象:/tmp 临时文件、非治理后缀、目录(目录递归删除按路径 token 保守放行)
    pytest.param("rm /tmp/scratch.json", id="rm-tmp-scratch"),
    pytest.param("rm -rf /mnt/user-data/workspace/images/筛选期病历", id="rm-image-dir"),
    pytest.param("rm /mnt/user-data/workspace/notes.md", id="rm-non-governed-suffix"),
]


def _request(command: str, tool: str = "bash"):
    return SimpleNamespace(
        tool_call={"name": tool, "args": {"command": command}, "id": "call-x"},
        runtime=SimpleNamespace(context={"thread_id": "t", "task_id": "task-1"}),
        state={"messages": []},
    )


def _handler(body: str = "(no output)"):
    calls: list = []

    def handler(request):
        calls.append(request)
        return ToolMessage(content=body, tool_call_id="call-x", name="bash")

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def _middleware(**overrides) -> BashWritePolicyMiddleware:
    return BashWritePolicyMiddleware(config=BashWritePolicyConfig(enabled=True, **overrides))


class TestBlockedCorpus:
    @pytest.mark.parametrize("command", BLOCKED)
    def test_inline_artifact_writes_are_refused(self, command: str):
        handler = _handler()
        result = _middleware().wrap_tool_call(_request(command), handler)
        assert handler.calls == [], "被拦的命令不得执行"
        assert result.content.startswith("Error:")
        assert "write_file" in result.content and "apply_json_patches" in result.content


class TestAllowedCorpus:
    @pytest.mark.parametrize("command", ALLOWED)
    def test_legitimate_commands_run_untouched(self, command: str):
        handler = _handler("ok")
        result = _middleware().wrap_tool_call(_request(command), handler)
        assert len(handler.calls) == 1, "正常命令必须照常执行"
        assert result.content == "ok"


class TestWarnMode:
    def test_command_runs_and_guidance_is_appended(self):
        command = BLOCKED[0].values[0]
        handler = _handler("done")
        result = _middleware(mode="warn").wrap_tool_call(_request(command), handler)
        assert len(handler.calls) == 1
        assert result.content.startswith("done")
        assert "apply_json_patches" in result.content


class TestDisabled:
    def test_disabled_config_is_a_pure_passthrough(self):
        middleware = BashWritePolicyMiddleware(config=BashWritePolicyConfig(enabled=False))
        command = BLOCKED[0].values[0]
        handler = _handler("done")
        assert middleware.wrap_tool_call(_request(command), handler).content == "done"
        assert len(handler.calls) == 1


class TestScope:
    def test_non_bash_tools_are_untouched(self):
        handler = _handler("ok")
        result = _middleware().wrap_tool_call(_request("anything", tool="write_file"), handler)
        assert result.content == "ok"

    def test_configurable_suffixes(self):
        middleware = _middleware(blocked_suffixes=[".json", ".md"])
        handler = _handler("ok")
        result = middleware.wrap_tool_call(
            _request("python3 -c \"open('/mnt/user-data/workspace/notes.md','w').write('x')\""),
            handler,
        )
        assert result.content.startswith("Error:")


class TestAsyncParity:
    @pytest.mark.asyncio
    async def test_async_path_matches_sync_path(self):
        async def ahandler(request):
            return ToolMessage(content="ok", tool_call_id="call-x", name="bash")

        blocked = await _middleware().awrap_tool_call(_request(BLOCKED[0].values[0]), ahandler)
        assert blocked.content.startswith("Error:")

        allowed = await _middleware().awrap_tool_call(_request(ALLOWED[2].values[0]), ahandler)
        assert allowed.content == "ok"


class TestNoGraphNode:
    """同 read_file_policy:只用 wrap_tool_call,不加图节点,不动 max_turns 倍率。"""

    def test_only_tool_call_wrappers_are_implemented(self):
        from langchain.agents.middleware import AgentMiddleware

        node_hooks = ("before_model", "after_model", "abefore_model", "aafter_model", "before_agent", "after_agent")
        overridden = [h for h in node_hooks if getattr(BashWritePolicyMiddleware, h, None) is not getattr(AgentMiddleware, h, None)]
        assert overridden == []


class TestChainMounting:
    @staticmethod
    def _app_config(**overrides):
        from deerflow.config.app_config import AppConfig
        from deerflow.config.sandbox_config import SandboxConfig

        return AppConfig(sandbox=SandboxConfig(use="test"), **overrides)

    def test_mounted_in_both_chains_when_enabled(self):
        from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares, build_subagent_runtime_middlewares

        config = self._app_config(bash_write_policy=BashWritePolicyConfig(enabled=True))
        for build in (build_lead_runtime_middlewares, build_subagent_runtime_middlewares):
            names = [type(m).__name__ for m in build(app_config=config)]
            assert "BashWritePolicyMiddleware" in names

    def test_disabled_config_mounts_nothing(self):
        from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares

        names = [type(m).__name__ for m in build_lead_runtime_middlewares(app_config=self._app_config())]
        assert "BashWritePolicyMiddleware" not in names
