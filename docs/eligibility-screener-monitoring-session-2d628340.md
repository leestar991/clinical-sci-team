# 会话 2d628340 执行分析：判定阶段 token 爆炸与门禁死循环

> 会话：`http://localhost:3000/workspace/agents/eligibility-screener/chats/2d628340-ddd4-4588-ba14-21287b35d98e`
>
> thread_id：`2d628340-ddd4-4588-ba14-21287b35d98e`
>
> 分析时间：2026-08-09（CST）
>
> 数据来源：Postgres `run_events` / `runs` 表（`DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/deerflow`），磁盘 workspace 产物
>
> 状态：**仅分析记录，未修改任何代码/配置**。

---

## 0. 结论

该会话对患者 **M019（LHSH）** 跑完整入排筛查流水线，总耗时约 **33 分钟**（12:34–13:12 UTC），真实消耗约 **17.9M token**，全部来自单一模型 `deepseek-v4-pro`。核心问题集中在 **判定阶段**（task 6/7）独占 8.25M token / 18.5 分钟，根因是 `uncertain_recheck` / `check_reason_alignment` 门禁脚本的**误报匹配 + 无熔断重试循环**。

六个独立异常：

1. **判定阶段门禁死循环**（最大问题）：`uncertain_recheck.py` 跑 12 次、`check_reason_alignment.py` 跑 7 次，agent 反复"跑门禁 → 发现 `suspected_missed` → grep 排查 → 改 JSON → 重跑"，单 task（IN 判定）达 75 个 AI 步 / 49 次 bash。
2. **门禁误报**：`subcondition_keywords` 对 lab 参考值范围（"男≤26"）、性别词、药名做关键词命中，把化验单参考区间当成入排命中，agent 花大量步数自证误报。
3. **`python3 -c` 内联生成大 JSON 的转义循环**：agent 用内联 Python 写 28 条判定 JSON，中文引号 `“ ”` 被 Python 当语法错误，反复改转义/三引号/重写脚本至少 6 轮。
4. **重复读文件，dedup 未生效**：`SKILL.md` 被读 10 次、`criteria_judge_IN.json` 读 4 次、`ocr_records.md` 读 11 次，且夹杂多个**空输出 AI 步骤**。
5. **主 run `llm.error`**：`deepseek-v4-pro` 一次调用 120s 未收到 streaming chunk 后断流，靠重试恢复。
6. **runs 状态机未收尾 + 监控口径陷阱**：follow-up run 的 11 个 subagent task 全部 `completed`，但 `runs.status` 仍标 `running`，导致 `analyze_eligibility_run.py` 把它当成 0 token 的空 run **漏算**，任何基于该脚本的基线对比都会严重失真。

---

## 1. 会话拓扑与真实消耗

会话由**两个 run** 组成（这是最反常的结构）：

| run_id | 时间（UTC） | 时长 | status | total_tokens | llm_calls | 性质 |
|---|---|---|---|---|---|---|
| `605aaf9a` | 12:34:22–12:38:58 | 4.6m | success | 628,031 | 14 | lead agent Phase 1 预处理（PDF 拆页 / OCR / 标准提取），**无 subagent** |
| `1dd83ab5`（幽灵 run） | 12:39:25–13:12:40 | 33m | **running（未收尾）** | 17,244,469 | 56 | 11 个 subagent task：解析 → QC → 修复 → 判定 → QC |

主 run 成功后，前端续传（`kwargs.input.messages` 内容为 `"2"`）触发了一个 follow-up run，这个 run 跑完了全部 11 个 task 却没有把状态标成 `success`。

**两 run 合计：约 17.9M token，33 分钟。**

follow-up run 的 token 分布（runs 表完整行）：

```
total_tokens:        17,244,469
  lead_agent_tokens:  5,199,025
  subagent_tokens:   12,045,444
  middleware_tokens:         0
token_usage_by_model: {'deepseek-v4-pro': {input: 17,036,775, output: 207,694, total: 17,244,469}}
```

> 注意：lead_agent_tokens 5.2M 是 follow-up run 内 lead 编排 11 个 subagent 的开销，subagent_tokens 12.05M 是子代理自身消耗。

---

## 2. 11 个 subagent task 的 token / 步数 / 耗时

| # | total_tokens | ai 步 | tool 步 | 耗时 | 描述 |
|---|---:|---:|---:|---:|---|
| 0 |   970,749 | 26 | 28 | 5.5m | IN 轨入选标准解析 |
| 1 |   863,469 | 23 | 27 | 4.9m | EX 轨排除标准解析 |
| 2 |   191,687 | 11 | 14 | 2.3m | IN 轨语义 QC 第 1 轮 |
| 3 |   251,042 | 10 | 23 | 2.1m | EX 轨语义 QC 第 1 轮 |
| 4 |   215,733 | 18 | 17 | 0.4m | EX 轨修复 EX-4 逻辑关系 |
| 5 |   154,726 |  8 | 12 | 0.0m | EX 轨语义 QC 第 2 轮 |
| 6 | **4,940,505** | **75** | **86** | **10.3m** | **IN 轨入选标准判定** ← 异常大头 |
| 7 | **3,309,135** | **55** | **64** |  **8.2m** | **EX 轨排除标准判定** ← 异常大头 |
| 8 |   524,266 | 20 | 28 | 2.4m | IN 轨判定 QC 第 1 轮 |
| 9 |   499,150 | 19 | 30 | 2.6m | EX 轨判定 QC 第 1 轮 |
| 10|   124,982 | 13 | 17 | 0.6m | IN 轨判定 QC 第 2 轮复检 |
| **合计** | **12,045,444** | 278 | 346 | 33m | |

**判定阶段（task 6 + 7）独占 8.25M token / 18.5 分钟 = 整个会话的 46%。** 这是优化的唯一重点。

---

## 3. 异常详解

### 3.1 判定阶段门禁死循环（task 6，最大问题）

task 6（IN 判定）49 次 bash 调用中：

- `uncertain_recheck.py` 跑了 **12 次**
- `check_reason_alignment.py` 跑了 **7 次**

循环模式：`跑门禁 → 发现 suspected_missed → grep/bash 排查 → 改 reason/JSON → 重跑门禁`，反复几十轮。两个典型案例：

**案例 A：IN-10-8 数字 "111" 反复绕门禁（step 117–136，连续 10 轮）**

OCR 实际文本是 `57-11um01/1`（乱码），agent 在 reason 里写了"上限约 111 μmol/L"，门禁的 `unsourced_number` 检查发现 "111" 不在 OCR 原文里，反复 flag。agent 的应对是反复改写 reason 里的数字表述（"111" → "约 111" → 删除数字），直到把数字从 reason 里彻底删掉才通过。这是**纯耗 token 的猫鼠游戏**，对判定结果质量无益。

**案例 B：连续 6 个 grep + 6 个空 AI 步骤（step 86–93）**

```
[86] AI   (空)
[87] TOOL grep
[88] AI   (空)
[89] TOOL grep
... 连续 6 轮 grep 无输出，AI 盲目试探
```

grep 对 `男`、`阿比特龙`、`恩扎卢胺` 等关键词搜索，命中全是 lab 参考范围（"男≤26"、"男 0-7"）或无关上下文，AI 产出空内容却继续推进流程。

### 3.2 门禁误报根因

`uncertain_recheck.py` 的 `subcondition_keywords` / `build_keywords` 对以下情况误报：

- **lab 参考值范围**："男≤26"、"男 0-7"、"男 6-17" 被当成性别相关入排命中
- **药名泛匹配**：`'新型内分泌治疗'` 等宽泛关键词命中无关段落
- **数字溯源过严**：reason 里任何数字都必须在 OCR 原文字面出现，但 OCR 有乱码（`57-11um01/1`），导致合法推断的数字被 flag

agent 不得不花大量步数证明这些是误报，而非真正漏判。

### 3.3 `python3 -c` 内联生成大 JSON 的转义循环（task 6 step 11–16）

agent 用 `python3 -c "..."` 内联写 28 条判定 JSON，中文引号 `“ ”` 被 Python 解析器当语法错误。应对轨迹：

```
[11] bash: python3 -c "..."  → 语法错误（中文引号）
[12] bash: python3 build_judgments_IN.py  → 改用脚本文件
[13] bash: python3 -c "读取脚本内容排查" 
[14] bash: python3 -c "再次排查"
[15] bash: python3 -c "正则替换引号"
[16] bash: "Rewrite the script using triple-quoted strings"
```

至少 6 轮无效 bash。正确做法是直接 `write_file` 落 JSON 文件，禁止 `python3 -c` 内联生成结构化数据。

### 3.4 重复读文件，dedup 未生效

| 文件 | 被读次数 | 所在 task |
|---|---:|---|
| `criteria-parser/SKILL.md` | 10 | task0 ×4, task1 ×6 |
| `criteria_judge_IN.json` | 4 | task6 |
| `criteria_parsed_EX.json` | 8 | task3 |
| `ocr/M019/ocr_records.md` | 11 | task7 |
| `criteria-parser/references/schema_example.json` | 3 | task0 ×1, task1 ×2 |

task 6 step [4]–[13] 反复读取 `criteria_judge_IN.json` + OCR 文件，其间 AI step [10][16][20][24] 产出**空内容**却推进流程。

这与已知的 [[criteria-token-saving-eval]] 指出的"read_file_dedup 纯纸面"一致——`read_file_dedup_middleware.py`（git status 显示为未提交新增文件）未真正生效。

### 3.5 主 run `llm.error`（seq=27）

```
"No streaming chunk received for 120.0s (model=deepseek-v4-pro, chunks_received=115).
 The connection may be alive at the TCP layer but is not producing content."
```

`deepseek-v4-pro` 一次调用收到 115 个 chunk 后断流 120s。靠重试恢复，但说明上游模型网关（`ai-gateway.fosunpharma.com`）对长输出不稳定。

### 3.6 runs 状态机未收尾 + 监控口径陷阱

follow-up run `1dd83ab5` 的 11 个 subagent task **全部 `completed`（0 未结束）**，但 `runs.status` 仍为 `running`。

直接后果：`backend/scripts/analyze_eligibility_run.py` 用 `RunStore.list_by_thread` 取 run 行时，该 run 在快照时 `total_tokens=0`、`status=running`，脚本把它当成空 run，最终只报告了主 run 的 628k token / 2 个 task，**漏掉了 17.2M token 的真实爆炸**。任何基于该脚本的"优化前后基线对比"都会因此失真。

---

## 4. 修复建议（按优先级）

### P0：修 `uncertain_recheck` 误报逻辑（task 6/7 token 根因）

**落点**：`skills/custom/eligibility-judgment/scripts/uncertain_recheck.py`（`subcondition_keywords` / `build_keywords` 函数）

- lab 参考值范围（`男≤26`、`男 0-7` 这类"性别 + 数值区间"模式）不应触发入排关键词命中。给关键词匹配加**上下文排除**：命中后检查所在行是否为参考范围格式（`数值-数值` / `≤数值` / `≥数值`），是则跳过。
- 药名匹配应收紧为**整词/精确药名**，而非 `'新型内分泌治疗'` 这类宽泛短语。
- `unsourced_number` 检查应区分**判定依据数字**（必须溯源）与**解释性表述数字**（如"上限约 111"），后者不应硬性要求 OCR 字面命中；或允许 agent 标注 `ocr_corrupted=true` 跳过字面溯源。

预期收益：直接砍掉 task 6 中 12 次 `uncertain_recheck` 重试的大半，以及伴随的 grep 排查步骤。

### P0：门禁死循环加熔断

**落点**：`uncertain_recheck.py` / `check_reason_alignment.py` 的调用约定，或 eligibility-judgment skill 的 SOUL 规则。

- 同一 `suspected_missed` 项重跑 **N 次（建议 2–3）** 未清，应升级为 `uncertain` 结论（让人工复核），而非无限重试。当前 12 次重跑是失控的。
- 在 skill 层面（`skills/custom/eligibility-judgment/skill.md`）加规则："同一门禁项连续 3 次未通过即标 uncertain 并推进，禁止继续改写 reason 绕门禁。"

### P1：判定 JSON 用 `write_file` 落盘，禁止 `python3 -c` 内联

**落点**：`skills/custom/eligibility-judgment/skill.md` 规则 + bash 工具策略。

- 判定产物必须用 `write_file` 直接写 JSON 文件，禁止 `python3 -c` / heredoc 内联生成结构化 JSON。
- 理由：中文引号 / 转义是反复踩的坑（本次 6 轮浪费，20d7e28c 会话也出现过脚本式生成问题）。

### P1：落实 `read_file_dedup`

**落点**：`backend/packages/harness/deerflow/agents/middlewares/read_file_dedup_middleware.py`（当前未提交）+ `backend/packages/harness/deerflow/config/read_dedup_config.py`。

- 同一 task 内同一文件路径的 `read_file` 只真正读一次，后续命中缓存。
- 验收指标：`SKILL.md` 单 task 内读取次数 = 1（当前 task0=4, task1=6）。
- 关联 [[eligibility-screener-ocr-parse-document]] 与 [[criteria-token-saving-eval]]。

### P1：修 `analyze_eligibility_run.py` 漏算 running run

**落点**：`backend/scripts/analyze_eligibility_run.py` 的 `analyze_run`。

- 当前只对 `status=success` / 有 `total_tokens` 的 run 行求和。应改为：**只要 run 行存在就计入**，并用 `run_events` 的 `subagent.end.metadata.usage` 累加作为交叉校验，而非只信 runs 表的 `total_tokens` 标量。
- 或在文档/`--help` 里明确警告：`status=running` 的 run 会被漏算。
- 该 bug 会让所有"优化前后基线对比"失真，优先级实际接近 P0。

### P2：修 runs 状态机收尾

**落点**：run 生命周期管理（`backend/packages/harness/deerflow/runtime/runs/` 或 journal 层）。

- 所有 subagent task `completed` 后，run 应标 `success`。当前 follow-up run 11/11 task 完成却仍 `running`，是状态机未正确收尾。
- 排查点：follow-up run（由前端续传 `"2"` 触发）是否走了与首轮不同的收尾路径。

### P2：`deepseek-v4-pro` streaming 超时

**落点**：模型配置 / 网关侧。

- 确认 `ai-gateway.fosunpharma.com` 对长输出（本次 115 chunk 后断流）的稳定性。
- 可调 `LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S` 或在 `config.yaml` 模型段设 `stream_chunk_timeout`，避免 120s 空等。

---

## 5. 附录：数据复现方法

```bash
cd backend
# 完整报告（注意：会漏算 status=running 的 run，见 3.6）
uv run python scripts/analyze_eligibility_run.py 2d628340-ddd4-4588-ba14-21287b35d98e \
    --output /tmp/elig_report_2d628340.json

# 取真实数据（含漏算的 follow-up run）需直接查 Postgres：
#   runs 表：total_tokens / lead_agent_tokens / subagent_tokens / status
#   run_events 表：subagent.start/.step/.end（per-task usage 在 .end.metadata.usage）
# list_events 需显式 user_id=None opt-out（CLI 无 request contextvar）
```

事件类型分布（follow-up run `1dd83ab5`）：`subagent.step` ×491，`subagent.start` ×8（首批快照），完整 11 个 task 的 start/end 在全量拉取后可见。
