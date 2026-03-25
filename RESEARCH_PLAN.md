# deer-sci-agent 研发计划

> Fork 自 [bytedance/deer-flow](https://github.com/bytedance/deer-flow)
> 仓库地址：https://github.com/leestar991/deer-sci-agent
> 开发分支：`feature/sci-research-agent`
> 计划创建日期：2026-03-25

---

## 项目目标

在 DeerFlow 超级代理框架基础上，构建面向**专业科学研究**场景的智能 Agent 系统，核心能力：

- 大规模学术文献的摄入、索引与语义检索（集成 OpenViking RAG）
- 多子代理并行的深度文献分析与数据提取
- 结构化科研报告自动生成（文献综述、实验分析、论文草稿）
- 跨会话知识积累，支持长期科研项目管理

---

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                  deer-sci-agent 系统架构                     │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   用户 ──→ Lead Agent (sci-research Skill)                  │
│                    │                                        │
│      ┌─────────────┼──────────────────┐                    │
│      │             │                  │                     │
│  OpenViking    Web Research      并行子代理 (≤3)            │
│  RAG 层        深度搜索                                      │
│                                                             │
│  ov find       tavily/jina      literature-analyzer         │
│  ov read       arxiv/doi        data-extractor              │
│  ov add-res    web_fetch        report-writer               │
│                                 ov-retriever                │
│      │             │                  │                     │
│      └─────────────┴──────────────────┘                    │
│                    │                                        │
│         SummarizationMiddleware (6000 tokens)               │
│         MemoryMiddleware (300 facts)                        │
│                    │                                        │
│         Markdown 学术报告 + 图表可视化                       │
└─────────────────────────────────────────────────────────────┘
```

**双引擎分工**：
- **DeerFlow**：工作流编排、子代理并发、文档转换、上下文压缩、长期记忆
- **OpenViking**：文献向量索引（Doubao Embedding 1024维）、语义检索、跨会话知识库、VLM 图表理解

---

## 新增文件结构

```
deer-sci-agent/
├── RESEARCH_PLAN.md                              ← 本文件
├── config.yaml                                   ← 调整摘要/记忆参数
│
├── skills/custom/sci-research/
│   ├── SKILL.md                                  ← 主 Skill（五阶段工作流）
│   ├── agents/
│   │   ├── literature-analyzer.md                ← 子代理 prompt：精读论文
│   │   ├── data-extractor.md                     ← 子代理 prompt：结构化数据提取
│   │   ├── report-writer.md                      ← 子代理 prompt：章节写作
│   │   └── ov-retriever.md                       ← 子代理 prompt：OV 语义检索
│   └── templates/
│       ├── research-report.md                    ← 科研报告结构模板
│       ├── literature-review.md                  ← 文献综述模板
│       └── citation-formats.md                   ← 引用格式规范（APA/GB/IEEE）
│
└── backend/packages/harness/deerflow/
    └── subagents/builtins/
        ├── literature_analyzer.py                ← 注册专用子代理（DeepSeek-R1）
        ├── data_extractor.py                     ← 注册专用子代理（Claude 3.5）
        ├── report_writer.py                      ← 注册专用子代理（GPT-4o）
        ├── ov_retriever.py                       ← 注册专用子代理（Doubao-lite）
        └── __init__.py                           ← 更新注册表
```

---

## 研发阶段

### Phase 0：基础设施准备（第 1 周）

**目标**：确保 OpenViking 与 DeerFlow 可互通，子代理扩展机制就绪。

| 任务 | 说明 | 负责层 |
|------|------|--------|
| 0-1 | 验证 OpenViking 服务启动（`ov ls` 可用） | 环境 |
| 0-2 | 确认 `ov add-resource` 可索引 PDF 转换后的 Markdown | 集成测试 |
| 0-3 | 扩展 `task_tool.py` 的 `subagent_type` Literal，支持 4 个新类型 | 核心代码 |
| 0-4 | 创建 4 个子代理 Python 配置文件并注册到 `BUILTIN_SUBAGENTS` | 核心代码 |
| 0-5 | 调整 `config.yaml` 摘要/记忆参数 | 配置 |
| 0-6 | 编写单元测试覆盖新子代理注册逻辑 | 测试 |

**验收标准**：`make test` 全部通过，`task(subagent_type="literature-analyzer")` 可正常调用。

---

### Phase 1：文献摄入与索引（第 2 周）

**目标**：用户上传 PDF/URL，自动完成格式转换 + OpenViking 向量索引。

| 任务 | 说明 |
|------|------|
| 1-1 | 创建 `sci-research` Skill 主文件（Phase 0 入库流程） |
| 1-2 | 实现上传后自动调用 `ov add-resource` 的 Skill 指令 |
| 1-3 | 支持批量 URL 摄入（arXiv、DOI、PubMed） |
| 1-4 | 创建 `ov-retriever` 子代理，封装 `ov find` + `ov read` 工作流 |
| 1-5 | 测试：上传 10 篇 PDF，验证语义检索准确率 |

**验收标准**：上传 PDF → 30 秒内可通过 `ov find` 语义检索到相关段落。

---

### Phase 2：深度文献分析（第 3 周）

**目标**：`literature-analyzer` 子代理对单篇论文做结构化精读。

| 任务 | 说明 |
|------|------|
| 2-1 | 编写 `literature-analyzer.md` 子代理 prompt（精读模板） |
| 2-2 | 配置该子代理使用 DeepSeek-R1 模型（强推理） |
| 2-3 | 定义标准化输出格式（研究问题/方法/发现/局限/差异点） |
| 2-4 | 编写 `data-extractor.md`，专注提取数值数据和对比表格 |
| 2-5 | 配置 `data-extractor` 使用 Claude 3.5 Sonnet（结构化精度） |
| 2-6 | 测试：对 5 篇论文并行分析，验证输出一致性 |

**验收标准**：单篇论文分析输出包含所有 5 个结构化字段，数据提取准确率 ≥ 90%。

---

### Phase 3：跨文献综合（第 4 周）

**目标**：整合多篇分析结果，识别共识、分歧与研究空白。

| 任务 | 说明 |
|------|------|
| 3-1 | 设计跨文献综合的 Skill 工作流（Phase 3 逻辑） |
| 3-2 | 利用 `ov find` 做主题聚类检索（相似研究归组） |
| 3-3 | Lead Agent 整合子代理输出，生成 Gap Analysis |
| 3-4 | 集成 `chart-visualization` Skill 生成对比图表 |
| 3-5 | 测试：10 篇文献综合，验证 Gap Analysis 质量 |

**验收标准**：Gap Analysis 包含至少 3 个有据可查的研究空白，每个空白有对应文献支撑。

---

### Phase 4：报告写作（第 5 周）

**目标**：`report-writer` 子代理生成各章节，Lead Agent 整合为完整报告。

| 任务 | 说明 |
|------|------|
| 4-1 | 创建报告结构模板（`research-report.md`） |
| 4-2 | 编写 `report-writer.md` 子代理 prompt（学术写作规范） |
| 4-3 | 配置 `report-writer` 使用 GPT-4o（写作质量） |
| 4-4 | 实现并行章节写作：方法论分析、文献综述、数据对比（3 子代理并发） |
| 4-5 | Lead Agent 串行完成：引言、讨论、结论（需全局视角） |
| 4-6 | 集成引用格式规范（APA/GB/IEEE 自动格式化） |
| 4-7 | 测试：端到端生成一篇完整文献综述报告 |

**验收标准**：生成报告包含完整 6 章结构，引用格式正确，图表嵌入正常。

---

### Phase 5：集成测试与优化（第 6 周）

**目标**：端到端场景测试，性能调优，文档完善。

| 任务 | 说明 |
|------|------|
| 5-1 | 端到端测试场景 A：20 篇文献 → 完整综述报告 |
| 5-2 | 端到端测试场景 B：指定研究方向 → 网络检索 + 报告 |
| 5-3 | 性能优化：OpenViking 检索延迟、子代理并发调度 |
| 5-4 | 成本优化：轻量模型替换高频低价值调用 |
| 5-5 | 完善 README、使用文档、配置说明 |
| 5-6 | 提交 PR 到 `main` 分支 |

---

## 子代理模型分配

| 子代理名称 | 模型 | 工具权限 | 最大轮次 | 超时 |
|-----------|------|---------|---------|------|
| `literature-analyzer` | DeepSeek-R1 | bash, read_file, tavily_web_search | 30 | 600s |
| `data-extractor` | claude-3-5-sonnet | bash, read_file, str_replace | 20 | 300s |
| `report-writer` | gpt-4o | bash, read_file, write_file, str_replace | 40 | 900s |
| `ov-retriever` | doubao-lite（或 gpt-4o-mini） | bash | 15 | 180s |
| Lead Agent | gpt-4o / claude-sonnet | 全部（含 task） | — | — |

---

## 关键配置变更（config.yaml）

```yaml
summarization:
  enabled: true
  trigger:
    - type: tokens
      value: 6000      # 论文分析需要更大窗口（原 4000）
    - type: messages
      value: 80        # 原 50
  keep:
    type: tokens
    value: 3000        # 原 messages: 20

memory:
  enabled: true
  max_facts: 300       # 原 100，扩大文献知识点存储
  fact_confidence_threshold: 0.6   # 原 0.7，科研事实容忍更低置信度
  max_injection_tokens: 3000       # 原 2000

subagents:
  enabled: true
  timeout_seconds: 900
  agents:
    literature-analyzer:
      timeout_seconds: 600
    data-extractor:
      timeout_seconds: 300
    ov-retriever:
      timeout_seconds: 180
```

---

## 里程碑

| 里程碑 | 目标日期 | 交付物 |
|--------|---------|--------|
| M0：基础设施就绪 | 2026-04-01 | 子代理注册完成，测试通过 |
| M1：文献摄入可用 | 2026-04-08 | PDF 上传 → OV 索引 → 语义检索 |
| M2：单篇分析可用 | 2026-04-15 | literature-analyzer 输出标准化 |
| M3：综合分析可用 | 2026-04-22 | Gap Analysis 生成 |
| M4：报告写作可用 | 2026-04-29 | 完整报告端到端生成 |
| M5：正式发布 | 2026-05-06 | PR 合并，文档完善 |

---

## 技术债务与风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| OpenViking 服务不稳定 | 检索失败 | 降级到纯 web_search 模式 |
| 子代理并发上限 3 个 | 大批量文献处理慢 | 分批调度，Lead Agent 管理队列 |
| 长报告超出模型上下文 | 写作截断 | 分章节写作 + SummarizationMiddleware |
| 模型 API 成本 | 超预算 | 轻量模型承担高频任务（ov-retriever） |
| 上游 deer-flow 更新冲突 | 合并困难 | 定期 `git fetch upstream` 同步 |

---

## 上游同步策略

```bash
# 定期同步上游更新
git fetch upstream
git checkout main
git merge upstream/main
git push origin main

# 将上游更新 rebase 到开发分支
git checkout feature/sci-research-agent
git rebase main
```

---

*本文档随研发进展持续更新。*
