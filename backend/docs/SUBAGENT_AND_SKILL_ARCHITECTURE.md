# Subagent 与 Skill 系统架构文档

> 完整梳理 DeerFlow 项目中 subagent（子代理）和 skill（技能）的加载与调用逻辑链。

---

## 目录

1. [系统概览](#系统概览)
2. [Subagent 系统](#subagent-系统)
   - 2.1 [配置层](#subagent-配置层)
   - 2.2 [注册与发现](#subagent-注册与发现)
   - 2.3 [工具注入](#subagent-工具注入)
   - 2.4 [运行时启用开关](#subagent-运行时启用开关)
   - 2.5 [提示词注入](#subagent-提示词注入)
   - 2.6 [中间件：并发限制](#subagent-中间件并发限制)
   - 2.7 [task() 工具执行流程](#task-工具执行流程)
   - 2.8 [SubagentExecutor 执行引擎](#subagentexecutor-执行引擎)
   - 2.9 [子代理中的 Skill 处理](#子代理中的-skill-处理)
3. [Skill 系统](#skill-系统)
   - 3.1 [目录结构与发现](#skill-目录结构与发现)
   - 3.2 [启用状态管理](#skill-启用状态管理)
   - 3.3 [路径一：系统提示词注入](#skill-路径一系统提示词注入)
   - 3.4 [路径二：斜杠命令激活（SkillActivationMiddleware）](#skill-路径二斜杠命令激活)
   - 3.5 [路径三：Agent 自主 read_file 加载](#skill-路径三agent-自主-read_file-加载)
   - 3.6 [路径四：子代理中的 Skill 加载](#skill-路径四子代理中的-skill-加载)
4. [两条路径的交互与对比](#两条路径的交互与对比)
5. [实战案例：clinical-medicine 对话分析](#实战案例clinical-medicine-对话分析)
6. [关键文件索引](#关键文件索引)

---

## 系统概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户请求                                   │
├─────────────────────────────────────────────────────────────────┤
│  前端 mode 选择器                                                │
│  Flash / Thinking / Pro / Ultra                                 │
│  └─ Ultra → subagent_enabled=true                               │
├─────────────────────────────────────────────────────────────────┤
│  Gateway API (services.py)                                       │
│  白名单透传: subagent_enabled, max_concurrent_subagents          │
├─────────────────────────────────────────────────────────────────┤
│  Lead Agent Factory (agent.py)                                   │
│  ├─ 读取 subagent_enabled → 决定是否注入 task 工具               │
│  ├─ 读取 max_concurrent → 配置 SubagentLimitMiddleware          │
│  └─ 调用 apply_prompt_template() 构建系统提示词                  │
├─────────────────────────────────────────────────────────────────┤
│  系统提示词 (prompt.py)                                          │
│  ├─ <skill_system>   → 列出可用 skill，指引渐进式加载            │
│  ├─ <soul>           → Agent 人格（SOUL.md）                    │
│  ├─ <subagent_system>→ 子代理编排指令（仅 subagent_enabled）    │
│  └─ <memory>         → 用户记忆上下文                            │
├─────────────────────────────────────────────────────────────────┤
│  LLM 决策                                                       │
│  ├─ 选择 task() → 派发子代理（并行/串行）                        │
│  ├─ 选择 read_file(SKILL.md) → 加载 skill 方法论                │
│  ├─ 选择 web_search/web_fetch → 直接调研                        │
│  └─ 用户输入 /skill-name → SkillActivationMiddleware 注入       │
├─────────────────────────────────────────────────────────────────┤
│  SubagentLimitMiddleware                                        │
│  └─ 截断超过 max_concurrent 的 task() 调用                       │
├─────────────────────────────────────────────────────────────────┤
│  SubagentExecutor                                               │
│  ├─ 线程池调度（3 workers）                                      │
│  ├─ 独立事件循环执行                                             │
│  ├─ 加载子代理专属 Skills                                        │
│  └─ 轮询 + SSE 事件推送结果                                      │
└─────────────────────────────────────────────────────────────────┘
```

**核心结论**：Subagent 和 Skill 是两套独立但可协同的系统：
- **Subagent** 通过 `task()` 工具实现并行任务分解
- **Skill** 通过三种路径（提示词注入、斜杠激活、read_file 自主加载）提供方法论指导
- **Agent SOUL.md** 是连接二者的桥梁——它定义 agent 人格并指导何时使用 skill 或 subagent

---

## Subagent 系统

### Subagent 配置层

#### SubagentConfig 数据结构

```python
# packages/harness/deerflow/subagents/config.py

@dataclass
class SubagentConfig:
    name: str                    # 唯一标识符
    description: str             # 描述（何时使用）
    system_prompt: str | None    # 子代理系统提示词
    tools: list[str] | None      # 允许的工具（None=全部）
    disallowed_tools: list[str]  # 禁止的工具（默认=["task"]，防止递归嵌套）
    skills: list[str] | None     # 技能白名单（None=全部, []=无, ["a","b"]=指定）
    model: str = "inherit"       # 模型（"inherit"=继承父代理）
    max_turns: int = 50          # 最大轮次
    timeout_seconds: int = 900   # 超时（秒）
```

#### 配置来源与优先级

```
config.yaml
├── subagents.timeout_seconds        # 全局默认超时（仅对 built-in 生效）
├── subagents.max_turns              # 全局默认最大轮次（仅对 built-in 生效）
├── subagents.agents.<name>:         # 按名称的覆盖配置
│   ├── timeout_seconds              #   优先级高于全局默认
│   ├── max_turns
│   ├── model
│   └── skills
└── subagents.custom_agents.<name>:  # 自定义子代理定义
    ├── description
    ├── system_prompt
    ├── tools / disallowed_tools
    ├── skills
    ├── model
    ├── max_turns
    └── timeout_seconds
```

#### Agent 专属配置（config.yaml）

```yaml
# backend/.deer-flow/agents/clinical-medicine/config.yaml
name: clinical-medicine
model: gpt-5.4
tool_groups:
  - web
  - file:read
  - file:write
  - bash
allowed_subagents:            # 该 agent 可用的子代理白名单
  - parkinson-clinical
  - trial-design
  - literature-analyzer
  - data-extractor
  - report-writer
```

`allowed_subagents` 控制两件事：
1. 系统提示词中 `<subagent_system>` 只列出这些子代理
2. 前端 UI 只显示这些可选子代理类型

---

### Subagent 注册与发现

```
registry.py
│
├── BUILTIN_SUBAGENTS (builtins/__init__.py)
│   ├── general-purpose  (max_turns=150, timeout=900s)
│   ├── bash             (max_turns=60,  timeout=900s)
│   └── 20+ 临床团队子代理:
│       parkinson-clinical, trial-design, literature-analyzer,
│       data-extractor, report-writer, cmo-gpl, gpm,
│       trial-statistics, data-management, drug-registration,
│       pharmacology, toxicology, chemistry, bioinformatics,
│       clinical-ops, quality-control, sci-ppt-generator, ...
│
├── get_subagent_config(name) → SubagentConfig | None
│   解析顺序:
│   1. BUILTIN_SUBAGENTS.get(name)
│   2. _build_custom_subagent_config(name) — 从 config.yaml custom_agents
│   3. 应用 per-agent overrides (timeout, max_turns, model, skills)
│
├── get_subagent_names() → list[str]
│   BUILTIN keys + config.yaml custom_agents keys
│
└── get_available_subagent_names() → list[str]
    过滤: 如果 host bash 不允许，移除 "bash"
```

---

### Subagent 工具注入

```python
# packages/harness/deerflow/tools/tools.py

SUBAGENT_TOOLS = [task_tool]  # task_status_tool 已不再暴露给 LLM

def get_available_tools(
    groups=None, include_mcp=True, model_name=None,
    subagent_enabled=False,  # ← 关键开关
    *, app_config=None
) -> list[BaseTool]:
    # ...
    if subagent_enabled:
        builtin_tools.extend(SUBAGENT_TOOLS)  # 添加 task 工具
    # ...
```

**调用链**：
1. `_make_lead_agent()` 从 `config.configurable` 读取 `subagent_enabled`
2. 调用 `get_available_tools(subagent_enabled=True/False, ...)`
3. 如果是子代理内部调用 → `subagent_enabled=False`（防止递归嵌套）

---

### Subagent 运行时启用开关

```
用户选择 "Ultra" 模式
    │
    ▼
前端 hooks.ts:
    subagent_enabled: context.mode === "ultra"
    │
    ▼
Gateway services.py:
    _CONTEXT_CONFIGURABLE_KEYS 包含 "subagent_enabled", "max_concurrent_subagents"
    merge_run_context_overrides() → config['configurable'] + config['context']
    │
    ▼
Lead Agent agent.py:
    subagent_enabled = cfg.get("subagent_enabled", False)
    │
    ├── True  → 添加 task 工具 + SubagentLimitMiddleware + 提示词注入
    └── False → 无 task 工具, 无中间件, 无提示词（IM 渠道默认 False）
```

**模式映射**：

| 模式 | thinking_enabled | is_plan_mode | subagent_enabled |
|------|-----------------|--------------|------------------|
| Flash | false | false | false |
| Thinking | true | false | false |
| Pro | true | true | false |
| Ultra | true | true | **true** |

---

### Subagent 提示词注入

```python
# packages/harness/deerflow/agents/lead_agent/prompt.py

def apply_prompt_template(subagent_enabled=False, max_concurrent_subagents=3, ...):
    if subagent_enabled:
        subagent_section = _build_subagent_section(n, ...)
        subagent_reminder = "- **Orchestrator Mode**: ..."
        subagent_thinking = "- **DECOMPOSITION CHECK**: ..."
```

`_build_subagent_section()` 生成 `<subagent_system>` 块，包含：

1. **角色定义**：DECOMPOSE → DELEGATE → SYNTHESIZE
2. **并发限制**：`MAXIMUM {n} task CALLS PER RESPONSE`
3. **可用子代理列表**：
   - 如果 `allowed_subagents` 指定 → 只列白名单中的
   - 否则 → 动态列出所有已注册子代理
4. **编排策略**：
   - 并行执行示例（单批/多批）
   - 执行决策矩阵（何时用/不用 subagent）
   - 多批次工作流（count → plan batches → execute → repeat → synthesize）
5. **直接执行回退**：无法分解为 2+ 并行子任务时直接执行

**关键**：当 `allowed_subagents` 非空时，`_build_available_subagents_text()` 只列出白名单中的子代理。这会影响 LLM 的子代理选择范围。

---

### Subagent 中间件：并发限制

```python
# packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py

MAX_CONCURRENT_SUBAGENTS = 3  # 来自 executor.py
MIN_SUBAGENT_LIMIT = 2
MAX_SUBAGENT_LIMIT = 4

class SubagentLimitMiddleware(AgentMiddleware):
    def __init__(self, max_concurrent=3):
        self.max_concurrent = _clamp_subagent_limit(max_concurrent)  # [2, 4]

    def after_model(self, state, runtime):
        # 统计 AIMessage 中的 task 工具调用数
        # 如果超过 max_concurrent → 截断多余的
        task_indices = [i for i, tc in enumerate(tool_calls) if tc["name"] == "task"]
        if len(task_indices) > self.max_concurrent:
            # 只保留前 N 个，其余丢弃
            truncated = [tc for i, tc in enumerate(tool_calls) if i not in drop_indices]
            return {"messages": [cloned_aimessage_with_truncated_tool_calls]}
```

**中间件在链中的位置**（build_middlewares 中的注册顺序）：
```
... → SystemMessageCoalescingMiddleware
   → SubagentLimitMiddleware (仅在 subagent_enabled=true 时)
   → LoopDetectionMiddleware
   → ClarificationMiddleware (最后)
```

---

### task() 工具执行流程

```python
# packages/harness/deerflow/tools/builtins/task_tool.py

@tool("task")
async def task_tool(
    runtime, description, prompt, subagent_type, tool_call_id
) -> str:
    # 1. 解析子代理配置
    config = get_subagent_config(subagent_type)

    # 2. 构建工具列表（subagent_enabled=False 防递归）
    tools = get_available_tools(
        groups=parent_tool_groups,     # 继承父代理的工具组
        subagent_enabled=False,         # 子代理不可再派发子代理
        model_name=effective_model
    )

    # 3. 创建 SubagentExecutor
    executor = SubagentExecutor(
        config=config, tools=tools, parent_model=...,
        sandbox_state=..., thread_data=..., thread_id=...,
        trace_id=..., user_id=..., user_role=...
    )

    # 4. 启动后台执行（异步，不阻塞）
    task_id = executor.execute_async(prompt, task_id=tool_call_id)

    # 5. 轮询等待结果（每 5 秒）
    while True:
        result = get_background_task_result(task_id)
        if result.status == COMPLETED:
            return f"Task Succeeded. Result: {result.result}"
        elif result.status in (FAILED, TIMED_OUT, CANCELLED):
            # 返回错误信息
        await asyncio.sleep(5)

    # 6. SSE 事件流
    writer({"type": "task_started", ...})    # 任务开始
    writer({"type": "task_running", ...})    # 流式输出
    writer({"type": "task_completed", ...})  # 任务完成
```

---

### SubagentExecutor 执行引擎

```python
# packages/harness/deerflow/subagents/executor.py

class SubagentExecutor:
    _scheduler_pool: ThreadPoolExecutor(max_workers=3)  # 调度池
    _isolated_subagent_loop: 持久事件循环                  # 执行循环

    def execute_async(self, prompt, task_id):
        # 1. 创建 SubagentResult 并存入 _background_tasks
        # 2. 提交到 _scheduler_pool 后台线程
        # 3. 在线程中调用 _submit_to_isolated_loop_in_context()
        #    在持久事件循环上执行 _aexecute()

    async def _aexecute(self, task):
        # 1. _build_initial_state(task)
        #    ├── _load_skills() → 加载子代理专属 skills
        #    ├── _apply_skill_allowed_tools() → 按 skill 过滤工具
        #    ├── _load_skill_messages() → 注入 skill 内容为 SystemMessage
        #    └── assemble_deferred_tools() → 组装延迟工具

        # 2. _create_agent() → 创建 LangChain agent
        #    ├── 中间件: runtime middlewares + SubagentLimitMiddleware(disabled)
        #    └── checkpointer=False (子代理不持久化)

        # 3. agent.astream() → 流式执行
        #    └── 检查 cancel_event → 协作式取消
```

**关键设计决策**：
- `max_workers=3`：最多 3 个子代理同时执行
- `checkpointer=False`：子代理是一次性的，不恢复
- `subagent_enabled=False`：防止无限递归嵌套
- 每个 `task()` 调用创建独立的 `SubagentExecutor` 实例

---

### 子代理中的 Skill 处理

```python
# executor.py

async def _load_skills(self) -> list[Skill]:
    if self.config.skills == []:     # 显式空列表 → 不加载
        return []
    all_skills = storage.load_skills(enabled_only=True)
    if self.config.skills is not None:  # 白名单过滤
        return [s for s in all_skills if s.name in allowed]
    return all_skills                   # None → 加载全部

def _apply_skill_allowed_tools(self, skills):
    # 按 skill 的 allowed_tools 过滤工具列表
    return filter_tools_by_skill_allowed_tools(self._base_tools, skills)

async def _load_skill_messages(self, skills):
    # 读取每个 skill 的 SKILL.md，注入为 SystemMessage
    for skill in skills:
        content = skill.skill_file.read_text()
        messages.append(SystemMessage(
            content=f'<skill name="{skill.name}">\n{content}\n</skill>'
        ))
```

---

## Skill 系统

### Skill 目录结构与发现

```
skills/
├── public/                          # 内置技能（只读，git 追踪）
│   ├── deep-research/
│   │   └── SKILL.md
│   ├── academic-paper-review/
│   │   └── SKILL.md
│   └── ...
├── custom/                          # 用户自定义技能（gitignore）
│   ├── .history/                    # 变更历史（JSONL）
│   └── my-skill/
│       └── SKILL.md
└── ...
```

**SKILL.md 格式**（YAML frontmatter + Markdown body）：

```markdown
---
name: deep-research
description: Use this skill instead of WebSearch for ANY question...
license: Apache-2.0
allowed-tools: [web_search, web_fetch, read_file]
---

# Deep Research Skill
## Overview
...
```

**发现流程**（`LocalSkillStorage.load_skills()`）：

```
1. os.walk(skills/public/) 和 os.walk(skills/custom/)
2. 找到所有 SKILL.md 文件
3. parse_skill_file() 解析 frontmatter → Skill 对象
4. 从 extensions_config.json 读取 enabled 状态
5. 按名称排序返回
```

**Skill 数据结构**：

```python
@dataclass
class Skill:
    name: str                    # 唯一名称（hyphen-case）
    description: str             # 描述
    license: str | None
    skill_dir: Path              # skill 目录路径
    skill_file: Path             # SKILL.md 文件路径
    category: SkillCategory      # PUBLIC 或 CUSTOM
    allowed_tools: list[str]     # 允许的工具白名单
    enabled: bool                # 是否启用
```

---

### Skill 启用状态管理

```json
// extensions_config.json（项目根目录）
{
    "mcpServers": {},
    "skills": {
        "academic-paper-review": { "enabled": true }
        // deep-research 未列出 → enabled=false
    }
}
```

**规则**：
- `extensions_config.json` 中 `enabled: true` → 启用
- 未列出 → `enabled=False`
- `LocalSkillStorage.load_skills()` 每次调用都重新读取 `extensions_config.json`（通过 `ExtensionsConfig.from_file()`），支持跨进程热更新
- Gateway API `PUT /api/skills/{name}` 可动态启用/禁用

---

### Skill 路径一：系统提示词注入

这是 skill 的**主要加载路径**，在 Lead Agent 初始化时执行。

```
prompt.py::apply_prompt_template()
    │
    ├── get_skills_prompt_section(available_skills)
    │   ├── get_enabled_skills_for_config()  → 加载所有启用的 skills
    │   ├── 过滤: 如果 available_skills 指定，只保留匹配的
    │   └── _get_cached_skills_prompt_section()  → 生成 XML
    │
    └── 注入到系统提示词的 <skill_system> 块
```

**生成的 `<skill_system>` 块结构**：

```xml
<skill_system>
You have access to skills that provide optimized workflows...

**Progressive Loading Pattern:**
1. When a user query matches a skill's use case, immediately call `read_file`
   on the skill's main file
2. Read and understand the skill's workflow
3. Follow the skill's instructions precisely

**Explicit Slash Skill Activation:**
- If the user starts a request with `/<skill-name>`, that skill was explicitly
  requested for the current turn.

**Skills are located at:** /mnt/skills

<available_skills>
    <skill>
        <name>academic-paper-review</name>
        <description>...</description>
        <location>/mnt/skills/public/academic-paper-review/SKILL.md</location>
    </skill>
    <!-- 更多 skills... -->
</available_skills>
</skill_system>
```

**缓存机制**：
- `@lru_cache(maxsize=32)` 缓存 `<skill_system>` 生成结果
- 当 `extensions_config.json` 变化时通过 `_invalidate_enabled_skills_cache()` 失效
- 后台守护线程异步刷新

**Agent SOUL.md 中 `available_skills` 的作用**：
- Agent config 可指定 `skills: [...]` 白名单
- 传给 `get_skills_prompt_section(available_skills=...)` 
- 只有白名单中的 skills 出现在系统提示词中

---

### Skill 路径二：斜杠命令激活

用户显式输入 `/skill-name task description` 时触发。

```
SkillActivationMiddleware.wrap_model_call()
    │
    ├── _find_activation_target()
    │   ├── 从最新的真实用户消息中查找 /skill-name 模式
    │   ├── parse_slash_skill_reference() → 正则匹配 ^/([a-z0-9]+(?:-[a-z0-9]+)*)
    │   └── 过滤保留命令: /new, /help, /bootstrap, /status, /models, /memory
    │
    ├── _resolve_activation()
    │   ├── 从 storage.load_skills() 查找 skill
    │   ├── 验证: skill 存在 + enabled + 在 available_skills 白名单中
    │   └── 读取 SKILL.md 内容 + 计算 SHA256
    │
    └── _build_activation_reminder()
        └── 生成隐藏 HumanMessage，包含:
            <slash_skill_activation>
              <user_request>task description</user_request>
              <skill name="..." sha256="...">
                <skill_content>
                  <!-- 完整的 SKILL.md 内容 -->
                </skill_content>
              </skill>
            </slash_skill_activation>

    → 该消息插入到用户消息之前，hide_from_ui=True
```

**关键约束**：
- Skill 必须在 `extensions_config.json` 中 `enabled: true`
- 在 Agent 白名单中（如果有）
- 不是保留命令
- 同一消息只激活一次（通过 `_has_existing_activation_for_target` 去重）

---

### Skill 路径三：Agent 自主 read_file 加载

Agent 可以在推理过程中**主动**用 `read_file` 工具读取 SKILL.md。

```
LLM 推理:
  "这是一个研究任务，我应该加载 deep-research skill"
    │
    ▼
read_file("/mnt/skills/public/deep-research/SKILL.md")
    │
    ▼
Agent 接收到完整的 SKILL.md 内容（作为 tool result）
    │
    ▼
Agent 按照 skill 中的方法论执行任务
```

**触发条件**：
1. 系统提示词中 `<skill_system>` 的 "Progressive Loading Pattern" 指引
2. Agent SOUL.md 中的指令（如 clinical-medicine 的 "调用 /deep-research 技能"）
3. Agent 自身的推理判断

**与路径二的区别**：

| 特性 | 路径二（斜杠激活） | 路径三（read_file） |
|------|-------------------|-------------------|
| 触发者 | 用户显式 `/skill-name` | Agent 自主决策 |
| 注入方式 | 中间件自动注入隐藏消息 | Agent 工具调用 + tool result |
| 内容位置 | 插入用户消息前的 HumanMessage | 作为 tool result 返回 |
| enabled 检查 | 严格（enabled + 白名单） | 无（直接读取文件系统） |
| 时机 | 模型调用前 | 模型推理过程中 |

**重要发现**：路径三**绕过**了 `extensions_config.json` 的 enabled 检查！只要文件存在于 `/mnt/skills/` 路径下，Agent 就可以读取。这意味着即使 skill 在配置中未启用，Agent 仍然可以通过 read_file 自主加载。

---

### Skill 路径四：子代理中的 Skill 加载

子代理通过 `SubagentExecutor._load_skills()` 加载 skill，与 Lead Agent 使用相同的存储层：

```
SubagentExecutor._build_initial_state()
    │
    ├── _load_skills()
    │   ├── 检查 config.skills:
    │   │   ├── [] → 空列表，不加载任何 skill
    │   │   ├── None → 加载所有 enabled skills
    │   │   └── ["a", "b"] → 只加载白名单中的
    │   └── storage.load_skills(enabled_only=True)
    │
    ├── _apply_skill_allowed_tools(skills)
    │   └── 按 skill 的 allowed_tools 过滤工具列表
    │
    └── _load_skill_messages(skills)
        └── 将每个 skill 的 SKILL.md 注入为 SystemMessage
            <skill name="...">
            <!-- 完整 SKILL.md 内容 -->
            </skill>
```

子代理中的 skill 加载是**同步**的（在 `_build_initial_state` 时完成），skill 内容作为 SystemMessage 注入到子代理的初始消息列表。

---

## 两条路径的交互与对比

### Subagent vs Skill：什么时候用哪个？

```
用户任务
    │
    ├── 广域调研/技术扫描
    │   └── SOUL.md → 使用 /deep-research skill
    │       └── deep-research SKILL.md → web_search/web_fetch 方法论
    │           （不使用 task() 子代理）
    │
    ├── PD 适应症临床问题
    │   └── SOUL.md → task(parkinson-clinical)
    │       （使用专用子代理）
    │
    ├── 竞品分析
    │   └── SOUL.md → 批次1: task(literature-analyzer) + task(data-extractor)
    │                → 批次2: task(trial-design) + task(report-writer)
    │       （并行子代理编排）
    │
    └── 简单操作
        └── 直接执行（不用 subagent 也不用 skill）
```

### 关键区别

| 维度 | Subagent (task) | Skill |
|------|----------------|------|
| 本质 | 并行执行引擎 | 方法论指导 |
| 工具 | `task()` 工具调用 | `read_file()` 或中间件注入 |
| 上下文 | 独立上下文，新的事件循环 | 共享主代理上下文 |
| 并发 | 最多 3 个并行（中间件限制） | 取决于 skill 方法论 |
| 工具集 | 可配置过滤 | `allowed_tools` 白名单 |
| 状态 | 一次性，不持久化 | 无状态 |
| 适用场景 | 可分解的独立子任务 | 需要方法论指导的复杂流程 |

---

## 实战案例：clinical-medicine 对话分析

### 案例背景

- Thread ID: `d41fe65d-1046-4b59-acb7-ffaaf5e1adb1`
- Agent: `clinical-medicine`
- 模式: `Ultra` (subagent_enabled=true)
- 任务: 研究 NVIDIA BioNeMo Agent Toolkit

### 实际执行路径

```
用户提问（中文，关于 BioNeMo）
    │
    ▼
Lead Agent 构建系统提示词:
    <skill_system> → academic-paper-review (唯一启用的 skill)
    <soul> → clinical-medicine SOUL.md
    <subagent_system> → 子代理编排指令（max 3 concurrent）
    │
    ▼
LLM 决策（基于 SOUL.md 决策树）:
    任务类型? → 广域调研/技术扫描
    SOUL.md 指令:
      "直接调用 /deep-research 技能，禁止主代理自行串行搜索"
    │
    ▼
LLM 执行:
    read_file("/mnt/skills/public/deep-research/SKILL.md")
    → 成功加载 deep-research 方法论
    │
    ▼
LLM 按 deep-research 方法论执行:
    Phase 1: web_fetch(NVIDIA blog) ← 初始调查
    Phase 2: 3× parallel (web_fetch + web_search×2) ← 广域探索
    Phase 3: 3× parallel (web_fetch×2 + web_search) ← GitHub 深潜
    ...共 6 轮，16 次工具调用
    Phase 4: 综合报告（14,803 字）
    │
    ▼
结果: task() 工具零调用
```

### 为什么 subagent 并行没有触发？

**直接原因**：deep-research skill 的方法论使用 `web_search`/`web_fetch` 直接工具，不涉及 `task()` 子代理调用。

**根本原因链**：
1. clinical-medicine SOUL.md 将任务路由到 "使用 /deep-research 技能"
2. deep-research SKILL.md 是方法论技能，不是编排技能
3. 方法论指示 Agent 自己执行多阶段 web 搜索
4. Agent 忠实地遵循方法论，未使用 task()

**如果希望触发 subagent**，需要：
- 修改 deep-research SKILL.md，加入子代理编排指令
- 或在 SOUL.md 中区分「需子代理并行」和「纯方法论指导」的场景

---

## 关键文件索引

### Subagent 系统

| 文件 | 作用 |
|------|------|
| `packages/harness/deerflow/subagents/config.py` | SubagentConfig 数据结构 + model 解析 |
| `packages/harness/deerflow/subagents/registry.py` | 子代理注册、发现、配置覆盖 |
| `packages/harness/deerflow/subagents/builtins/__init__.py` | 内置子代理注册（20+ 类型） |
| `packages/harness/deerflow/subagents/executor.py` | SubagentExecutor 执行引擎 |
| `packages/harness/deerflow/tools/tools.py` | `get_available_tools()` + SUBAGENT_TOOLS |
| `packages/harness/deerflow/tools/builtins/task_tool.py` | `task()` 工具实现 |
| `packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` | 并发限制中间件 |
| `packages/harness/deerflow/agents/lead_agent/agent.py` | `_make_lead_agent()` 工厂函数 |
| `packages/harness/deerflow/agents/lead_agent/prompt.py` | `_build_subagent_section()` 提示词生成 |
| `backend/app/gateway/services.py` | 运行时配置白名单透传 |
| `backend/app/channels/manager.py` | IM 渠道默认配置 |

### Skill 系统

| 文件 | 作用 |
|------|------|
| `packages/harness/deerflow/skills/types.py` | Skill 数据结构 + SkillCategory 枚举 |
| `packages/harness/deerflow/skills/__init__.py` | 公共 API |
| `packages/harness/deerflow/skills/storage/skill_storage.py` | SkillStorage 抽象基类 + `load_skills()` |
| `packages/harness/deerflow/skills/storage/local_skill_storage.py` | 本地文件系统实现 |
| `packages/harness/deerflow/skills/slash.py` | 斜杠命令解析 + 技能解析 |
| `packages/harness/deerflow/skills/parser.py` | SKILL.md frontmatter 解析 |
| `packages/harness/deerflow/skills/tool_policy.py` | Skill allowed_tools 过滤 |
| `packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py` | 斜杠激活中间件 |
| `packages/harness/deerflow/agents/lead_agent/prompt.py` | `get_skills_prompt_section()` 系统提示词注入 |
| `packages/harness/deerflow/subagents/executor.py` | 子代理中的 `_load_skills()` |
| `extensions_config.json` | Skill 启用状态配置 |

### Agent 配置

| 文件 | 作用 |
|------|------|
| `backend/.deer-flow/agents/<name>/config.yaml` | Agent 配置（model, tool_groups, allowed_subagents, skills） |
| `backend/.deer-flow/agents/<name>/SOUL.md` | Agent 人格定义 + 决策树 |
| `backend/config.yaml` | 全局配置（subagents, skills paths） |
| `extensions_config.json` | 扩展配置（MCP servers, skills enabled state） |
