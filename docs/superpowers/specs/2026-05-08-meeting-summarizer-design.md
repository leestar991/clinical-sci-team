# meeting-summarizer Agent 设计文档

**日期**：2026-05-08  
**状态**：已批准，待实施  
**范围**：`backend/.deer-flow/agents/meeting-summarizer/`

---

## 一、背景与目标

用户需要一个能够处理**系列学术会议**（ASCO、EHA、ASH、ESMO 等）的情报分析 agent。每次会议结束后，用户提供会议主题对应的 MD 文本材料和现场照片，agent 输出深度研究报告，并跨会议积累结构化知识库。

### 核心需求

1. 解析 MD 材料 + 现场照片（含图表识别）
2. 自动萃取靶点/通路/适应症关键词，并通过 `ask_clarification` 让用户确认后再执行检索
3. 穷尽检索同靶点/通道/适应症的临床管线、专利、文献、竞品
4. 生成深度 Markdown 报告 + 汇报 PPT
5. 维护两种知识库格式：时间线流水档 + 主题聚合图谱

---

## 二、架构方案选择

**选定方案：方案 A — 单层 orchestrator（第一期）**

`meeting-summarizer` 作为主协调器，复用系统现有子团队：`literature-analyzer`、`data-extractor`、`report-writer`、`sci-ppt-generator`。不新建子团队。

### 备选方案（记录，用于后续迭代）

**方案 B — 两层专职分工**（第二期演进方向）  
新建 `competitive-intel` 专职情报子 agent，封装所有靶点/专利/临床注册检索逻辑。适用场景：检索站点清单扩展、需要复用情报能力于其他 agent 时。

**方案 C — 三层流水线**（重型场景备选）  
`meeting-summarizer`（入口）→ `intel-researcher`（检索）→ `report-synthesizer`（写作）三个独立 agent，通过文件传递中间产物。适用场景：并发多会议处理、各层需独立扩展时。

---

## 三、组件设计

### 3.1 Agent 身份

- **角色**：学术会议情报分析协调器
- **模型**：`claude-sonnet-4-6`
- **工具组**：`web`、`file:read`、`file:write`、`bash`
- **允许子团队**：`literature-analyzer`、`data-extractor`、`report-writer`、`sci-ppt-generator`

### 3.2 五阶段执行协议

```
阶段1  材料摄取
       read_file（md材料）+ view_image（照片，含 sips 压缩预处理，>3MB 时触发）

阶段2  关键词萃取 → ask_clarification 确认
       自动提取：靶点 / 通路 / 适应症 / 化合物名
       用户确认/补充后锁定检索参数

阶段3  并行深度检索（≤3并发/批）
       批次1: literature-analyzer（PubMed/论文/会议摘要）
              data-extractor（ClinicalTrials.gov/专利局）
              web（MNC官网/年报/投资者材料）
       批次2: literature-analyzer（上市药品说明书/指南/真实世界数据）
              data-extractor（专利到期/失败案例数据）

阶段4  报告合成（≤3并发/批）
       批次1: report-writer（深度 Markdown 报告）
       批次2: sci-ppt-generator（汇报 PPT）

阶段5  知识库更新
       时间线档案：追加到 meeting-archive/timeline.md
       主题知识图谱：按靶点/适应症/通道合并更新 meeting-archive/topics/<topic>.md
```

### 3.3 知识库文件结构

```
/mnt/user-data/outputs/meeting-archive/
├── timeline.md                          # 按时间追加，每次会议一节
├── topics/
│   ├── target-<name>.md                 # 靶点维度，跨会议合并
│   ├── indication-<name>.md             # 适应症维度
│   └── pathway-<name>.md               # 通路维度
└── reports/
    ├── YYYY-MM-DD-<meeting>-report.md   # 单次深度报告
    └── YYYY-MM-DD-<meeting>-slides.pptx # 单次PPT
```

---

## 四、检索策略

### 4.1 检索站点优先级映射

| 检索目标 | 主要来源 | 执行方 |
|----------|----------|--------|
| 临床进度（IND/Ph1/2/3） | ClinicalTrials.gov | `data-extractor` |
| 专利信息 | Google Patents、USPTO、EPO、CNIPA | `data-extractor` |
| 论文/会议摘要 | PubMed、bioRxiv、会议官网 | `literature-analyzer` |
| MNC动态 | 官网新闻稿、年报、投资者材料 | web（直接） |
| 上市竞品标签/指南 | FDA/EMA药品标签、治疗指南 | `literature-analyzer` |
| 市场规模/流行病学 | 公开报告、学术综述 | `literature-analyzer` |
| 失败/成功案例 | 临床结果数据库、FDA审评报告 | `data-extractor` |

### 4.2 两阶段关键词确认协议

自动提取后，通过 `ask_clarification` 向用户呈现：

```
从材料中识别到以下研究对象，请确认或补充：

靶点：[自动识别列表]
通道/通路：[自动识别列表]
适应症：[自动识别列表]
化合物/药品名：[自动识别列表]

检索范围：国内 / 国外 / 仅临床阶段 / 含临床前
```

用户确认或修改后，锁定参数进入阶段3。

---

## 五、报告输出结构

```markdown
## [会议名称] 深度研究报告

### 一、核心论点与创新点
### 二、机制与原理解析
### 三、行业现状 / 数据 / 案例
### 四、竞品格局（同靶点 / 同通道 / 同适应症）
  #### 4.1 已上市竞品
  #### 4.2 临床阶段管线（穷尽列表，含Phase标注）
  #### 4.3 临床前/早期研究
### 五、专利全景
  #### 5.1 核心专利（含到期时间）
  #### 5.2 专利空白与机会窗口
### 六、市场规模与流行病学
### 七、风险信号（失败案例、黑框警告、撤市事件）
### 八、综合结论与战略建议
### 参考文献
```

---

## 六、错误处理

| 场景 | 处理方式 |
|------|----------|
| 图片 > 3MB | `sips` 压缩后再 `view_image`；失败则降级到文字材料 |
| 子团队返回空结果 | 标注"检索无结果"继续，不中断流程 |
| 检索站点不可达 | 记录在报告"数据局限"小节，提示用户手动补充 |
| 关键词确认超时 | 保持自动提取结果继续，报告末尾注明"关键词未经用户确认" |
| 知识库首次运行 | 自动初始化 `timeline.md` 和 `topics/` 目录 |
| 同一会议重复提交 | 检测到已有同名报告文件时，提示用户而非静默覆盖 |

---

## 七、行为约束

1. **禁止捏造数据**：所有竞品数据、专利号、临床注册号必须来自检索结果
2. **cite everywhere**：每条竞品/专利/临床数据必须附来源 URL 或文献引用
3. **穷尽声明**：检索受限时在报告中明确写"以下列表可能不完整，建议补充检索 [具体来源]"
4. **知识库幂等**：重复提交时不静默覆盖，主动提示用户
5. **最多3并发**：严格遵守系统 `MAX_CONCURRENT_SUBAGENTS = 3` 限制
6. **禁止手动设置 max_turns**：调用 `task()` 时不传 `max_turns`

---

## 八、文件清单

创建以下文件：

- `backend/.deer-flow/agents/meeting-summarizer/config.yaml`
- `backend/.deer-flow/agents/meeting-summarizer/SOUL.md`
- `backend/.deer-flow/agents/meeting-summarizer/memory.json`
