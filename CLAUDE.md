# NextTask — 企业级虚拟数字员工平台

本文件是 NextTask 项目的 Claude Code 主开发指南，涵盖平台架构、核心模块设计、开发规范和实现状态。所有代码变更必须保持本文档同步。

---

## 平台定位

NextTask 是一个**企业级虚拟数字员工平台**，将 AI Agent 技术从个人工具升级为可在企业内部规模化部署的数字员工基础设施。

核心设计目标：

| 目标 | 实现机制 |
|------|----------|
| **多租户隔离** | 三层身份模型（部门 / 用户 / Agent），全维度数据隔离 |
| **多层级权限** | 分层配置合并，Skills/MCP 最严格原则，Persona 继承覆盖 |
| **扩展记忆上下文** | OpenViking RAG 后端，跨会话语义知识库，本地 + OV 混合存储 |
| **虚拟专业团队** | 16 个领域专家 Sub-Agent，Lead Agent 动态调度并行协作 |
| **深度研究最佳实践** | 需求确认 → 文献摄入 → 多 Agent 并行分析 → 结构化报告生成 |

---

## 系统架构

```
┌───────────────────────────────────────────────────────────────────────┐
│                        NextTask 企业平台                               │
├───────────────────────────────────────────────────────────────────────┤
│                                                                       │
│   IM 渠道 / Web UI / Python Client                                    │
│       │                                                               │
│       ▼  config.configurable { dept_id, user_id, agent_name, ... }   │
│   ┌─────────────────────────────────────────────────────────────┐    │
│   │                    Identity 层                               │    │
│   │   AgentIdentity.from_config()  →  PersonaLoader.load()      │    │
│   │   三层路径解析 (Paths)  →  分层配置合并 (LayeredAppConfig)   │    │
│   └──────────────────────────┬──────────────────────────────────┘    │
│                              │                                        │
│   ┌──────────────────────────▼──────────────────────────────────┐    │
│   │                   Lead Agent (LangGraph)                     │    │
│   │                                                              │    │
│   │   MemoryMiddleware ──→ OVMemoryBackend / LocalMemory         │    │
│   │   SandboxMiddleware ──→ LocalWorkspace / MinIOWorkspace      │    │
│   │   Skills (分层加载 + 按需注入)                               │    │
│   │   MCP Tools (LayeredExtensions)                              │    │
│   └──────────────┬───────────────────────────────────────────────┘   │
│                  │  task() tool → SubagentExecutor                    │
│        ┌─────────┼─────────────────────────────────┐                 │
│        │         │                                  │                 │
│   [cmo-gpl]  [trial-design]  [pharmacology]   [literature-analyzer]  │
│   [gpm]      [trial-stats]   [toxicology]     [data-extractor]       │
│   [clinical-ops] [data-mgmt] [chemistry]      [ov-retriever]         │
│   [drug-reg] [bioinformatics][quality-ctrl]   [report-writing]       │
│        │         │                                  │                 │
│        └─────────┴──────────┬───────────────────────┘                │
│                             │                                         │
│   ┌─────────────────────────▼──────────────────────────────────┐     │
│   │                    OpenViking RAG 层                         │     │
│   │                                                              │     │
│   │   X-OV-Account (dept_id) / X-OV-User (user_id) /           │     │
│   │   X-OV-Agent (agent_name)                                   │     │
│   │                                                              │     │
│   │   语义检索 · 知识库索引 · 跨会话记忆持久化                   │     │
│   └──────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 服务架构

| 服务 | 端口 | 说明 |
|------|------|------|
| Nginx（统一入口） | 2026 | 反向代理，路由所有请求 |
| Frontend（Next.js） | 3000 | Web UI |
| LangGraph Server | 2024 | Agent 运行时 |
| Gateway API（FastAPI） | 8001 | REST API，模型/Skills/Memory/上传管理 |
| Provisioner（可选） | 8002 | K8s Pod sandbox 模式下启动 |

```
Nginx 路由规则：
  /api/langgraph/*  →  LangGraph (2024)
  /api/*            →  Gateway (8001)
  /                 →  Frontend (3000)
```

---

## 项目结构

```
nexttask/
├── CLAUDE.md                          ← 本文件（主开发指南）
├── README_zh.md                       ← 中文产品说明
├── config.yaml                        ← 全局主配置（模型/Sandbox/Memory/Summarization）
├── config.example.yaml                ← 配置模板
├── extensions_config.json             ← 全局 MCP servers + Skills 开关
├── Makefile                           ← 根目录命令 (check/install/dev/stop)
│
├── backend/
│   ├── CLAUDE.md                      ← 后端详细架构文档
│   ├── langgraph.json                 ← LangGraph agent 注册
│   ├── pyproject.toml
│   ├── Makefile                       ← 后端命令 (dev/gateway/test/lint)
│   │
│   ├── packages/harness/deerflow/     ← deerflow-harness 包 (import: deerflow.*)
│   │   │
│   │   ├── identity/                  ← 三层身份系统 [Phase 1-2]
│   │   │   ├── agent_identity.py      ← AgentIdentity 数据类 + OV header 生成
│   │   │   └── persona.py             ← PersonaLoader，分层 Persona 合并
│   │   │
│   │   ├── config/                    ← 配置系统
│   │   │   ├── app_config.py          ← AppConfig（全局）
│   │   │   ├── layered_config.py      ← LayeredAppConfig（身份分层合并）[Phase 3]
│   │   │   ├── layered_extensions.py  ← LayeredExtensionsConfig（Skills/MCP 分层）[Phase 6]
│   │   │   ├── paths.py               ← 路径计算（identity_*_dir 系列方法）
│   │   │   ├── memory_config.py       ← MemoryConfig（backend 字段：local/ov/ov+local）
│   │   │   ├── workspace_config.py    ← WorkspaceConfig（local/minio 后端选择）
│   │   │   └── agents_config.py       ← 预置 Agent 配置
│   │   │
│   │   ├── agents/
│   │   │   ├── lead_agent/
│   │   │   │   ├── agent.py           ← make_lead_agent()，identity 贯穿入口
│   │   │   │   └── prompt.py          ← apply_prompt_template()，注入 Persona + Memory
│   │   │   ├── memory/
│   │   │   │   ├── updater.py         ← LLM 记忆提取，原子写入
│   │   │   │   ├── queue.py           ← 防抖队列，per-thread 去重
│   │   │   │   ├── ov_backend.py      ← OVMemoryBackend（OpenViking HTTP API）[Phase 4]
│   │   │   │   └── prompt.py          ← 记忆提取 prompt 模板
│   │   │   ├── middlewares/           ← 12 个中间件（执行顺序见下文）
│   │   │   └── thread_state.py        ← ThreadState schema
│   │   │
│   │   ├── sandbox/
│   │   │   ├── sandbox.py             ← 抽象接口
│   │   │   ├── middleware.py          ← SandboxMiddleware + WorkspaceBackend 集成
│   │   │   ├── tools.py               ← bash/ls/read_file/write_file/str_replace
│   │   │   ├── local/                 ← LocalSandboxProvider
│   │   │   └── workspace/             ← [Phase 5]
│   │   │       ├── __init__.py        ← WorkspaceBackend 抽象接口
│   │   │       ├── local_backend.py   ← 本地文件系统后端（sync_down/sync_up）
│   │   │       └── minio_backend.py   ← MinIO 对象存储后端
│   │   │
│   │   ├── subagents/
│   │   │   ├── builtins/              ← 16 个内置 Sub-Agents
│   │   │   │   ├── # 通用
│   │   │   │   ├── general_purpose.py
│   │   │   │   ├── bash_agent.py
│   │   │   │   ├── literature_analyzer.py
│   │   │   │   ├── data_extractor.py
│   │   │   │   ├── report_writer.py
│   │   │   │   ├── ov_retriever.py
│   │   │   │   ├── # 虚拟临床开发团队
│   │   │   │   ├── cmo_gpl.py         ← 首席医学官 / 全球项目负责人
│   │   │   │   ├── gpm.py             ← 全球项目经理
│   │   │   │   ├── parkinson_clinical.py
│   │   │   │   ├── trial_design.py
│   │   │   │   ├── trial_statistics.py
│   │   │   │   ├── data_management.py
│   │   │   │   ├── drug_registration.py
│   │   │   │   ├── pharmacology.py
│   │   │   │   ├── toxicology.py
│   │   │   │   ├── chemistry.py
│   │   │   │   ├── bioinformatics.py
│   │   │   │   ├── clinical_ops.py
│   │   │   │   ├── quality_control.py
│   │   │   │   └── report_writing.py
│   │   │   ├── executor.py            ← 双线程池并发执行（max 3）
│   │   │   └── registry.py            ← Agent 注册表
│   │   │
│   │   ├── skills/loader.py           ← 分层 Skills 加载（identity 过滤）[Phase 6]
│   │   ├── tools/                     ← 工具组装（get_available_tools）
│   │   ├── mcp/                       ← MCP 集成（懒初始化 + mtime 缓存失效）
│   │   ├── models/factory.py          ← create_chat_model（反射 + thinking/vision 支持）
│   │   ├── community/                 ← tavily/jina/firecrawl/aio_sandbox
│   │   └── client.py                  ← DeerFlowClient（内嵌模式，无 HTTP）
│   │
│   ├── app/
│   │   ├── gateway/                   ← FastAPI Gateway
│   │   │   └── routers/
│   │   │       ├── identity.py        ← Identity API（部门/用户/Agent CRUD）[Phase 7]
│   │   │       └── ...                ← models/mcp/skills/memory/uploads/threads
│   │   └── channels/                  ← IM 渠道（Telegram/Slack/Feishu）
│   │
│   └── tests/                         ← 单元测试
│
├── frontend/                          ← Next.js 16 前端（NextTask UI）
├── skills/
│   ├── public/                        ← 内置 Skills
│   └── custom/                        ← 自定义 Skills（gitignored）
└── docs/
    ├── IDENTITY_ISOLATION.md          ← 身份隔离架构设计
    └── DEVELOPMENT_PLAN.md            ← 7 阶段开发路线图
```

---

## 模块一：三层身份隔离系统

### 身份模型

```
Department (dept_id)     ← 部门：共享模型配置、基础 Skills、基础 MCP
  └── User (user_id)     ← 用户：个人偏好、个人 Memory、个人 Skills
       └── Agent (agent_name)  ← Agent：专属 Persona、专属 Workspace
```

### AgentIdentity

**文件**：`deerflow/identity/agent_identity.py`

```python
@dataclass
class AgentIdentity:
    dept_id: str | None    # → X-OpenViking-Account
    user_id: str | None    # → X-OpenViking-User
    agent_name: str | None # → X-OpenViking-Agent

    # 从 LangGraph config 解析
    @classmethod
    def from_config(cls, config: RunnableConfig) -> "AgentIdentity": ...

    # OV header 生成
    @property
    def ov_headers(self) -> dict[str, str]: ...  # 三个 OV header 一次返回
```

ID 字段必须匹配 `[a-zA-Z0-9_.-]{1,64}`，否则拒绝构造。

### 传入方式

通过 LangGraph `config.configurable` 传入，向后兼容（缺省时退回全局）：

```python
config = RunnableConfig(configurable={
    "dept_id": "clinical",          # 部门
    "user_id": "alice",             # 用户
    "agent_name": "cmo-gpl",        # Agent
    "model_name": "gpt-4o",
    "thinking_enabled": True,
    "subagent_enabled": True,
})
```

### 路径体系

**文件**：`deerflow/config/paths.py`

| 方法 | 路径 |
|------|------|
| `identity_config_files(identity)` | 全局→部门→用户→Agent 的 `config.yaml` 路径列表 |
| `identity_extensions_dirs(identity)` | `extensions_config.json` 分层搜索目录 |
| `identity_memory_file(identity)` | `depts/{dept}/users/{user}/agents/{agent}/memory.json` |
| `identity_persona_dirs(identity)` | Persona 文件搜索目录（全局→部门→用户→Agent） |
| `identity_workspace_dir(identity)` | Agent 持久化 workspace 目录 |

---

## 模块二：Persona 系统

**文件**：`deerflow/identity/persona.py`

每个 Agent 的行为特征通过分层 Persona 文件定义，支持两种合并策略：

| 合并策略 | 文件 | 规则 |
|----------|------|------|
| **Override**（最细粒度胜） | `IDENTITY.md` / `SOUL.md` / `USER.md` / `AGENTS.md` | 最底层定义覆盖所有父层 |
| **Append**（层层叠加） | `BOOTSTRAP.md` / `HEARTBEAT.md` / `TOOLS.md` / `WORKFLOW_AUTO.md` / `ERRORS.md` / `LESSONS.md` | 各层内容用 `---` 拼接 |

Persona 文件目录层级（全局 → 部门 → 用户 → Agent），`PersonaLoader.load(identity)` 自动合并。

系统提示注入顺序：
```
<identity> → <user_context> → <bootstrap> → <tools_guidance>
→ <workflow> → <lessons> → <error_patterns> → <team_context> → <heartbeat>
```

---

## 模块三：分层配置合并

### LayeredAppConfig

**文件**：`deerflow/config/layered_config.py`

合并顺序：全局 `config.yaml` → 部门 → 用户 → Agent（后层覆盖前层）

合并规则：
- **Scalar**：后层直接覆盖
- **Dict**：递归深度合并
- **含 `name` 字段的 List**：按 `name` 去重合并，保留原始顺序

### LayeredExtensionsConfig

**文件**：`deerflow/config/layered_extensions.py`

| 资源类型 | 合并策略 |
|----------|----------|
| MCP Servers | 后层覆盖（url/headers 可被部门/用户/Agent 级覆盖） |
| Skills | **最严格原则**：任一层显式禁用 → 最终禁用 |

---

## 模块四：记忆系统（Memory + OpenViking）

### 后端类型

`MemoryConfig.backend` 支持三种模式：

| 模式 | 说明 |
|------|------|
| `local` | 本地 JSON 文件（默认），路径由 `identity_memory_file()` 确定 |
| `ov` | 纯 OpenViking 后端，使用 OV HTTP API 存取 |
| `ov+local` | 双写：本地作为 fallback，OV 作为持久化向量存储 |

### OVMemoryBackend

**文件**：`deerflow/agents/memory/ov_backend.py`

```python
class OVMemoryBackend:
    def __init__(self, ov_url: str, api_key: str | None, identity: AgentIdentity): ...

    def _headers(self) -> dict[str, str]:
        # 每次请求携带三层 OV 身份 header，实现完整的租户隔离
        return {
            "X-OpenViking-Account": identity.ov_account,  # dept_id
            "X-OpenViking-User":    identity.ov_user,     # user_id
            "X-OpenViking-Agent":   identity.ov_agent,    # agent_name
            "Authorization": f"Bearer {api_key}",
        }

    async def store_memory(self, messages: list[dict]) -> None: ...
    async def retrieve_memory(self, query: str, top_k: int = 15) -> list[dict]: ...
```

Agent-space 名称通过 `MD5(dept_id/user_id/agent_name)` 派生，与 OpenViking 内部哈希约定对齐。

### OpenViking 身份映射

| NextTask 层级 | OV HTTP Header | OV URI 命名空间 |
|--------------|----------------|----------------|
| `dept_id` | `X-OpenViking-Account` | `/local/{dept_id}/` |
| `user_id` | `X-OpenViking-User` | `viking://user/{user_id}/` |
| `agent_name` | `X-OpenViking-Agent` | `viking://agent/{space}/` |

### 记忆工作流

1. `MemoryMiddleware` 过滤（用户输入 + 最终 AI 响应），入队
2. 防抖队列（默认 30s），per-thread 去重
3. 后台 LLM 提取 facts，原子写入（temp file + rename）
4. 下次对话注入 Top 15 facts + User Context 摘要

### config.yaml 配置

```yaml
memory:
  enabled: true
  backend: "ov+local"           # local / ov / ov+local
  ov_url: $OPENVIKING_URL
  ov_api_key: $OPENVIKING_API_KEY
  storage_path: .deer-flow/memory.json
  debounce_seconds: 30
  max_facts: 100
  fact_confidence_threshold: 0.7
  injection_enabled: true
  max_injection_tokens: 2000
```

---

## 模块五：Workspace 隔离（持久化存储）

**文件**：`deerflow/sandbox/workspace/`

| 后端 | 适用场景 | 路径约定 |
|------|----------|----------|
| `LocalWorkspaceBackend` | 单机部署 | `depts/{dept}/users/{user}/agents/{agent}/workspace/` |
| `MinIOWorkspaceBackend` | 分布式/云部署 | `{bucket}/{prefix}/{dept}/{user}/{agent}/` |

每次任务开始前 `sync_down`（持久化 → 沙箱），结束后 `sync_up`（沙箱 → 持久化），实现跨 session 状态保持。

### config.yaml 配置

```yaml
workspace:
  backend: "local"              # local / minio
  # MinIO 模式
  minio:
    endpoint: "localhost:9000"
    bucket: "nexttask"
    access_key: $MINIO_ACCESS_KEY
    secret_key: $MINIO_SECRET_KEY
    secure: false
    prefix: "workspaces"
```

---

## 模块六：虚拟临床开发团队（Sub-Agents）

16 个内置专业 Sub-Agent，通过 `task()` 工具由 Lead Agent 动态调度，最多 3 个并行。

### 团队结构

**战略层**
- `cmo-gpl`：首席医学官 / 全球项目负责人 — 开发策略、获益-风险、阶段推进决策
- `gpm`：全球项目经理 — IDP 时间线、关键路径、风险登记册（CPM/PERT）

**临床科学层**
- `trial-design`：临床试验设计（ICH E6/E8/E9/E10，适应性设计，SPIRIT 格式）
- `parkinson-clinical`：帕金森病临床专家（MDS-UPDRS、α-synuclein、DaTscan）
- `trial-statistics`：临床统计学家（SAP、MMRM、多重性控制、O'Brien-Fleming）
- `bioinformatics`：生物信息学（NGS、CDx、BEST Framework、多组学）

**运营与质量层**
- `clinical-ops`：临床运营（RBM/ICH E6(R2)，CRO 管理，IMP 供应链）
- `quality-control`：质量与合规（GCP/GLP/GMP，CAPA，TMF，检查准备）
- `data-management`：临床数据管理（CDISC CDASH/SDTM/ADaM，EDC，MedDRA）

**药学与非临床层**
- `pharmacology`：药理学（PK/PD，PBPK，NONMEM/Monolix，DDI，IVIVE）
- `toxicology`：毒理学（GLP tox，ICH S 系列，NOAEL/MABEL，遗传毒性）
- `chemistry`：CMC（ICH Q 系列，CTD Module 3，分析方法验证，稳定性）
- `drug-registration`：法规事务（IND/NDA/BLA/MAA，FDA/EMA/NMPA，突破性疗法）

**知识管理层**
- `literature-analyzer`：深度文献分析（结构化解读，方法论评估）
- `data-extractor`：结构化数据提取（数值表格，跨研究对比）
- `ov-retriever`：OpenViking 语义检索（已索引知识库检索）
- `report-writing`：临床文件撰写（CSR/ICH E3，IB，监管简报）

### 典型多 Agent 协作场景

```python
# III 期方案设计
"请为 LRRK2-G2019S PD 人群设计一个 Phase 3 疾病修饰研究方案"
→ Lead Agent 拆解为并行任务：
  task(parkinson-clinical): 定义患者群体、终点、MCID
  task(trial-design):       方案结构、随机化、盲法设计
  task(trial-statistics):   样本量计算、SAP 框架
  task(gpm):                关键路径、IND 到 EOP2 里程碑
→ Lead Agent 汇总为完整方案框架

# IND 申报包准备
"准备 PD α-syn 抑制剂的 IND 申报摘要"
→ task(toxicology):        非临床安全研究设计，NOAEL 确定
  task(pharmacology):      FIH 剂量选择，PK 参数预测
  task(chemistry):         CTD Module 3 摘要，规格设置
  task(drug-registration): IND 结构，FDA 申报策略
```

---

## 模块七：深度研究最佳实践

基于 `sci-research-agent` 分支设计，内置深度科研工作流。

### 研究工作流（五阶段）

**Phase -1：需求确认与资料准备**
```
ask_clarification → 用户补充研究问题
用户上传 PDF/Word/PPT（UploadsMiddleware 自动注入）
Lead Agent 生成研究计划草案
ask_clarification → 用户确认/修改
```

**Phase 0：文献摄入**
```
ov add-resource <url/doi>   # 摄入文献到 OV 知识库
ov add-resource <file>      # 摄入本地文档
# 支持批量摄入（并行 Sub-Agent 加速）
```

**Phase 1：语义检索与初步筛选**
```
task(ov-retriever): ov find "<研究问题>" → 返回相关段落 + resource_id
# 多角度查询，覆盖不同切面
```

**Phase 2：深度并行分析**
```
# ov-retriever 召回候选文献后，并行分析（最多 3 个）
task(literature-analyzer): 单篇深度解读（研究设计、结论、局限性）
task(data-extractor):       提取数值数据，构建对比表格
task(literature-analyzer):  另一篇...
```

**Phase 3：综合与报告生成**
```
task(report-writing):  整合分析结果，生成结构化学术报告
# 报告类型：文献综述 / CSR / 系统综述 / 数据分析报告
```

### OpenViking 工具集

```bash
ov find "<query>"           # 语义检索，返回相关段落
ov read <resource_id>       # 获取完整文献内容
ov add-resource <url>       # 摄入网页/DOI/arXiv
ov add-resource <file>      # 摄入本地文件（PDF/Word/PPT）
ov list                     # 列出已索引资源
```

### SummarizationMiddleware 配置（科研场景推荐）

```yaml
summarization:
  enabled: true
  trigger:
    type: tokens
    threshold: 6000          # 科研文献场景建议 6000
  keep:
    type: count
    count: 10                # 保留最近 10 条消息
```

---

## 中间件执行顺序

```
1. ThreadDataMiddleware       → 创建 per-thread 目录结构
2. UploadsMiddleware          → 追踪并注入新上传文件
3. SandboxMiddleware          → 获取沙箱 + WorkspaceBackend sync_down
4. DanglingToolCallMiddleware → 修复无响应的 tool_calls（用户中断时）
5. GuardrailMiddleware        → 工具调用前置授权（可选）
6. SummarizationMiddleware    → 上下文超限时压缩（可选）
7. TodoListMiddleware         → Plan 模式任务追踪（可选）
8. TitleMiddleware            → 首次对话后自动生成 thread 标题
9. MemoryMiddleware           → 队列对话，触发异步记忆提取（OV 路由）
10. ViewImageMiddleware       → 注入 base64 图片（视觉模型）
11. SubagentLimitMiddleware   → 截断超过 MAX_CONCURRENT_SUBAGENTS(3) 的 task 调用
12. ClarificationMiddleware   → 拦截 ask_clarification，中断等待用户输入（最后执行）
```

---

## 快速开始

### 安装

```bash
make config     # 生成本地配置文件
# 编辑 config.yaml，配置模型
# 编辑 .env，设置 API keys
make install    # 安装 backend + frontend 依赖
```

### 启动

```bash
# Docker（推荐）
make docker-init && make docker-start

# 本地开发
make dev
```

访问：http://localhost:2026

### 关键环境变量

```bash
# 基础模型
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...

# 网络搜索
TAVILY_API_KEY=...

# OpenViking（记忆/知识库）
OPENVIKING_URL=http://localhost:8080
OPENVIKING_API_KEY=...

# 分布式存储（可选）
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...

# IM 渠道（可选）
TELEGRAM_BOT_TOKEN=...
SLACK_BOT_TOKEN=...
SLACK_APP_TOKEN=...
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...

# 链路追踪（可选）
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=nexttask
```

---

## config.yaml 完整结构参考

```yaml
# 模型配置
models:
  - name: gpt-4o
    display_name: GPT-4o
    use: langchain_openai:ChatOpenAI
    model: gpt-4o
    api_key: $OPENAI_API_KEY
    max_tokens: 4096
    supports_vision: true
    supports_thinking: false

# Sandbox 模式
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider   # Docker 模式
  # use: deerflow.sandbox.local:LocalSandboxProvider       # 本地模式

# Workspace 持久化
workspace:
  backend: local              # local / minio

# 记忆系统
memory:
  enabled: true
  backend: "ov+local"         # local / ov / ov+local
  ov_url: $OPENVIKING_URL
  ov_api_key: $OPENVIKING_API_KEY
  debounce_seconds: 30
  max_facts: 100

# 上下文压缩
summarization:
  enabled: true
  trigger:
    type: tokens
    threshold: 6000

# Sub-Agent 并发
subagents:
  enabled: true               # 开启虚拟团队协作

# IM 渠道
channels:
  langgraph_url: http://localhost:2024
  gateway_url: http://localhost:8001
  session:
    context:
      thinking_enabled: true
      subagent_enabled: true
  telegram:
    enabled: false
    bot_token: $TELEGRAM_BOT_TOKEN
  slack:
    enabled: false
    bot_token: $SLACK_BOT_TOKEN
    app_token: $SLACK_APP_TOKEN
  feishu:
    enabled: false
    app_id: $FEISHU_APP_ID
    app_secret: $FEISHU_APP_SECRET
```

---

## 测试

```bash
# 运行所有测试
make test

# 运行特定测试
cd backend && PYTHONPATH=. uv run pytest tests/test_identity.py -v
cd backend && PYTHONPATH=. uv run pytest tests/test_layered_config.py -v
cd backend && PYTHONPATH=. uv run pytest tests/test_layered_skills.py -v
cd backend && PYTHONPATH=. uv run pytest tests/test_workspace_backend.py -v
cd backend && PYTHONPATH=. uv run pytest tests/test_client.py -v
```

**关键测试文件**：

| 测试文件 | 覆盖范围 |
|----------|----------|
| `test_identity.py` | AgentIdentity 验证、OV header 生成 |
| `test_identity_gateway.py` | Identity API 端点 |
| `test_layered_config.py` | 分层配置深度合并逻辑 |
| `test_layered_skills.py` | Skills 最严格原则合并 |
| `test_user_isolation.py` | 用户级数据隔离验证 |
| `test_workspace_backend.py` | Local/MinIO workspace sync |
| `test_client.py` | DeerFlowClient + Gateway 一致性（77 个测试） |
| `test_memory_updater.py` | 记忆提取和去重 |
| `test_subagent_executor.py` | Sub-Agent 并发执行 |

---

## 开发规范

### 强制要求

1. **TDD**：每个新功能或 bugfix 必须配套单元测试
2. **文档同步**：代码变更后立即更新 `CLAUDE.md` 和 `README_zh.md`
3. **Harness 边界**：`deerflow.*` 禁止 import `app.*`（CI 强制校验）
4. **Identity 贯穿**：所有涉及 Memory/Skills/Workspace 的新功能必须接受 `identity` 参数

### 代码风格

- Linter：`ruff`，行长 240 字符
- Python 3.12+，类型注解
- 双引号，空格缩进

### 架构约束

- Harness（`deerflow.*`）是可发布包，不依赖 `app.*`
- App（`app.*`）依赖 Harness，不反向
- 新增 Sub-Agent 必须在 `subagents/builtins/__init__.py` 注册
- 分层配置合并使用 `_deep_merge()`，禁止手动字典操作

---

## 实现路线图

| 阶段 | 功能 | 状态 |
|------|------|------|
| Phase 1 | AgentIdentity + 路径扩展 + Lead Agent 接入 | ✅ 完成 |
| Phase 2 | Persona 系统（PersonaLoader + 分层文件合并） | ✅ 完成 |
| Phase 3 | 分层配置合并（LayeredAppConfig + LayeredExtensions） | ✅ 完成 |
| Phase 4 | Memory 隔离（identity 路径 + OVMemoryBackend） | ✅ 完成 |
| Phase 5 | Workspace 隔离（LocalBackend + MinIOBackend） | ✅ 完成 |
| Phase 6 | Skills/MCP 分层加载（最严格原则） | ✅ 完成 |
| Phase 7 | Identity Gateway API + 前端多租户 UI | 📋 待开发 |
| 深度研究 | OpenViking 知识库工作流 + sci-research Skill | 🔄 进行中 |
| 企业功能 | SSO 集成、审计日志、配额管理 | 📋 待规划 |

---

## 致谢

NextTask 基于以下开源项目构建：

- **[LangGraph](https://github.com/langchain-ai/langgraph)**：多 Agent 编排与工作流
- **[LangChain](https://github.com/langchain-ai/langchain)**：LLM 交互与工具集成
